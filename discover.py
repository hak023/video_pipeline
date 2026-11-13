from __future__ import annotations

import html
import itertools
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

from ad_blacklist import blacklist
from diagnostics import (
    attach_page_diagnostics, get_diagnostics, log_event, log_exception, now_iso,
)
from media_classifier import classify_candidate, strong_ad_rules
from media_grouping import group_candidates, order_attempts
from media_tools import get_browser_launch_kwargs, get_context_kwargs
from progress import log


class DiscoverNavigationError(RuntimeError):
    """페이지 이동 실패 (ERR_CONNECTION_RESET, 타임아웃, HTTP 4xx/5xx 등) - 재시도 대상.
    HTTP 상태를 알 수 있으면 status 에 담는다 (404/410/451 은 호출측이 재시도하지 않는다)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


MEDIA_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
    "video/mp4",
)


def _is_closed_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "has been closed" in msg or "target closed" in msg or "browser has been closed" in msg


def _is_ad_embed(url: str) -> bool:
    """임베드(플레이어 iframe) URL 이 알려진 광고 호스트인지. 미디어 후보와 달리 플레이어가 아닌 광고 iframe 은
    영상 항목으로 세지 않는다. 판정 근거는 이벤트 로그에 남긴다."""
    m = blacklist.match(url)
    if m:
        log_event("media_discovery", "embed_skipped_ad", f"광고 iframe 로 보여 임베드에서 제외: {m['source']}:{m['pattern']}",
                  level="DEBUG", frame_url=url)
    return m is not None


def _get_content_length(
    url: str,
    referer: str = "",
    user_agent: str = "",
    proxy: str | None = None,
) -> int | None:
    """HEAD request to get Content-Length of an MP4 URL. Returns None on failure."""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={
            "Referer": referer,
            "User-Agent": user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    )
    try:
        with opener.open(req, timeout=8) as resp:
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl else None
    except Exception:
        return None


def _probe_m3u8_kind(info: dict[str, Any], proxy: str | None = None) -> str:
    """Return 'master', 'media', or 'unknown' by peeking at playlist body."""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)

    req = urllib.request.Request(
        info["url"],
        headers={
            "Referer": info.get("referer") or "",
            "User-Agent": info.get("user_agent")
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    )
    try:
        with opener.open(req, timeout=15) as resp:
            head = resp.read(16384).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return "unknown"
    if "#EXT-X-STREAM-INF" in head:
        return "master"
    if "#EXTINF" in head:
        return "media"
    return "unknown"


# 브라우저가 실제로 보낸 요청 헤더 중 다운로드 때 그대로 재현할 것들 (쿠키는 별도 처리)
_REPLAY_HEADERS = {
    "referer", "origin", "accept", "accept-language", "user-agent",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "authorization",
}
_SEGMENT_EXT = (".ts", ".m4s", ".aac", ".vtt", ".key", ".webvtt")
MAX_CAPTURED = 20


def _safe_request_headers(resp) -> dict[str, str]:
    try:
        raw = resp.request.all_headers()
    except Exception:
        return {}
    return {k.lower(): v for k, v in raw.items() if k.lower() in _REPLAY_HEADERS}


def _collect_cookies(ctx, urls: list[str]) -> list[dict[str, Any]]:
    try:
        urls = [u for u in dict.fromkeys(urls) if u.startswith("http")][:10]
        keys = ("name", "value", "domain", "path", "secure", "httpOnly", "expires")
        return [{k: c.get(k) for k in keys} for c in ctx.cookies(urls)]
    except Exception:
        return []


# ---- 재생 트리거: '버튼을 두 번 눌러야 재생'되는 플레이어 대응
MAX_PLAY_ROUNDS = 4        # 클릭 시도 횟수 (진짜 스트림이 잡히면 즉시 중단)
REAL_VIDEO_SECONDS = 90    # 이 길이를 넘는 플레이어 <video> 가 보이면 본편으로 판단
SHORT_VIDEO_SECONDS = 60   # 길이가 알려진 영상이 이보다 짧으면 재생 루프 종료 근거로 삼지 않음 (광고 가능성)

_CLICK_BTN_JS = """() => {
    const sels = ['#player-button', '.play-button', '.vjs-big-play-button', 'button.play',
                  '.plyr__control--overlaid', 'button[aria-label*="Play"]', '.jw-icon-display',
                  '.jw-display-icon-container', '.fp-play', '#overlay', '#playback'];
    let n = 0;
    for (const s of sels) {
        document.querySelectorAll(s).forEach(b => { try { b.click(); n++; } catch (e) {} });
        if (n) break;
    }
    return n;
}"""

_PLAY_VIDEO_JS = """() => {
    let n = 0;
    try {
        const ov = document.getElementById('overlay');
        if (ov) {
            for (let i = 0; i < 3; i++) {
                try { ov.click(); n++; } catch (e) {}
            }
            try { ov.remove(); } catch (e) {}
        }
    } catch (e) {}
    try {
        if (typeof jwplayer === 'function') {
            const jw = jwplayer();
            if (jw && typeof jw.play === 'function') { jw.play(); n++; }
        }
    } catch (e) {}
    document.querySelectorAll('video').forEach(v => { try { v.play(); n++; } catch (e) {} });
    return n;
}"""

_VIDEO_STATE_JS = """() => Math.max(0, ...Array.from(document.querySelectorAll('video'))
    .map(v => (isFinite(v.duration) ? v.duration : 0)))"""

_PLAYER_CENTER_JS = """() => {
    const el = document.querySelector('.flowplayer, .fp-player, .jwplayer, #player, iframe[src*="embed"]');
    if (!el) return null;
    el.scrollIntoView({block: 'center'});
    const r = el.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
}"""

_DOM_MEDIA_JS = """() => {
    const out = [], seen = new Set();
    const add = (s, d, extra) => {
        if (!s || typeof s !== 'string') return;
        if (s.startsWith('blob:') || s.startsWith('data:')) return;
        try { s = new URL(s, document.baseURI || location.href).href; } catch (e) { return; }   // //host/x, /x, x.mp4 모두 절대 URL 로
        if (s.startsWith('http') && !seen.has(s)) {
            seen.add(s);
            out.push(Object.assign({src: s, duration: (typeof d === 'number' && isFinite(d) && d > 0) ? d : null}, extra || {}));
        }
    };
    // JWPlayer: 페이지의 모든 인스턴스, 각 playlist 의 모든 항목
    try {
        if (typeof jwplayer === 'function') {
            const list = [], ids = new Set();
            document.querySelectorAll('.jwplayer, [id^="jwplayer"], #player').forEach(el => {
                try {
                    if (!el.id || ids.has(el.id)) return;
                    const jw = jwplayer(el.id);
                    if (jw && typeof jw.getPlaylist === 'function') { ids.add(el.id); list.push([jw, el.id]); }
                } catch (e) {}
            });
            if (!list.length) {
                const jw = jwplayer();
                if (jw && typeof jw.getPlaylist === 'function') list.push([jw, 'jwplayer-1']);
            }
            list.forEach(([jw, pid]) => {
                try {
                    const pl = jw.getPlaylist() || [];
                    const cur = (typeof jw.getDuration === 'function') ? jw.getDuration() : null;
                    const curIdx = (typeof jw.getPlaylistIndex === 'function') ? jw.getPlaylistIndex() : 0;
                    pl.forEach((item, i) => {
                        if (!item) return;
                        const dur = (typeof item.duration === 'number' && item.duration > 0) ? item.duration : (i === curIdx ? cur : null);
                        const extra = {playlist_index: i, player_id: pid, from_playlist: true};
                        (item.allSources || item.sources || []).forEach(src => src && src.file && add(src.file, dur, extra));
                        if (item.file) add(item.file, dur, extra);
                    });
                } catch (e) {}
            });
        }
    } catch (e) {}
    const player = document.querySelector('media-player, [data-media-player]');
    if (player) {
        const s = player.src || player.getAttribute('src');
        if (typeof s === 'string') add(s, player.duration); else if (s && s.src) add(s.src, player.duration);
    }
    document.querySelectorAll('video').forEach(v => {
        add(v.currentSrc || v.src || v.getAttribute('src'), v.duration);
        v.querySelectorAll('source').forEach(x => add(x.src || x.getAttribute('src'), v.duration));
    });
    document.querySelectorAll('source').forEach(x => add(x.src || x.getAttribute('src'), null));
    return out;
}"""

# ---- 팝업(광고) 처리: 우리가 만든 페이지가 아닌 새 탭은 전부 닫는다 (rel=noopener 팝업 포함)
_own = threading.local()


def _own_page(ctx):
    _own.flag = True
    try:
        return ctx.new_page()
    finally:
        _own.flag = False


def _close_stray_page(new_p) -> None:
    if getattr(_own, "flag", False):
        return
    _own.popups = getattr(_own, "popups", 0) + 1
    try:
        url = new_p.url
    except Exception:
        url = ""
    log_event("browser", "popup_closed", "광고 의심 팝업 탭을 닫음", level="DEBUG", frame_url=url)
    try:
        new_p.close()
    except Exception:
        pass


def _popup_count() -> int:
    return getattr(_own, "popups", 0)


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""

def _is_descendant_frame(frame: Any, root: Any) -> bool:
    """frame이 root 프레임 자체이거나 그 하위 프레임인지 확인."""
    current = frame
    while current is not None:
        if current == root:
            return True
        current = current.parent_frame
    return False


def _find_embed_frame(
    page: Any,
    embed_url: str,
    debug: dict[str, Any] | None = None,
) -> tuple[Any | None, tuple[float, float] | None]:
    """embed_url에 연결되는 iframe 프레임과 클릭 좌표를 찾는다.

    URL 비교는 fragment만 제외한다. bounding box가 없어도 iframe 프레임을
    찾았다면 매칭은 유효하며, 클릭 좌표만 None으로 반환한다.
    """
    expected = urljoin(page.url, embed_url).split("#", 1)[0]
    matches: list[tuple[Any, tuple[float, float] | None]] = []
    inspected: list[dict[str, Any]] = []

    for index, locator in enumerate(page.locator("iframe").all()):
        try:
            src = locator.get_attribute("src") or ""
            resolved_src = urljoin(page.url, src).split("#", 1)[0]

            element = locator.element_handle()
            
            frame = element.content_frame() if element else None
            frame_url = (frame.url or "").split("#", 1)[0] if frame else ""

            # src는 원본 iframe 주소, frame.url은 리다이렉트 후 주소일 수 있다.
            src_matches = resolved_src == expected
            frame_matches = frame_url == expected

            box = locator.bounding_box()
            click_point = None
            if box:
                click_point = (
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )

            inspected.append({
                "index": index,
                "src": src[:250],
                "resolved_src": resolved_src[:250],
                "frame_url": frame_url[:250],
                "src_matches": src_matches,
                "frame_matches": frame_matches,
                "frame_found": frame is not None,
                "bounding_box_found": box is not None,
            })

            if frame is not None and (src_matches or frame_matches):
                matches.append((frame, click_point))

        except Exception as e:
            inspected.append({
                "index": index,
                "error": f"{type(e).__name__}: {str(e).splitlines()[0][:150]}",
            })

    # 동일한 Frame 객체가 중복 수집된 경우는 한 개로 취급한다.
    unique_matches: list[tuple[Any, tuple[float, float] | None]] = []
    seen_frames: set[int] = set()
    for frame, point in matches:
        frame_id = id(frame)
        if frame_id not in seen_frames:
            seen_frames.add(frame_id)
            unique_matches.append((frame, point))

    if len(unique_matches) == 1:
        if debug is not None:
            debug["iframe_match"] = {
                "status": "matched",
                "expected": expected[:250],
                "iframe_count": len(inspected),
                "match_count": 1,
                "bounding_box_found": unique_matches[0][1] is not None,
                "iframes": inspected,
            }
        return unique_matches[0]

    if debug is not None:
        debug["iframe_match"] = {
            "status": "not_found" if not unique_matches else "ambiguous",
            "expected": expected[:250],
            "iframe_count": len(inspected),
            "match_count": len(unique_matches),
            "iframes": inspected,
        }

    return None, None

def _has_real_stream(candidates) -> bool:
    """재생 루프를 멈춰도 될 만큼 '본편일 가능성이 있는' 후보가 있는지.
    광고로 알려진 후보와 길이가 알려진 짧은 영상(<60s)은 근거로 삼지 않는다(광고 HLS 에서 조기 종료 방지)."""
    for item in candidates:
        c = item if isinstance(item, dict) else {"url": str(item)}
        u = c.get("url", "")
        dur = c.get("dom_duration") or 0
        m = blacklist.match(u)
        if m and m["source"] != "dynamic":
            continue
        if dur >= REAL_VIDEO_SECONDS:
            return True
        if 0 < dur < SHORT_VIDEO_SECONDS:
            continue
        low = u.lower()
        if ".m3u8" in low or ".mpd" in low:
            return True
        clean = low.split("?")[0]
        if any(clean.endswith(ext) for ext in (".mp4", ".mkv", ".webm")) and dur > 0:
            return True
    return False


def _eval_frames(
    page,
    js: str,
    hosts: set[str] | None = None,
    target_frame: Any | None = None,
) -> list[Any]:
    """허용된 프레임에서만 JS를 실행한다."""
    out: list[Any] = []
    for fr in page.frames:
        if target_frame is not None and not _is_descendant_frame(fr, target_frame):
            continue
        if hosts is not None and _host(fr.url) not in hosts:
            continue
        try:
            out.append(fr.evaluate(js))
        except Exception as e:
            if _is_closed_error(e):
                raise
    return out


def _resp_total_length(resp) -> int | None:
    """응답이 가리키는 파일 전체 크기. 206 응답의 Content-Length 는 조각 크기이므로 Content-Range 의 total 을 쓴다."""
    try:
        h = resp.headers
        rng = h.get("content-range") or ""
        m = re.search(r"/(\d+)\s*$", rng)
        if m:
            return int(m.group(1))
        if resp.status == 206:
            return None
        cl = h.get("content-length")
        return int(cl) if cl and cl.isdigit() else None
    except Exception:
        return None


def _is_special_stream_url(u: str) -> bool:
    """확장자 없이 제공되는 특정 플레이어 스트림(ssrr.org, /sora/ 경로)."""
    try:
        p = urlparse(u)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return host.endswith("ssrr.org") or "sora" in [s.lower() for s in p.path.split("/")]


def _media_candidate(
    resp,
    referer: str,
    allowed_hosts: set[str] | None,
    trace: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """응답이 미디어이면 후보 dict, 아니면 None. 광고 여부는 여기서 판단하지 않는다(수집과 판별 분리).

    allowed_hosts 가 주어지면(B 단계) 요청을 일으킨 프레임의 호스트가 플레이어 호스트일 때만 인정한다.
    frame_url 이 허용 목록에 없더라도 page/referer/origin 이 허용 목록과 같은 호스트면 통과한다.
    """
    u = resp.url
    path = urlparse(u).path.lower()
    ctype = (resp.headers.get("content-type") or "").lower()
    if path.endswith(_SEGMENT_EXT) or "mp2t" in ctype or "segment" in ctype:
        return None  # HLS/DASH 조각

    clean_path = u.split("?")[0].lower()
    path_media = path.endswith((".m3u8", ".mpd", ".mp4")) or clean_path.endswith((".m3u8", ".mpd", ".mp4"))
    special = _is_special_stream_url(u)
    if special:
        return {"url": u, "content_type": ctype, "referer": referer, "status": resp.status,
                "first_seen": now_iso(), "source": "network"}
    if not (path_media or "mpegurl" in ctype or "video" in ctype):
        return None

    frame_url = ""
    response_frame = None
    is_main_frame = False

    try:
        response_frame = resp.frame
        frame_url = response_frame.url
        is_main_frame = response_frame.parent_frame is None
    except Exception:
        pass

    request_headers = {}
    try:
        request_headers = {
            k.lower(): v for k, v in resp.request.all_headers().items()
        }
    except Exception:
        pass

    if allowed_hosts is not None:
        fh = _host(frame_url)

        # 플레이어 호스트 자체에서 온 요청은 허용한다.
        # 플레이어 클릭 중 최상위 글 페이지에서 발생한 요청은 호출부에서 별도 귀속한다.
        if fh not in allowed_hosts and not is_main_frame:
            why = f"frame_unresolved:{fh or '?'}"
            if trace is not None and len(trace) < 12:
                trace.append({"url": u[:140], "why": why})
            # 프레임 귀속 실패는 광고 판정이 아니다. AD 후보를 만들지 않는다.
            return None

    return {
        "url": u,
        "content_type": ctype,
        "referer": referer,
        "frame_url": frame_url,
        "frame_role": "main" if is_main_frame else "child",
        "status": resp.status,
        "content_length": _resp_total_length(resp),
        "first_seen": now_iso(),
        "source": "network",
        "request_headers": _safe_request_headers(resp),
    }


def _prune_segments(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """m3u8/mpd 아래(같은 디렉터리 트리)에 있는 .mp4 는 fMP4 조각일 가능성이 높아 후보에서 제외.
    같은 호스트라도 playlist 와 무관한 경로의 mp4(별도 본편)는 보존한다."""
    streams = [c for c in cands if ".m3u8" in c["url"].lower() or ".mpd" in c["url"].lower()]
    if not streams:
        return cands
    prefixes = [c["url"].split("?")[0].rsplit("/", 1)[0] + "/" for c in streams]
    keep = []
    for c in cands:
        if any(c is x for x in streams) or not any(c["url"].startswith(p) for p in prefixes):
            keep.append(c)
        else:
            log_event("media_discovery", "segment_pruned", "playlist 하위 조각으로 보아 제외", level="DEBUG",
                      candidate_id=c.get("candidate_id"), frame_url=c["url"])
    return keep


def _play_until_stream(
    page,
    captured,
    wait_ms: int,
    click_hosts: set[str] | None = None,
    video_hosts: set[str] | None = None,
    use_player_center: bool = False,
    target_frame: Any | None = None,
    click_point: tuple[float, float] | None = None,
) -> bool:
    """재생을 여러 번 시도해 '본편' 스트림이 나올 때까지 진행. 본편이 확인되면 True.

    첫 클릭이 광고(오버레이/팝업)에 소모되고 두 번째 클릭에서야 재생되는 플레이어를 위해:
      1라운드: 재생 버튼 JS 클릭
      2라운드~: 플레이어 중앙(없으면 화면 중앙)을 실제 마우스로 클릭 → 다시 JS 클릭
    click_hosts/video_hosts 로 클릭·재생 대상 프레임을 플레이어로 제한한다 (광고 영상 재생 방지).
    """
    for rnd in range(MAX_PLAY_ROUNDS):
        if rnd > 0:
            try:
                pt = click_point
                if pt is None and use_player_center:
                    pt = page.evaluate(_PLAYER_CENTER_JS)
                if not pt:
                    vp = page.viewport_size or {"width": 1280, "height": 720}
                    pt = (vp["width"] / 2, vp["height"] / 2)

                if isinstance(pt, dict):
                    page.mouse.click(pt["x"], pt["y"])
                else:
                    page.mouse.click(pt[0], pt[1])
            except Exception as e:
                if _is_closed_error(e):
                    raise
            page.wait_for_timeout(800)

        _eval_frames(page, _CLICK_BTN_JS, click_hosts, target_frame)
        _eval_frames(page, _PLAY_VIDEO_JS, video_hosts, target_frame)
        page.wait_for_timeout(2000 if rnd == 0 else 2500)
        longest = max([
            v for v in _eval_frames(page, _VIDEO_STATE_JS, video_hosts, target_frame)
            if isinstance(v, (int, float))
        ] or [0])
        confirmed = longest > REAL_VIDEO_SECONDS or any(
            isinstance(c, dict) and (c.get("dom_duration") or 0) >= REAL_VIDEO_SECONDS for c in captured.values())
        if _has_real_stream(captured.values() if isinstance(captured, dict) else captured) or longest > REAL_VIDEO_SECONDS:
            if not confirmed:
                page.wait_for_timeout(2500)   # 길이로 확인되지 않은 스트림: 광고 뒤에 본편 요청이 이어지는지 추가 관찰
            page.wait_for_timeout(1000)   # 플레이리스트/후속 요청이 잡히도록 잠깐 대기
            return True
    page.wait_for_timeout(wait_ms)
    return _has_real_stream(captured.values() if isinstance(captured, dict) else captured)


def _fill_page_debug(page, debug: dict[str, Any]) -> None:
    """임베드 페이지가 실제로 무엇을 보여줬는지 기록 (차단/잠금/오류 페이지 판별용)."""
    try:
        debug["final_url"] = page.url[:150]
        debug["title"] = (page.title() or "")[:80]
        debug["body"] = re.sub(r"\s+", " ", page.inner_text("body", timeout=1500))[:160]
        debug["frames"] = [fr.url[:100] for fr in page.frames]
    except Exception:
        pass


def _extract_embed_stream(
    ctx,
    embed_url: str,
    wait_ms: int = 4000,
    page_url: str = "",
    debug: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """임베드 플레이어 URL을 iframe wrapper 환경에서 열어 미디어 후보를 수집한다.

    - 직접 방문 시 frame-buster / 안티크롤러에 걸리는 문제 방지
    - 실제 브라우저와 동일한 iframe 환경을 제공하여 정상 스트림(jwplayer sources 등) 온전 추출
    """
    captured: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, str]] = []
    dom_items: list[dict[str, Any]] = []
    page = _own_page(ctx)

    # fuckadblock 감지 우회:
    # 1. fuckadblock.min.js 자체를 빈 스크립트로 차단
    # 2. embed 플레이어 HTML에 anti-fuckadblock 스크립트를 직접 주입 (cross-origin iframe은 add_init_script 적용 불가)
    _ANTI_FUCKADBLOCK_SNIPPET = (
        "<script>"
        "window.fuckAdBlock=window.blockAdBlock={"
        "onDetected:function(){return this;},"
        "onNotDetected:function(cb){if(typeof cb==='function')cb();return this;},"
        "setOption:function(){return this;},"
        "check:function(){return false;}"
        "};"
        "window.adsbygoogle=window.adsbygoogle||{loaded:true,push:function(){}};"
        "</script>"
    )

    _embed_host = _host(embed_url)

    def _route_embed_inject(route):
        """embed 플레이어 HTML에 anti-fuckadblock 스크립트를 주입한다."""
        req = route.request
        u = req.url
        url_host = _host(u)
        # fuckadblock 스크립트 자체 차단 (확장된 매칭)
        if "fuckadblock" in u.lower() or "fuckAdBlock" in u:
            route.fulfill(status=200, body="", content_type="text/javascript")
            return
        # iamcdn.net/player 경로의 모든 fuckadblock 관련 스크립트 차단
        if url_host == "iamcdn.net" and "fuckadblock" in u.lower():
            route.fulfill(status=200, body="", content_type="text/javascript")
            return
        # embed 플레이어 HTML에만 스크립트 주입 (다른 리소스는 패스)
        if url_host == _embed_host and req.resource_type == "document":
            try:
                resp = route.fetch()
                body = resp.body().decode("utf-8", errors="replace")
                # <head> 직후에 anti-fuckadblock 삽입 (첫 번째 자식으로)
                if "<head>" in body:
                    body = body.replace("<head>", "<head>" + _ANTI_FUCKADBLOCK_SNIPPET, 1)
                elif "<html>" in body:
                    body = body.replace("<html>", "<html>" + _ANTI_FUCKADBLOCK_SNIPPET, 1)
                else:
                    body = _ANTI_FUCKADBLOCK_SNIPPET + body
                route.fulfill(
                    status=resp.status,
                    headers=dict(resp.headers),
                    body=body.encode("utf-8"),
                )
                return
            except Exception:
                pass
        route.continue_()

    try:
        page.route("**", _route_embed_inject)
    except Exception as e:
        log_event("media_discovery", "route_failed", f"embed 라우팅 설정 실패: {e}", level="WARNING", frame_url=embed_url)

    diag = get_diagnostics()
    player_id = diag.new_player_id()
    if debug is not None:
        debug["player_id"] = player_id
    attach_page_diagnostics(page, f"embed:{player_id}")
    jw_info: dict[str, Any] = {"embed_url": embed_url, "page_url": page_url, "kind": "embed_direct"}
    t_start = time.monotonic()

    # 진단: 최근 요청/응답 요약 (report debug 용, 상한 있음)
    all_requests: list[dict[str, Any]] = []
    all_responses: list[dict[str, Any]] = []

    def on_req(req):
        try:
            all_requests.append({"url": req.url, "method": req.method, "resource_type": req.resource_type})
        except Exception:
            pass

    def on_resp(resp):
        try:
            all_responses.append({"url": resp.url, "status": resp.status,
                                  "content_type": resp.headers.get("content-type", "")})
        except Exception:
            pass
        try:
            c = _media_candidate(resp, embed_url, None, trace)
        except Exception as e:
            log_exception("media_discovery", e, frame_url=embed_url)
            return
        if c and c["url"] not in captured and len(captured) < MAX_CAPTURED:
            c.update(embed_url=embed_url, player_id=player_id, play_order=len(captured) + 1)
            captured[c["url"]] = c
            log_event("media_discovery", "candidate_detected", "네트워크에서 미디어 후보 발견", level="DEBUG",
                      frame_url=c.get("frame_url"), player_id=player_id, url=c["url"],
                      content_type=c.get("content_type"), status=c.get("status"))

    page.on("request", on_req)
    page.on("response", on_resp)

    wrapper_html = (
        '<!DOCTYPE html><html><head></head><body style="margin:0;padding:0;">'
        f'<iframe id="player_frame" src="{html.escape(embed_url, quote=True)}" width="1280" height="720"></iframe>'
        "</body></html>"
    )

    # jwplayer 초기화 완료를 대기하는 JS (최대 N회 폴링)
    _JW_READY_JS = """() => {
        try {
            if (typeof jwplayer !== 'function') return null;
            const jw = jwplayer();
            if (!jw || typeof jw.getPlaylist !== 'function') return null;
            const pl = jw.getPlaylist();
            if (!pl || !pl[0]) return null;
            const srcs = pl[0].allSources || pl[0].sources || [];
            return srcs.length > 0 ? srcs.map(s => s.file || s) : null;
        } catch(e) { return null; }
    }"""

    # jwplayer.setup() 직접 호출 시도 JS
    _JW_SETUP_JS = """() => {
        try {
            if (typeof jwplayer !== 'function') return 'no_jwplayer';
            const jw = jwplayer();
            if (!jw) return 'no_instance';
            // setup이 이미 완료되었는지 확인
            if (jw.getPlaylist && jw.getPlaylist() && jw.getPlaylist().length > 0) {
                return 'already_ready';
            }
            // setup 직접 호출 (player element가 있는 경우)
            const playerEl = document.querySelector('#player, [data-jwplayer], .jwplayer');
            if (playerEl && jw.setup) {
                jw.setup({
                    width: 1280,
                    height: 720,
                    primary: 'html5',
                    sources: [{file: ''}]
                });
                return 'setup_called';
            }
            return 'no_player_el';
        } catch(e) { return 'error:' + e.message; }
    }"""

    try:
        page.set_content(wrapper_html)
        # 플레이어 스크립트 로딩 + jwplayer 초기화를 위해 충분히 대기
        page.wait_for_timeout(5000)

        # jwplayer 초기화 완료될 때까지 폴링. 5회(약 10초 시점) 지나도 어느 프레임에도 jwplayer 자체가 없으면
        # JW 플레이어가 아닌 것으로 보고 기다리지 않는다 (JW 가 있으면 기존처럼 최대 15회까지 대기).
        jw_ready = False
        jw_present = False
        for _poll in range(15):
            for fr in page.frames:
                try:
                    if not jw_present and fr.evaluate("() => typeof jwplayer === 'function'"):
                        jw_present = True
                    srcs = fr.evaluate(_JW_READY_JS)
                    if srcs:
                        log(f"    [JW READY] {fr.url[:60]} → {len(srcs)}개 소스 ({_poll+1}번째 폴링)")
                        jw_ready = True
                        break
                except Exception:
                    pass
            if jw_ready:
                break
            if _poll >= 4 and not jw_present:
                break
            page.wait_for_timeout(1000)

        # jwplayer가 존재하지만 소스가 없는 경우: setup 직접 시도
        if not jw_ready:
            for fr in page.frames:
                try:
                    setup_result = fr.evaluate(_JW_SETUP_JS)
                    if setup_result == 'setup_called':
                        log(f"    [JW SETUP] jwplayer.setup() 직접 호출 성공 ({fr.url[:60]})")
                        # setup 후 3초 더 대기 후 재확인
                        page.wait_for_timeout(3000)
                        for _poll2 in range(5):
                            try:
                                srcs = fr.evaluate(_JW_READY_JS)
                                if srcs:
                                    log(f"    [JW READY] setup 후 소스 확인 ({len(srcs)}개)")
                                    jw_ready = True
                                    break
                            except Exception:
                                pass
                            page.wait_for_timeout(1000)
                        if jw_ready:
                            break
                    elif setup_result == 'already_ready':
                        log(f"    [JW READY] setup 이미 완료된 상태 ({fr.url[:60]})")
                        jw_ready = True
                        break
                except Exception:
                    pass

        if not jw_ready:
            if jw_present:
                log("    [JW WAIT] jwplayer 초기화 미확인 (오버레이 클릭 시도)")
            else:
                log("    [JW] jwplayer 없음 → 일반 플레이어로 진행")
        jw_info.update(jw_ready=jw_ready, jw_present=jw_present, init_wait_sec=round(time.monotonic() - t_start, 1))
        if jw_ready or jw_present:
            log_event("player_init", "jw_ready" if jw_ready else "jw_not_ready",
                      "JWPlayer 초기화 확인" if jw_ready else "JWPlayer 는 있으나 초기화 미확인",
                      level="INFO" if jw_ready else "WARNING", player_id=player_id, frame_url=embed_url,
                      waited_sec=jw_info["init_wait_sec"])
        else:
            log_event("player_init", "jw_absent", "JWPlayer 없음(일반 플레이어로 진행)", level="INFO",
                      player_id=player_id, frame_url=embed_url, waited_sec=jw_info["init_wait_sec"])

        # 1. 로드 직후 원본 DOM / jwplayer 소스 우선 수집
        for fr_items in _eval_frames(page, _DOM_MEDIA_JS, None):
            if fr_items:
                dom_items.extend(fr_items)

        # 2. 재생 시도 (오버레이 해제 및 클릭)
        stream_ok = _play_until_stream(page, captured, wait_ms, click_hosts=None, video_hosts=None)
        jw_info["play_result"] = "stream_found" if stream_ok else "no_stream_after_play"

        # 3. 재생 후 추가/교체된 DOM 미디어 수집
        for fr_items in _eval_frames(page, _DOM_MEDIA_JS, None):
            if fr_items:
                dom_items.extend(fr_items)

        if debug is not None:
            _fill_page_debug(page, debug)
        jw_info["frames"] = [fr.url for fr in page.frames]
        diag.note_frames(jw_info["frames"])
    except Exception as e:
        log(f"  [Embed Extract Warning] {embed_url}: {str(e).splitlines()[0]}")
        log_exception("media_discovery", e, frame_url=embed_url, player_id=player_id)
        jw_info["error"] = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
    finally:
        try:
            page.remove_listener("response", on_resp)
        except Exception:
            pass
        if debug is not None:
            debug["rejected"] = trace[:12]
            debug["captured_urls"] = sorted(captured.keys())
            debug["debug_requests"] = all_requests[-50:] if all_requests else []
            debug["debug_responses"] = [
                {"url": r.get("url"), "status": r.get("status"), "ctype": r.get("content_type")}
                for r in all_responses[-50:]
            ]
            debug["log_popups"] = _popup_count()
        jw_info.update(captured=len(captured), dom_items=len(dom_items), rejected=len(trace),
                       requests=len(all_requests), responses=len(all_responses), popups_closed=_popup_count(),
                       elapsed_sec=round(time.monotonic() - t_start, 1))
        diag.player(player_id, **jw_info)
        try:
            page.close()
        except Exception:
            pass

    cands: dict[str, dict[str, Any]] = {}
    for it in dom_items:
        u, dur = it.get("src"), it.get("duration")
        if not u:
            continue
        c = cands.setdefault(u, {
            "url": u,
            "content_type": "application/x-mpegurl" if ".m3u8" in u else "video/mp4",
            "referer": embed_url,
            "embed_url": embed_url,
            "player_id": it.get("player_id") or player_id,
            "source": "dom",
            "first_seen": now_iso(),
        })
        if it.get("from_playlist"):
            c.update(from_playlist=True, playlist_index=it.get("playlist_index"))
        if dur is not None and dur > 0:
            c["dom_duration"] = float(dur)

    # 네트워크 응답에서 캡처된 미디어 병합 (DOM 후보와 같은 URL 이면 네트워크 메타정보를 보강)
    for u, c in captured.items():
        if u in cands:
            for k in ("status", "content_length", "frame_url", "request_headers", "play_order"):
                if c.get(k) is not None:
                    cands[u].setdefault(k, c[k])
        else:
            cands[u] = c

    out = _prune_segments(list(cands.values()))
    if out:
        cookies = _collect_cookies(ctx, [c["url"] for c in out] + [embed_url])
        for c in out:
            c["cookies"] = cookies
    return out


# 자동화 탐지 완화 (webdriver 외에 헤드리스에서 흔히 비어 있는 속성들)
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
try {
    const q = navigator.permissions && navigator.permissions.query;
    if (q) navigator.permissions.query = (p) => p && p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission }) : q.call(navigator.permissions, p);
} catch (e) {}
"""


def _launch_browser(p, headless: bool, proxy: str | None):
    """headless 일 때 '새 headless 모드'(full Chromium)를 우선 사용 - 구형 headless-shell 보다 봇 탐지에 덜 걸림.
    실패하면 기존 방식으로 실행한다."""
    kwargs = get_browser_launch_kwargs(headless=headless, proxy=proxy)
    if headless and "channel" not in kwargs and "executable_path" not in kwargs:
        try:
            return p.chromium.launch(**{**kwargs, "channel": "chromium"})
        except Exception:
            pass
    return p.chromium.launch(**kwargs)


MAX_VIDEOS_PER_PAGE = 50
DISCOVER_BUDGET_SEC = 360      # 글 1개의 탐색(임베드 순회) 시간 예산. 광고 iframe 이 많은 글에서 끝없이 늘어지는 것을 막는다


def _site_key(host: str) -> str:
    parts = [x for x in (host or "").lower().split(".") if x]
    if len(parts) >= 3 and parts[-2] in {"co", "com", "or", "ne", "go", "ac", "org", "net"} and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (host or "").lower()


def _page_hijacked(page, page_url: str) -> bool:
    """글 페이지가 광고 스크립트에 의해 다른 사이트로 이동했는지 (같은 사이트의 미러/서브도메인은 정상)."""
    try:
        now = _host(page.url)
    except Exception:
        return False
    if not now or _site_key(now) == _site_key(_host(page_url)):
        return False
    log(f"  [하이재킹] 글 페이지가 다른 사이트로 이동됨({now}) → 해당 페이지에서의 재생/클릭을 생략")
    log_event("navigation", "page_hijacked", f"글 페이지가 다른 사이트로 이동됨: {now}", level="WARNING",
              page_url=page_url, final_host=now)
    return True

# 페이지 내 모든 임베드 플레이어 URL을 DOM 순서대로 수집
_EMBED_JS = """() => {
    const urls = [];
    const add = (u) => { if (u && /^https?:/i.test(u) && !urls.includes(u)) urls.push(u); };
    document.querySelectorAll('script.fp-embed').forEach(el => {
        try {
            const d = JSON.parse(el.textContent || el.innerText);
            const m = d.html && d.html.match(/src=["']([^"']+)["']/);
            if (m) add(m[1]);
        } catch (e) {}
    });
    document.querySelectorAll('iframe').forEach(i => {
        const u = i.src || i.getAttribute('data-src') || i.getAttribute('data-lazy-src') || '';
        if (/upns|tellmewhy|embed/i.test(u)) add(u);
        else if (u && i.clientWidth >= 400 && i.clientHeight >= 200) add(u);   // 728x90, 300x250 등 광고 배너는 제외
    });
    return urls;
}"""


def _scroll_page(page) -> None:
    """지연 로딩(lazy-load)되는 플레이어를 모두 불러오기 위해 끝까지 스크롤."""
    try:
        for _ in range(4):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(500)
        page.evaluate("window.scrollTo(0, 0)")
    except Exception as e:
        if _is_closed_error(e):
            raise


def discover_videos(
    page_url: str,
    wait_ms: int = 6000,
    click_selector: str | None = None,
    proxy: str | None = None,
    headless: bool = True,
) -> list[dict[str, Any]]:
    """페이지 안의 모든 동영상을 DOM 순서대로 탐색한다.

    반환: [{"index": 1, "embed_url": str|None, "candidates": [stream_info, ...], "group_id", "low_confidence",
            "ad_excluded", "debug"}, ...]
    수집된 후보는 판별(MAIN/AD/PENDING)·그룹화를 거쳐 '영상 단위 항목'이 된다. 항목의 candidates 는 다운로드
    시도 순서이며, 광고로 판별된 후보는 포함되지 않는다(근거는 candidates.json 에 기록).
    본편을 못 찾은 임베드도 candidates=[] 로 포함한다 (인덱스가 고정되어 재시도/히스토리 키가 안정적).

    탐색 순서
      A) 임베드 URL 을 Referer 와 함께 직접 열어 플레이어 프레임의 요청만 수집
      B) A 로 본편(m3u8/mpd)을 못 얻으면, 글 페이지에서 직접 재생 버튼을 눌러
         '플레이어 iframe 에서 발생한 요청'만 수집 (도메인 잠금/클릭 2회 구조 대응)
      C) 임베드가 아예 없는 페이지: 클릭 후 네트워크/DOM 에서 탐색 (프레임 제한 없이 수집 후 판별)
    """
    active_player: dict[str, Any] = {
        "index": None,
        "frame": None,
    }
    found: dict[str, dict[str, Any]] = {}           # 모든 프레임의 미디어 (C 용)
    player_found: dict[str, dict[str, Any]] = {}    # 플레이어 프레임에서 발생한 미디어만 (B 용)
    player_hosts: set[str] = set()
    trace_b: list[dict[str, str]] = []              # B 에서 거절된 요청 (진단용)
    popups0 = _popup_count()
    # Playwright 동기 API 는 핸들러 안의 왕복 호출(all_headers 등) 중에 다른 응답 핸들러가 끼어들 수 있으므로
    # 순번은 핸들러 시작 시점에 확보한다 (len(dict)+1 은 경합한다).
    order_counter = itertools.count(1)

    def on_response(resp) -> None:
        url = resp.url
        path = urlparse(url).path.lower()
        ctype = (resp.headers.get("content-type") or "").lower()
        if not (path.endswith(_SEGMENT_EXT) or "mp2t" in ctype or "segment" in ctype):
            path_media = path.endswith((".m3u8", ".mpd", ".mp4"))
            is_media = path_media or any(t in ctype for t in MEDIA_TYPES)
            if is_media and url not in found and len(found) < MAX_CAPTURED * 3:
                order = next(order_counter)
                found[url] = {"url": url, "content_type": ctype, "referer": page_url,
                              "status": resp.status, "content_length": _resp_total_length(resp),
                              "first_seen": now_iso(), "play_order": order, "source": "network",
                              "request_headers": _safe_request_headers(resp)}
        if player_hosts:
            try:
                response_frame = resp.frame
            except Exception:
                response_frame = None

            target_frame = active_player["frame"]
            group_index = active_player["index"]

            # B 경로에서는 현재 재생 중인 플레이어와 연결된 응답만 받는다.
            if group_index is not None:
                is_main = False
                try:
                    is_main = response_frame.parent_frame is None
                except Exception:
                    pass

                belongs_to_active = (
                    is_main
                    or (
                        target_frame is not None
                        and response_frame is not None
                        and _is_descendant_frame(response_frame, target_frame)
                    )
                )
                if not belongs_to_active:
                    return

                try:
                    c = _media_candidate(
                        resp,
                        page_url,
                        player_hosts,
                        trace_b,
                    )
                except Exception as e:
                    log_exception("media_discovery", e, page_url=page_url)
                    return

                if c:
                    c["inpage_group_index"] = group_index
                    if c["url"] not in player_found and len(player_found) < MAX_CAPTURED:
                        c["play_order"] = len(player_found) + 1
                        player_found[c["url"]] = c

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    page_closed = False
    embed_urls: list[str] = []
    groups: list[dict[str, Any]] = []
    diag = get_diagnostics()
    diag.note_page(page_url)
    budget_sec = int(os.environ.get("VP_DISCOVER_BUDGET", DISCOVER_BUDGET_SEC))
    deadline = time.monotonic() + budget_sec

    with sync_playwright() as p:
        browser = _launch_browser(p, headless, proxy)
        ctx = browser.new_context(**get_context_kwargs(ua))
        ctx.add_init_script(_STEALTH_JS)

        # 광고 팝업: 우리가 만든 페이지가 아닌 새 탭은 모두 즉시 닫기
        ctx.on("page", _close_stray_page)

        page = _own_page(ctx)
        attach_page_diagnostics(page, "main")
        page.on("response", on_response)

        try:
            resp = page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            browser.close()
            log_event("navigation", "goto_failed", f"페이지 접속 실패: {str(e).splitlines()[0]}", level="WARNING", page_url=page_url)
            raise DiscoverNavigationError(str(e).splitlines()[0]) from e
        status = resp.status if resp else None
        log_event("navigation", "page_loaded", f"페이지 로드 (HTTP {status})", page_url=page_url, status=status, final_url=page.url)
        if status is not None and status >= 400:
            browser.close()
            log_event("navigation", "http_error", f"페이지 HTTP {status}", level="WARNING", page_url=page_url, status=status)
            raise DiscoverNavigationError(f"HTTP {status}", status=status)

        # 1. 페이지 내 모든 임베드 플레이어 수집 (스크롤로 lazy-load 유도)
        try:
            page.wait_for_timeout(1500)
            _scroll_page(page)
            embed_urls = [u for u in page.evaluate(_EMBED_JS) if not _is_ad_embed(u)]
            embed_urls = embed_urls[:MAX_VIDEOS_PER_PAGE]
            diag.note_frames([fr.url for fr in page.frames])
        except Exception as e:
            if not _is_closed_error(e):
                browser.close()
                raise DiscoverNavigationError(str(e).splitlines()[0]) from e
            page_closed = True
        if not embed_urls:
            log_event("media_discovery", "no_embed", "임베드 플레이어(iframe)를 찾지 못함",
                      level="INFO", page_url=page_url, frames=len(page.frames) if not page_closed else 0,
                      page_closed=page_closed)

        if embed_urls:
            player_hosts.update(h for h in (_host(u) for u in embed_urls) if h)
            log(f"  → 임베드 플레이어 {len(embed_urls)}개 감지")
            log_event("media_discovery", "embeds_detected", f"임베드 플레이어 {len(embed_urls)}개 감지",
                      page_url=page_url, embeds=[str(u)[:150] for u in embed_urls])

            # ---- A) 임베드 직접 열기 (페이지당 시간 예산 안에서; 초과분은 빈 항목으로 남겨 인덱스를 고정)
            for i, eu in enumerate(embed_urls, 1):
                if time.monotonic() > deadline:
                    log_event("media_discovery", "budget_exceeded", f"탐색 시간 예산({budget_sec}s) 초과: 임베드 {i}번째부터 생략",
                              level="WARNING", page_url=page_url, skipped=len(embed_urls) - i + 1)
                    log(f"  [시간 초과] 탐색 예산 {budget_sec}초 초과 → 임베드 {i}~{len(embed_urls)}번 생략")
                    for j in range(i, len(embed_urls) + 1):
                        groups.append({"index": j, "embed_url": embed_urls[j - 1], "candidates": [],
                                       "debug": {"headless": headless, "skipped": "discover_budget"}})
                    break
                if len(embed_urls) > 1:
                    log(f"    [{i}/{len(embed_urls)}] 스트림 추출: {eu[:90]}")
                dbg: dict[str, Any] = {"headless": headless}
                infos = _extract_embed_stream(ctx, eu, page_url=page_url, debug=dbg)
                for info in infos:
                    info["user_agent"] = ua
                    info["page_url"] = page_url
                groups.append({"index": i, "embed_url": eu, "candidates": infos, "debug": dbg})

            # ---- B) 본편(m3u8/mpd/충분한 길이의 mp4)을 못 얻은 플레이어: 글 페이지에서 직접 재생
            need_b = [g for g in groups if not g["debug"].get("skipped") and not _has_real_stream(g["candidates"])]
            if need_b and not page_closed and _page_hijacked(page, page_url):
                need_b = []     # 광고가 글 페이지를 다른 사이트로 옮겼다: 그 사이트에서는 클릭/재생하지 않는다
            if need_b and not page_closed and time.monotonic() > deadline:
                log_event("media_discovery", "budget_exceeded", "탐색 시간 예산 초과: 글 페이지 직접 재생(B) 생략", level="WARNING",
                          page_url=page_url)
                need_b = []

            if need_b and not page_closed:
                log("  → 임베드 직접 열기로 본편을 못 찾음: 글 페이지에서 직접 재생을 시도합니다")
                log_event(
                    "media_discovery",
                    "inpage_play",
                    "글 페이지에서 직접 재생 시도(B)",
                    page_url=page_url,
                    players=len(need_b),
                )

                for g in need_b:
                    embed_url = g.get("embed_url")
                    target_frame, click_point = _find_embed_frame(
                        page,
                        embed_url or "",
                        debug=g["debug"],
                    )

                    if target_frame is None:
                        match_info = g["debug"].get("iframe_match", {})
                        g["debug"]["inpage"] = {
                            "player_found": 0,
                            "rejected": [],
                            "reason": match_info.get("status", "target_embed_frame_not_found"),
                            "iframe_match": match_info,
                        }
                        log_event(
                            "media_discovery",
                            "player_frame_unresolved",
                            "임베드 URL과 연결되는 iframe을 하나로 특정하지 못함",
                            level="WARNING",
                            page_url=page_url,
                            frame_url=embed_url,
                            iframe_match=g["debug"].get("iframe_match"),
                        )

                        # 기존 continue 유지
                        continue                    
                    
                    active_player["index"] = g["index"]
                    active_player["frame"] = target_frame
                    before = set(player_found)

                    try:
                        _play_until_stream(
                            page,
                            player_found,
                            wait_ms,
                            target_frame=target_frame,
                            click_point=click_point,
                        )
                    except Exception as e:
                        if _is_closed_error(e):
                            page_closed = True
                        else:
                            log(f"  [Play Warning] {str(e).splitlines()[0]}")
                            log_exception(
                                "media_discovery",
                                e,
                                page_url=page_url,
                            )
                    finally:
                        active_player["index"] = None
                        active_player["frame"] = None

                    if page_closed:
                        break

                    mine = [
                        c for url, c in player_found.items()
                        if url not in before
                        and c.get("inpage_group_index") == g["index"]
                    ]
                    mine = _prune_segments(mine)

                    try:
                        frames_now = [fr.url[:100] for fr in page.frames]
                    except Exception:
                        frames_now = []

                    g["debug"]["inpage"] = {
                        "player_found": len(mine),
                        "rejected": trace_b[:12],
                        "frames": frames_now,
                    }

                    if mine:
                        cookies = _collect_cookies(
                            ctx,
                            [c["url"] for c in mine] + [g["embed_url"]],
                        )
                        have = {c["url"] for c in g["candidates"]}

                        for c in mine:
                            c.update(
                                referer=g["embed_url"],
                                user_agent=ua,
                                page_url=page_url,
                                cookies=cookies,
                                embed_url=g["embed_url"],
                                source="network_inpage",
                            )

                        g["candidates"] += [
                            c for c in mine if c["url"] not in have
                        ]

                try:
                    frames_now = [fr.url[:100] for fr in page.frames]
                except Exception:
                    frames_now = []
                diag.note_frames(frames_now)

            try:
                browser.close()
            except Exception:
                pass
            for g in groups:
                g["debug"]["popups_closed"] = _popup_count() - popups0
            # 임베드가 있으면 광고가 섞인 C 경로로 내려가지 않는다
            return _finalize_items(groups, page_url=page_url, proxy=proxy, player_hosts=player_hosts)

        # ---- C) 임베드 없음: 클릭 후 DOM/네트워크에서 탐색
        if click_selector and not page_closed:
            try:
                page.click(click_selector, timeout=5000, force=True)
            except Exception:
                for f in page.frames:
                    try:
                        f.click(click_selector, timeout=2000, force=True)
                        break
                    except Exception:
                        pass

        if not page_closed:
            try:
                _play_until_stream(page, found, wait_ms)
            except Exception as e:
                if not _is_closed_error(e):
                    raise
                page_closed = True

        # Inspect DOM videos / JW playlist in main page and all child frames (iframes)
        try:
            for fr_items in _eval_frames(page, _DOM_MEDIA_JS, None):
                for it in fr_items or []:
                    src = it.get("src")
                    if not src or str(src).startswith("blob:"):
                        continue
                    c = found.setdefault(src, {"url": src, "content_type": "dom/video", "referer": page_url,
                                               "first_seen": now_iso(), "source": "dom"})
                    if it.get("duration"):
                        c["dom_duration"] = float(it["duration"])
                    if it.get("from_playlist"):
                        c.update(from_playlist=True, playlist_index=it.get("playlist_index"),
                                 player_id=it.get("player_id"))
        except Exception as e:
            if _is_closed_error(e):
                page_closed = True
            else:
                log_exception("media_discovery", e, page_url=page_url)
        diag.note_frames([fr.url for fr in page.frames] if not page_closed else [])

        try:
            ua = page.evaluate("navigator.userAgent") or ua
        except Exception:
            pass

        try:
            browser.close()
        except Exception:
            pass

    if page_closed and not found:
        # 광고 리다이렉트/브라우저 크래시 등으로 페이지가 닫힘 → 새 브라우저로 재시도
        raise DiscoverNavigationError("page/browser closed before media was found")

    if not found:
        log_event("media_discovery", "no_media_found", "미디어 후보 없음(임베드 없음, 네트워크/DOM 모두 비어 있음)",
                  level="WARNING", page_url=page_url, responses_seen=len(found))
        return []
    pretty = _prune_segments(list(found.values()))
    for info in pretty:
        info["user_agent"] = ua
        info["page_url"] = page_url
    return _finalize_items([{"index": 1, "embed_url": None, "candidates": pretty, "debug": {"headless": headless}}],
                           page_url=page_url, proxy=proxy, player_hosts=set())


# ----------------------------------------------------------------- 수집 이후: 보강 → 판별 → 그룹화
MAX_ENRICH = 40


def _enrich_candidates(cands: list[dict[str, Any]], proxy: str | None) -> None:
    """판별에 필요한 메타정보(HLS 종류/길이, mp4 용량)를 보강한다. 실패해도 조용히 넘어가며 이벤트만 남긴다."""
    for c in cands[:MAX_ENRICH]:
        u = c["url"]
        path = urlparse(u).path.lower()
        try:
            if path.endswith(".m3u8") or "mpegurl" in (c.get("content_type") or ""):
                if not c.get("m3u8_kind"):
                    c["m3u8_kind"] = _probe_m3u8_kind(c, proxy=proxy)
                if c.get("duration") is None and c.get("m3u8_kind") != "unknown":
                    d = _m3u8_duration(c, proxy)
                    if d:
                        c["duration"] = d
            elif not path.endswith(".mpd") and c.get("content_length") is None:
                c["content_length"] = _get_content_length(
                    u, referer=c.get("referer", ""), user_agent=c.get("user_agent", ""), proxy=proxy)
        except Exception as e:
            log_event("classification", "enrich_failed", f"메타 보강 실패: {type(e).__name__}", level="DEBUG",
                      frame_url=u)


def _finalize_items(
    raw_groups: list[dict[str, Any]],
    *,
    page_url: str,
    proxy: str | None,
    player_hosts: set[str],
) -> list[dict[str, Any]]:
    """수집된 후보 전체를 판별(MAIN/AD/PENDING)하고 영상 단위로 그룹화해 처리 항목 목록을 만든다.

    - 광고로 판별된 후보도 제거 근거와 함께 candidates.json 에 남는다.
    - 임베드마다 MAIN 그룹이 있으면 MAIN 그룹 + 광고 근거가 전혀 없는 PENDING 그룹을 항목으로 만든다.
      MAIN 이 없으면 PENDING 그룹을 모두 항목으로 만들고 low_confidence 로 표시한다.
    - 항목의 candidates 는 다운로드 시도 순서로 정렬되어 있다. 항목 index 는 1부터 연속.
    """
    diag = get_diagnostics()
    items: list[dict[str, Any]] = []
    for rg in raw_groups:
        cands: list[dict[str, Any]] = rg.get("candidates") or []
        try:
            with diag.stage("classification"):
                _enrich_candidates(cands, proxy)
                for c in cands:
                    c.setdefault("candidate_id", diag.new_candidate_id())
                    res = classify_candidate(c, blacklist=blacklist, player_hosts=player_hosts)
                    c.update(res)
                    diag.candidate(c, page_url=page_url)
                    log_event("classification", "candidate_classified",
                              f"{res['classification']} (main={res['main_score']}, ad={res['ad_score']})",
                              level="DEBUG", candidate_id=c["candidate_id"], frame_url=c["url"], rules=res["rules"])
                    if strong_ad_rules(res):
                        blacklist.observe_ad(c["url"], reason="ad_path_token", page_url=page_url, strong=True)
                ads = [c for c in cands if c["classification"] == "AD"]
                rest = [c for c in cands if c["classification"] != "AD"]
                vgroups = group_candidates(rest)
                for g in vgroups:
                    rep = g["representative"]["candidate_id"]
                    for m in g["members"]:
                        diag.update_candidate(m["candidate_id"], group_id=g["group_id"],
                                              duplicate_of=rep if m["candidate_id"] != rep else None)
                has_main = any(g["classification"] == "MAIN" for g in vgroups)
                kept = [g for g in vgroups if g["classification"] == "MAIN"] if has_main else list(vgroups)
                if has_main:
                    for g in vgroups:
                        if g["classification"] == "PENDING":
                            if g["ad_score"] == 0:
                                kept.append(g)
                            else:
                                for m in g["members"]:
                                    diag.update_candidate(m["candidate_id"], excluded_reason="pending_with_ad_evidence")
                kept.sort(key=lambda g: g["group_id"])
        except Exception as e:
            # 판별 계층의 오류가 탐색 결과를 잃게 하지 않는다: 후보를 전부 PENDING 으로 두고 그대로 전달
            log_exception("classification", e, page_url=page_url)
            for c in cands:
                c.setdefault("classification", "PENDING")
            items.append({"index": len(items) + 1, "embed_url": rg.get("embed_url"), "candidates": cands,
                          "group_id": None, "low_confidence": True, "ad_excluded": 0, "debug": rg.get("debug")})
            continue

        counts = {"candidates": len(cands), "main": sum(c["classification"] == "MAIN" for c in cands),
                  "ad": len(ads), "pending": sum(c["classification"] == "PENDING" for c in cands), "groups": len(kept)}
        log_event("classification", "embed_classified",
                  f"후보 {counts['candidates']}개 → MAIN {counts['main']} / AD {counts['ad']} / PENDING {counts['pending']}, "
                  f"영상 그룹 {len(kept)}개", frame_url=rg.get("embed_url"), page_url=page_url, **counts)
        if not kept:
            reason = "all_candidates_ad" if ads else "no_candidates"
            log_event("classification", "no_main_found" if ads else "no_media_found",
                      "본편으로 볼 후보가 없음: " + ("모든 후보가 광고로 판별됨" if ads else "후보 자체가 없음"),
                      level="WARNING", frame_url=rg.get("embed_url"), page_url=page_url, reason=reason, **counts)
            items.append({"index": len(items) + 1, "embed_url": rg.get("embed_url"), "candidates": [],
                          "group_id": None, "low_confidence": False, "ad_excluded": len(ads), "debug": rg.get("debug")})
            continue
        for g in kept:
            items.append({
                "index": len(items) + 1, "embed_url": rg.get("embed_url"), "candidates": g["attempts"],
                "group_id": g["group_id"], "group_reasons": g["reasons"], "members": len(g["members"]),
                "low_confidence": g["classification"] != "MAIN", "ad_excluded": len(ads), "debug": rg.get("debug"),
            })
    return items


def discover(
    page_url: str,
    wait_ms: int = 6000,
    click_selector: str | None = None,
    proxy: str | None = None,
    headless: bool = True,
) -> list[dict[str, Any]]:
    """하위 호환: 모든 동영상의 후보를 평탄화해서 반환."""
    groups = discover_videos(
        page_url, wait_ms=wait_ms, click_selector=click_selector,
        proxy=proxy, headless=headless,
    )
    return [c for g in groups for c in g["candidates"]]


def rank_candidates(
    candidates: list[dict[str, Any]],
    proxy: str | None = None,
) -> list[dict[str, Any]]:
    """다운로드 시도 순서로 정렬한 후보 목록 (--discover-only/select_best 용; 판별은 하지 않는다).

    m3u8(master → media → 기타) > mpd > mp4(화질/용량 큰 순). master 아래 하위 playlist 는 제외.
    앞 후보가 실패(403 등)하면 호출측이 다음 후보를 시도한다.
    """
    if not candidates:
        return []
    _enrich_candidates(candidates, proxy)
    return order_attempts(candidates)


def select_best(
    candidates: list[dict[str, Any]],
    proxy: str | None = None,
) -> dict[str, Any] | None:
    ranked = rank_candidates(candidates, proxy=proxy)
    return ranked[0] if ranked else None


# ----------------------------------------------------------------- duration probe
_EXTINF_RE = re.compile(r"#EXTINF:\s*([0-9]+(?:\.[0-9]+)?)")
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")


def _fetch_text(
    url: str,
    referer: str = "",
    user_agent: str = "",
    proxy: str | None = None,
    limit: int = 2_000_000,
    timeout: int = 15,
) -> str | None:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url,
        headers={
            "Referer": referer,
            "User-Agent": user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read(limit).decode("utf-8", errors="replace")
    except Exception:
        return None


def _m3u8_duration(info: dict[str, Any], proxy: str | None) -> float | None:
    """m3u8 플레이리스트의 #EXTINF 합계로 총 길이 계산 (master면 variant를 따라감)."""
    url = info["url"]
    ref, ua = info.get("referer", ""), info.get("user_agent", "")
    text = _fetch_text(url, ref, ua, proxy)
    if text is None:
        return None
    if "#EXT-X-STREAM-INF" in text:
        lines = [l.strip() for l in text.splitlines()]
        best_uri, best_bw = None, -1
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                m = _BANDWIDTH_RE.search(line)
                bw = int(m.group(1)) if m else 0
                for nxt in lines[i + 1:]:
                    if nxt and not nxt.startswith("#"):
                        if bw > best_bw:
                            best_uri, best_bw = nxt, bw
                        break
        if not best_uri:
            return None
        text = _fetch_text(urljoin(url, best_uri), ref, ua, proxy)
        if text is None:
            return None
    if "#EXT-X-ENDLIST" not in text:
        return None  # 라이브/불완전 → 판단 불가
    total = sum(float(x) for x in _EXTINF_RE.findall(text))
    return total if total > 0 else None


def _find_ffprobe() -> str | None:
    try:
        from media_tools import resolve_ffmpeg
        ff = resolve_ffmpeg()
        if ff:
            pth = pathlib.Path(str(ff))
            d = pth if pth.is_dir() else pth.parent
            for n in ("ffprobe.exe", "ffprobe"):
                if (d / n).is_file():
                    return str(d / n)
    except Exception:
        pass
    return shutil.which("ffprobe")


def _ffprobe_duration(info: dict[str, Any], proxy: str | None) -> float | None:
    exe = _find_ffprobe()
    if not exe:
        return None
    cmd = [
        exe, "-v", "error",
        "-protocol_whitelist", "http,https,tcp,tls,crypto",
        "-user_agent", info.get("user_agent") or "Mozilla/5.0",
        "-headers", f"Referer: {info.get('referer', '')}\r\n",
    ]
    if proxy and proxy.startswith("http"):
        cmd += ["-http_proxy", proxy]
    cmd += ["-show_entries", "format=duration", "-of", "default=nw=1:nk=1", "-i", info["url"]]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=25, stdin=subprocess.DEVNULL,
        )
        v = (r.stdout or "").strip().splitlines()
        return float(v[0]) if v and v[0] not in ("N/A", "") else None
    except Exception:
        return None


def probe_duration(info: dict[str, Any], proxy: str | None = None) -> float | None:
    """다운로드 전에 영상 길이(초)를 추정. 알 수 없으면 None (다운로드 후 재검사).
    탐색 단계에서 이미 구한 길이(duration)가 있으면 재요청하지 않는다."""
    cached = info.get("duration")
    if isinstance(cached, (int, float)) and cached > 0:
        return float(cached)
    path = urlparse(info["url"]).path.lower()
    try:
        if path.endswith(".m3u8") or "mpegurl" in (info.get("content_type") or ""):
            return _m3u8_duration(info, proxy)
        if path.endswith(".mpd"):
            return None
        return _ffprobe_duration(info, proxy)
    except Exception:
        return None
