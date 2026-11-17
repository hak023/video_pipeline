"""403 이후 OpenX download 페이지로 넘기는 인수.

임베드를 최상위로 열면 프레임 탈출로 리다이렉트되므로 iframe 안에서 플레이어를 준비한다.
about:blank 에 올리면 스토리지가 막혀 로더에서 멈추므로, 가능하면 글 페이지 origin 에 iframe 을 넣는다.
플레이어는 클릭 없이는 컨트롤바와 Download 버튼을 만들지 않는다. overlay/playback 클릭과
재생을 시도한 뒤 30초만 버튼을 본다. 그 안에 있으면 download 페이지를 열고, 없으면
스트리밍으로 본편 mp4가 모일 때까지 유지한다. 광고 반복과 Download Now 는 사용자 Chrome 의
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
# 스트리밍은 한 탭이 영상 길이만큼 붙잡는다. 직렬이면 배치가 너무 느려서 재생 탭은 10개까지 연다.
MAX_CONCURRENT_HANDOFFS = 10
# download 탭은 Tampermonkey용으로 닫지 않는다. 그래서 배치가 상한 없이 쌓지 않게 한다.
MAX_OPEN_DOWNLOAD_PAGES = 3
_HANDOFF_GUARD = threading.Lock()
_HANDOFF_ACTIVE = 0
_HANDOFF_COND = threading.Condition(_HANDOFF_GUARD)
DOWNLOAD_BUTTON_SELECTOR = '[aria-label="Download video"], [button="download"]'
# 이 시간 안에 버튼이 없으면 버튼이 없는 영상으로 보고, 스트리밍 수신을 유지한다.
BUTTON_DECISION_MS = 30_000
# 스트리밍은 버튼 판정과 별개다. 본편 바이트가 끝날 때까지 두되, 무한 대기는 막는다.
CAPTURE_MAX_SECONDS = 3 * 60 * 60
_SEEK_GAP_JS = """(ratio) => {
    const videos = Array.from(document.querySelectorAll('video'));
    const video = videos.sort((a, b) => (b.duration || 0) - (a.duration || 0))[0];
    if (!video) return {ok: false};
    if (video.paused) {
        video.play().catch(() => {});
    }
    const duration = Number.isFinite(video.duration) ? video.duration : 0;
    if (ratio != null && duration > 1) {
        const target = Math.min(duration - 0.05, Math.max(0, Number(ratio) * duration));
        if (Math.abs((video.currentTime || 0) - target) > 1.5) {
            video.currentTime = target;
        }
    }
    return {ok: true, duration, current: video.currentTime || 0, paused: video.paused};
}"""
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
        // muted는 소리만 끈다. 이 플레이어는 하나의 mp4를 Range로 받으므로 수신 바이트는 줄지 않는다.
        // 끄면 제스처 없는 play()가 막혀 요청 자체가 시작되지 않을 수 있다.
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


def _wait_for_download_button(page, timeout_ms: int, capture=None) -> None:
    """버튼 대기 중에도 본문 읽기를 이벤트 밖에서 한다. 즉시 실패하면 재시도로 시간을 끌지 않는다."""
    import time

    locator = page.frame_locator("#player_frame").locator(DOWNLOAD_BUTTON_SELECTOR).first
    deadline = time.monotonic() + max(int(timeout_ms), 0) / 1000
    while True:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError("download button wait timed out")
        started = time.monotonic()
        try:
            locator.wait_for(state="attached", timeout=min(500, remaining_ms))
            return
        except Exception as exc:
            if type(exc).__name__ != "TimeoutError":
                raise
            if time.monotonic() - started < 0.05 and remaining_ms > 500:
                raise
        if capture is not None:
            drain = getattr(capture, "drain", None)
            if drain is not None:
                drain()


def _seek_playback_gap(page, ratio) -> None:
    """비어 있는 바이트 위치로 재생을 옮겨, 브라우저가 그 Range를 받게 한다."""
    for frame in getattr(page, "frames", []):
        if frame == getattr(page, "main_frame", None):
            continue
        try:
            frame.evaluate(_SEEK_GAP_JS, ratio)
        except Exception:
            continue


def streaming_stop_reason(
    *,
    quiet: float,
    complete: bool,
    covered: int,
    total: int,
    media_seen: int,
    inflight: int,
    tap_bytes: int = 0,
) -> str:
    """수신이 멈춘 이유. 빈 문자열이면 계속 기다린다.

    미디어 응답을 보기만 하고 본문이 아직 없으면 45초에 끊지 않는다.
    그때 끊으면 재생은 됐는데 저장은 0인 채로 끝난다.
    """
    if inflight:
        return ""
    if complete and quiet >= 3:
        return "complete"
    if quiet >= 90 and total and covered < total:
        return "stalled"
    if quiet >= 45 and covered <= 0 and media_seen <= 0 and tap_bytes <= 0:
        return "no_media"
    if quiet >= 90 and covered <= 0 and (media_seen > 0 or tap_bytes > 0):
        return "media_unreadable"
    return ""


def _capture_until_full(page, capture, max_seconds: int = CAPTURE_MAX_SECONDS) -> None:
    """버튼이 없다고 판정한 뒤의 스트리밍 수신이다. 이후 버튼이 생겨도 download 페이지로 바꾸지 않는다."""
    import time

    deadline = time.monotonic() + max(int(max_seconds), 1)
    last_signature = None
    quiet_since = time.monotonic()
    last_log = 0.0
    # 이 탭을 앞으로 가져오면 다른 재생 탭이 백그라운드로 내려가 요청이 멈출 수 있다.
    log("  [재생 캡처] 다른 재생 탭을 멈추지 않도록 이 탭을 앞으로 가져오지 않습니다.")
    while time.monotonic() < deadline:
        try:
            capture.attach_frames(page)
        except Exception:
            pass
        try:
            capture.drain()
        except Exception:
            pass
        progress = capture.media_progress()
        covered = int(progress.get("covered") or 0)
        total = int(progress.get("total") or 0)
        media_seen = int(getattr(capture, "media_seen", 0) or 0)
        inflight = int(getattr(capture, "tap_inflight", 0) or 0)
        tap_bytes = int(getattr(capture, "tap_bytes", 0) or 0)
        signature = (covered, total, progress.get("file_count"), media_seen, inflight, tap_bytes)
        now = time.monotonic()
        if signature != last_signature:
            last_signature = signature
            quiet_since = now
        quiet = now - quiet_since
        reason = streaming_stop_reason(
            quiet=quiet,
            complete=bool(progress.get("complete")),
            covered=covered,
            total=total,
            media_seen=media_seen,
            inflight=inflight,
            tap_bytes=tap_bytes,
        )
        if reason == "complete":
            log(f"  [재생 캡처] 본편 전체 수신 {covered}/{total} bytes")
            capture.stop_reason = reason
            return
        if reason:
            capture.stop_reason = reason
            log(f"  [재생 캡처] 중단 사유: {reason}. 받은 본문 {covered}/{total} bytes, 미디어 응답 {media_seen}건")
            return
        if now - last_log >= 5:
            last_log = now
            if total:
                log(f"  [재생 캡처] 받는 중 {covered}/{total} bytes ({covered * 100 // total}%)")
            elif tap_bytes:
                log(f"  [재생 캡처] 미디어 응답을 파일로 받는 중 {tap_bytes} bytes")
            else:
                log(f"  [재생 캡처] 받는 중 {covered} bytes, 전체 길이 아직 모름")
        if quiet >= 8 and covered and inflight == 0 and progress.get("gap_ratio") is not None:
            _seek_playback_gap(page, progress.get("gap_ratio"))
        try:
            page.wait_for_timeout(1000)
        except Exception:
            capture.stop_reason = capture.stop_reason or "page_closed"
            return
    capture.stop_reason = getattr(capture, "stop_reason", "") or "limit"
    log("  [재생 캡처] 최대 대기 시간에 도달해 중단")


def total_known(progress: dict) -> bool:
    return bool(int(progress.get("total") or 0))


def _prime_embed_player(page, keep_ids: set[int] | None = None, capture=None) -> None:
    """로더에 멈춘 플레이어를 깨운다. 수동 재생은 웹페이지 클릭 뒤에야 컨트롤바가 생긴다."""
    for rnd in range(_PRIME_ROUNDS):
        if rnd == 0 and capture is not None:
            log("  [재생 캡처] 음소거로 재생합니다. 자동재생용이며 mp4 Range 수신량에는 영향 없습니다.")
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
        if capture is not None:
            drain = getattr(capture, "drain", None)
            if drain is not None:
                drain()
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
    timeout_ms: int = BUTTON_DECISION_MS,
    playwright=None,
    capture_dir: str | Path | None = None,
    max_open_download_pages: int = MAX_OPEN_DOWNLOAD_PAGES,
    max_concurrent_handoffs: int = MAX_CONCURRENT_HANDOFFS,
) -> dict[str, Any]:
    """30초 안에 download 버튼이 있으면 download 페이지로, 없으면 스트리밍으로 본편을 받는다.

    버튼 판정이 끝난 뒤에는 경로를 바꾸지 않는다.
    반환 ok 는 download 페이지를 열었거나 스트리밍을 마쳤다는 뜻이지, 파일 완료는 playback.complete 로 본다.
    연결한 Chrome 은 사용자의 브라우저이므로 닫지 않는다.
    download 탭 상한은 download 페이지를 새로 열지만 막고, 버튼이 없는 스트리밍은 막지 않는다.
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
                    keep_ids = {id(existing) for existing in getattr(context, "pages", [])}
                    wrapper = context.new_page()
                    keep_ids.add(id(wrapper))
                    if capture_dir:
                        capture = PlaybackCapture(Path(capture_dir))
                        capture.install(wrapper, cdp_url=cdp_url)
                        log(f"  [재생 캡처] 버튼 확인 전에 패킷 기록을 시작합니다: {capture.out_dir}")
                        tap_status = getattr(capture, "tap_status", "")
                        tap_error = getattr(capture, "tap_error", "")
                        log(
                            f"  [재생 캡처] 이 탭의 미디어 응답 저장 연결: {tap_status}"
                            + (f" ({tap_error})" if tap_error else "")
                        )
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
                    decision_ms = max(0, int(timeout_ms))
                    log(f"  [분기] download 버튼은 {decision_ms // 1000}초만 확인합니다. 없으면 스트리밍을 유지합니다.")
                    try:
                        _wait_for_download_button(wrapper, decision_ms, capture)
                    except Exception as exc:
                        if type(exc).__name__ != "TimeoutError":
                            raise
                        log(f"  [분기] {decision_ms // 1000}초 안에 download 버튼이 없어 스트리밍 다운로드를 유지합니다.")
                        if capture is not None and hasattr(capture, "media_progress"):
                            _capture_until_full(wrapper, capture)
                        keep_capture = True
                        playback = capture.summary() if capture else None
                        saved = len((playback or {}).get("files") or [])
                        log(
                            f"  [재생 캡처] download 버튼 없음. "
                            f"전체={bool((playback or {}).get('complete'))} "
                            f"{(playback or {}).get('covered') or 0}/{(playback or {}).get('total') or 0} bytes, "
                            f"조각 {saved}개 / 건너뜀 {(playback or {}).get('skipped_count', 0)}개, "
                            f"중단 {(playback or {}).get('stop_reason') or 'limit'}"
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
                    open_count = download_page_count(session)
                    if open_count is not None and open_count >= page_limit:
                        reason = (
                            f"download 버튼은 있으나 Chrome에 download 페이지가 이미 {open_count}개입니다 "
                            f"(상한 {page_limit}). 새 download 탭은 열지 않습니다. "
                            "스트리밍으로 전환하지 않으며, 이번 항목은 완료로 기록하지 않습니다."
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
                    target_id = _open_background_tab(session, page_url)
                    log(f"  [분기] {decision_ms // 1000}초 안에 download 버튼이 있어 download 페이지로 넘깁니다.")
                    if capture:
                        log("  [재생 캡처] 임시 스트리밍 패킷은 지웁니다.")
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
