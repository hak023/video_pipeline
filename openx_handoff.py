"""403 이후 OpenX download 페이지로 넘기는 인수.

임베드를 최상위로 열면 프레임 탈출로 리다이렉트되므로 iframe 안에서 플레이어를 준비한다.
about:blank 에 올리면 스토리지가 막혀 로더에서 멈추므로, 가능하면 글 페이지 origin 에 iframe 을 넣는다.
플레이어는 클릭 없이는 컨트롤바와 Download 버튼을 만들지 않는다. overlay/playback 클릭과
재생을 시도한 뒤에만 download 페이지를 연다. 광고 반복과 Download Now 는 사용자 Chrome 의
Tampermonkey 가 처리한다. Turnstile 토큰은 주입하지 않는다.
"""
from __future__ import annotations

import html
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from diagnostics import log_event, mask_text
from playback_capture import PlaybackCapture
from playwright.sync_api import sync_playwright
from progress import log

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DOWNLOAD_PAGE_ORIGIN = "https://download.openx.xyz/"
# 사용자 Chrome 하나에서 재생·라우팅을 동시에 하면 메모리와 CPU가 바로 찬다.
MAX_CONCURRENT_HANDOFFS = 1
# download 탭은 Tampermonkey용으로 닫지 않는다. 그래서 배치가 상한 없이 쌓지 않게 한다.
MAX_OPEN_DOWNLOAD_PAGES = 3
_HANDOFF_GUARD = threading.Lock()
_HANDOFF_ACTIVE = 0
_HANDOFF_COND = threading.Condition(_HANDOFF_GUARD)
DOWNLOAD_BUTTON_SELECTOR = '[aria-label="Download video"], [button="download"]'
_PRIME_ROUNDS = 4
_PRIME_PLAYER_JS = """() => {
    const sels = ['#overlay', '#playback', '#player-button', '.play-button', '.vjs-big-play-button',
                  'button.play', '.jw-icon-display', '.jw-display-icon-container',
                  'button[aria-label*="Play"]'];
    let n = 0;
    for (const s of sels) {
        document.querySelectorAll(s).forEach(el => { try { el.click(); n++; } catch (e) {} });
    }
    try {
        const ov = document.getElementById('overlay');
        if (ov) {
            ov.click();
            ov.remove();
            n++;
        }
    } catch (e) {}
    try {
        if (typeof jwplayer === 'function') {
            const jw = jwplayer();
            if (jw && typeof jw.play === 'function') { jw.play(); n++; }
        }
    } catch (e) {}
    document.querySelectorAll('video').forEach(v => {
        try { v.muted = true; const p = v.play(); if (p && p.catch) p.catch(() => {}); n++; } catch (e) {}
    });
    return n;
}"""
_SLUG_RE = re.compile(r"[A-Za-z0-9_-]{4,64}")
_ACCESS_DENIED_RE = re.compile(r"HTTP Error 403|HTTP 403|UserProjectAccountProblem")
_SLUG_LOCKS_GUARD = threading.Lock()
_SLUG_LOCKS: dict[str, threading.Lock] = {}


def is_openx_access_denied(reason: str | None) -> bool:
    """헤더 재시도와 진단이 끝난 뒤의 403/제공자 billing 오류인지."""
    return bool(_ACCESS_DENIED_RE.search(reason or ""))


def video_slug(embed_url: str | None) -> str | None:
    if not embed_url:
        return None
    try:
        values = parse_qs(urlsplit(embed_url).query).get("v") or []
    except ValueError:
        return None
    slug = values[0].strip() if values else ""
    return slug if _SLUG_RE.fullmatch(slug) else None


def download_page_url(slug: str) -> str:
    return f"{DOWNLOAD_PAGE_ORIGIN}?v={slug}"


def _wrapper_html(embed_url: str) -> str:
    src = html.escape(embed_url, quote=True)
    return (
        "<!DOCTYPE html><html><head></head><body style='margin:0;padding:0;'>"
        f"<iframe id='player_frame' src='{src}' width='1280' height='720'></iframe>"
        "</body></html>"
    )


def _slug_lock(slug: str) -> threading.Lock:
    """같은 영상만 중복 탭을 막는다. 다른 영상도 Chrome 인수 슬롯 안에서만 연다."""
    with _SLUG_LOCKS_GUARD:
        lock = _SLUG_LOCKS.get(slug)
        if lock is None:
            lock = threading.Lock()
            _SLUG_LOCKS[slug] = lock
        return lock


def _cdp_session(browser, context):
    opener = getattr(browser, "new_browser_cdp_session", None)
    if opener:
        return opener()
    if context.pages:
        return context.new_cdp_session(context.pages[0])
    return None


@contextmanager
def _handoff_slot(limit: int):
    """사용자 Chrome에서 동시에 재생·캡처하는 수를 제한한다."""
    global _HANDOFF_ACTIVE
    slots = max(1, int(limit))
    with _HANDOFF_COND:
        while _HANDOFF_ACTIVE >= slots:
            _HANDOFF_COND.wait()
        _HANDOFF_ACTIVE += 1
    try:
        yield
    finally:
        with _HANDOFF_COND:
            _HANDOFF_ACTIVE -= 1
            _HANDOFF_COND.notify_all()


def download_page_count(session) -> int | None:
    """열려 있는 download.openx.xyz 탭 수. 세지 못하면 None."""
    if session is None:
        return None
    try:
        listed = session.send("Target.getTargets", {}) or {}
    except Exception:
        return None
    count = 0
    for info in listed.get("targetInfos") or []:
        if info.get("type") not in (None, "page"):
            continue
        if "download.openx.xyz" in str(info.get("url") or ""):
            count += 1
    return count


def _matching_download_target(session, page_url: str) -> str | None:
    slug = video_slug(page_url)
    if session is None or not slug:
        return None
    try:
        listed = session.send("Target.getTargets", {}) or {}
    except Exception:
        return None
    for info in listed.get("targetInfos") or []:
        if info.get("type") not in (None, "page"):
            continue
        url = str(info.get("url") or "")
        if "download.openx.xyz" in url and video_slug(url) == slug:
            return info.get("targetId")
    return None


def _article_url(page_url: str | None) -> str:
    url = (page_url or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


def _open_embed_frame(page, embed_url: str, page_url: str | None) -> str:
    """임베드를 iframe 으로 연다. 글 페이지 origin 을 우선하고, 실패하면 wrapper 로 폴백한다."""
    article = _article_url(page_url)
    if article:
        try:
            page.goto(article, wait_until="domcontentloaded", timeout=15_000)
            page.evaluate(
                """(src) => {
                    const frame = document.createElement('iframe');
                    frame.id = 'player_frame';
                    frame.src = src;
                    frame.width = '1280';
                    frame.height = '720';
                    frame.allow = 'autoplay; fullscreen';
                    frame.allowFullscreen = true;
                    document.body.replaceChildren(frame);
                }""",
                embed_url,
            )
            return "article"
        except Exception:
            pass
    page.set_content(_wrapper_html(embed_url), wait_until="domcontentloaded")
    return "wrapper"


def _is_extension_block_url(url: str | None) -> bool:
    """AdGuard 등이 광고 탭을 막으면 주소가 chrome-extension:// 로 바뀐다."""
    return (url or "").startswith("chrome-extension://")


def _close_extension_block_pages(page, keep_ids: set[int] | None = None) -> None:
    """이번 작업으로 새로 열린 AdGuard 차단 탭만 닫는다. 기존 탭과 작업 페이지는 유지한다."""
    try:
        pages = list(page.context.pages)
    except Exception:
        return
    for other in pages:
        if other == page or (keep_ids is not None and id(other) in keep_ids):
            continue
        try:
            url = other.url or ""
        except Exception:
            continue
        if not _is_extension_block_url(url):
            continue
        try:
            other.close()
        except Exception:
            pass


def _prime_embed_player(page, keep_ids: set[int] | None = None, capture=None) -> None:
    """로더에 멈춘 플레이어를 깨운다. 수동 재생은 웹페이지 클릭 뒤에야 컨트롤바가 생긴다."""
    for rnd in range(_PRIME_ROUNDS):
        if capture is not None:
            capture.attach_frames(page)
        _close_extension_block_pages(page, keep_ids)
        try:
            box = page.locator("#player_frame").bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        except Exception:
            pass
        for fr in getattr(page, "frames", []):
            if fr == getattr(page, "main_frame", None):
                continue
            try:
                fr.evaluate(_PRIME_PLAYER_JS)
            except Exception:
                pass
        try:
            page.frame_locator("#player_frame").locator(
                "#overlay, #playback, .jw-icon-display, .jw-display-icon-container"
            ).first.click(timeout=1000, force=True)
        except Exception:
            pass
        try:
            page.wait_for_timeout(800 if rnd == 0 else 1200)
        except Exception:
            pass
        _close_extension_block_pages(page, keep_ids)


def _open_background_tab(session, url: str) -> str | None:
    """download 페이지를 배경 탭으로 연다. 포커스를 가져오지 않는 쪽을 우선한다."""
    try:
        created = session.send("Target.createTarget", {"url": url, "background": True})
    except Exception:
        created = session.send("Target.createTarget", {"url": url})
    return (created or {}).get("targetId")


def open_download_handoff(
    embed_url: str | None,
    *,
    page_url: str | None = None,
    cdp_url: str = DEFAULT_CDP_URL,
    timeout_ms: int = 60_000,
    playwright=None,
    capture_dir: str | Path | None = None,
    max_open_download_pages: int = MAX_OPEN_DOWNLOAD_PAGES,
    max_concurrent_handoffs: int = MAX_CONCURRENT_HANDOFFS,
) -> dict[str, Any]:
    """플레이어 Download 버튼이 생긴 뒤 download 페이지를 연다.

    반환 ok 는 페이지를 열었다는 뜻이지 파일이 저장됐다는 뜻이 아니다.
    버튼이 없으면, 재생 시작 전에 켜 둔 캡처의 패킷을 playback 으로 돌려준다.
    연결한 Chrome 은 사용자의 브라우저이므로 닫지 않는다.
    download 탭이 상한 이상이면 새 탭과 재생을 시작하지 않는다.
    """
    slug = video_slug(embed_url)
    if not slug or not embed_url:
        return {"ok": False, "reason": "임베드 주소에서 영상 id(v)를 찾지 못했습니다"}
    article_url = page_url
    page_url = download_page_url(slug)
    browser = None
    wrapper = None
    player_ready = False
    session = None
    capture: PlaybackCapture | None = None
    keep_capture = False
    page_limit = max(0, int(max_open_download_pages))
    # 같은 Chrome을 여러 워커가 동시에 재생하지 않는다. 같은 영상 id는 그 안에서도 한 번만 연다.
    with _handoff_slot(max_concurrent_handoffs):
        with _slug_lock(slug):
            try:
                with (playwright or sync_playwright()) as p:
                    browser = p.chromium.connect_over_cdp(cdp_url)
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    session = _cdp_session(browser, context)
                    existing = _matching_download_target(session, page_url)
                    if existing:
                        return {
                            "ok": True,
                            "reused": True,
                            "slug": slug,
                            "download_page": page_url,
                            "target_id": existing,
                        }
                    open_count = download_page_count(session)
                    if open_count is not None and open_count >= page_limit:
                        reason = (
                            f"Chrome에 download 페이지가 이미 {open_count}개 열려 있습니다 "
                            f"(상한 {page_limit}). PC 부하를 막기 위해 새 탭과 재생 캡처를 시작하지 않았습니다. "
                            "download.openx.xyz 탭을 닫은 뒤 다시 실행하세요. 이번 항목은 완료로 기록하지 않습니다."
                        )
                        log(f"  [페이지 상한] {reason}")
                        log_event(
                            "playback_capture",
                            "page_limit",
                            reason,
                            level="WARNING",
                            page_url=embed_url,
                            open_download_pages=open_count,
                            max_open_download_pages=page_limit,
                        )
                        return {
                            "ok": False,
                            "page_limit": True,
                            "slug": slug,
                            "download_page": None,
                            "open_download_pages": open_count,
                            "reason": reason,
                        }
                    keep_ids = {id(existing) for existing in getattr(context, "pages", [])}
                    wrapper = context.new_page()
                    keep_ids.add(id(wrapper))
                    if capture_dir:
                        capture = PlaybackCapture(Path(capture_dir))
                        capture.install(wrapper)
                        log(f"  [재생 캡처] 버튼 확인 전에 패킷 기록을 시작합니다: {capture.out_dir}")
                    embed_host = _open_embed_frame(wrapper, embed_url, article_url)
                    if capture is not None:
                        capture.attach_frames(wrapper)
                    _prime_embed_player(wrapper, keep_ids, capture)
                    if capture is not None:
                        log(
                            f"  [재생 캡처] CDP 세션 {getattr(capture, 'cdp_sessions', 0)}개 "
                            f"(iframe 별도 세션이 없으면 1개), "
                            f"같은 세션 프레임 {getattr(capture, 'frame_attach_skipped', 0)}개"
                        )
                    try:
                        wrapper.frame_locator("#player_frame").locator(DOWNLOAD_BUTTON_SELECTOR).first.wait_for(
                            state="attached", timeout=timeout_ms,
                        )
                    except Exception as exc:
                        if type(exc).__name__ != "TimeoutError":
                            raise
                        keep_capture = True
                        playback = capture.summary() if capture else None
                        saved = len((playback or {}).get("files") or [])
                        log(
                            f"  [재생 캡처] download 버튼 없음. "
                            f"저장 {saved}개 / 건너뜀 {(playback or {}).get('skipped_count', 0)}개 / "
                            f"cdp 세션 {(playback or {}).get('cdp_sessions', 0)} / "
                            f"cdp 응답 {(playback or {}).get('cdp_responses', 0)} / "
                            f"response {(playback or {}).get('response_count', 0)}"
                        )
                        log_event(
                            "playback_capture",
                            "kept_no_download_button",
                            "download 버튼이 없어 재생 패킷을 유지",
                            page_url=embed_url,
                            saved=saved,
                            capture_dir=str(capture.out_dir) if capture else "",
                        )
                        return {
                            "ok": True,
                            "no_download_button": True,
                            "reused": False,
                            "slug": slug,
                            "download_page": None,
                            "playback": playback,
                            "reason": "임베드에 download 버튼이 없습니다",
                        }
                    player_ready = True
                    if session is None:
                        session = _cdp_session(browser, context)
                    target_id = _open_background_tab(session, page_url)
                    if capture:
                        log("  [재생 캡처] download 버튼이 있어 임시 패킷은 지우고 download 페이지로 넘깁니다")
                    return {
                        "ok": True,
                        "reused": False,
                        "slug": slug,
                        "download_page": page_url,
                        "target_id": target_id,
                        "embed_host": embed_host,
                    }
            except Exception as exc:
                detail = f"{type(exc).__name__}: {mask_text(str(exc))[:180]}"
                keep_capture = capture is not None
                playback = capture.summary() if capture else None
                if browser is None:
                    reason = (
                        f"Tampermonkey가 설치된 Chrome(CDP {cdp_url})에 연결하지 못했습니다. "
                        "--remote-debugging-port=9222 로 실행된 프로필인지 확인하세요. "
                        f"({detail})"
                    )
                elif not player_ready:
                    reason = f"임베드 플레이어가 준비되지 않아 download 페이지로 연결하지 않았습니다. ({detail})"
                else:
                    reason = f"플레이어는 준비됐지만 download 페이지를 열지 못했습니다. ({detail})"
                if playback and playback.get("files"):
                    reason += f" 그때까지 받은 재생 패킷 {len(playback['files'])}개는 남겨 두었습니다."
                return {
                    "ok": False,
                    "slug": slug,
                    "download_page": page_url,
                    "playback": playback,
                    "reason": reason,
                }
            finally:
                if capture is not None:
                    capture.uninstall(wrapper)
                    if not keep_capture:
                        capture.discard()
                if session is not None:
                    try:
                        session.detach()
                    except Exception:
                        pass
                if wrapper is not None:
                    try:
                        wrapper.close()
                    except Exception:
                        pass
