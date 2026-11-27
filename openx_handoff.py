"""403 이후 OpenX download 페이지로 넘기는 인수.

임베드를 최상위로 열면 프레임 탈출로 리다이렉트되므로 iframe 안에서 플레이어를 준비한다.
about:blank 에 올리면 스토리지가 막혀 로더에서 멈추므로, 가능하면 글 페이지 origin 에 iframe 을 넣는다.
플레이어는 클릭 없이는 컨트롤바와 Download 버튼을 만들지 않는다. overlay/playback 클릭과
재생을 시도한 뒤 30초만 버튼을 본다. 그 안에 있으면 download 페이지를 열고, 없으면
다운로드 불가로 처리한다. 스트리밍 fallback 구현은 보존하되 기본적으로 비활성화한다.
광고 반복과 Download Now 는 사용자 Chrome 의 Tampermonkey 가 처리한다.
Turnstile 토큰은 주입하지 않는다.
"""
from __future__ import annotations

import html
import math
import re
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from diagnostics import attach_page_diagnostics, log_event, mask_text
from playback_capture import PlaybackCapture
from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright
from progress import log

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DOWNLOAD_PAGE_ORIGIN = "https://download.openx.xyz/"
# 재생 탭은 최대 5개를 유지하되, 새 플레이어 초기화는 하나씩 진행한다.
MAX_CONCURRENT_HANDOFFS = 5
# download 탭은 Tampermonkey용으로 닫지 않는다. 그래서 배치가 상한 없이 쌓지 않게 한다.
MAX_OPEN_DOWNLOAD_PAGES = 3
_HANDOFF_GUARD = threading.Lock()
_HANDOFF_ACTIVE = 0
_HANDOFF_COND = threading.Condition(_HANDOFF_GUARD)
_HANDOFF_INIT_LOCK = threading.Lock()
_WINDOW_MINIMIZED = False
_WINDOW_MINIMIZED_LOCK = threading.Lock()
DOWNLOAD_BUTTON_SELECTOR = '[aria-label="Download video"], [button="download"]'
# 이 시간 안에 버튼이 없으면 다운로드 불가로 처리한다.
BUTTON_DECISION_MS = 30_000
# 스트리밍 fallback: download 버튼이 없을 때 재생 스트림으로 영상을 수신·보존한다.
ENABLE_STREAMING_FALLBACK = True
# 스트리밍은 버튼 판정과 별개다. 본편 바이트가 끝날 때까지 두되, 무한 대기는 막는다.
CAPTURE_MAX_SECONDS = 3 * 60 * 60
DEFAULT_STREAMING_MAX_DURATION_SECONDS = 2 * 60 * 60
_PRIME_ROUNDS = 4
# 매초 확인하면 바쁜 렌더러에 evaluate 왕복이 쌓인다. 영상 길이는 몇 초 늦게 알아도 된다.
DURATION_CHECK_SECONDS = 5.0
# 직접 다운로드가 본편을 받는 동안에는 브라우저 재생이 필요 없다. 디코딩과 중복 수신을 멈춘다.
PAUSE_PLAYER_DURING_DIRECT = True
DIRECT_PAUSE_CHECK_SECONDS = 10.0
# 한 바퀴가 이보다 오래 걸리면 Chrome/CDP 가 밀린 것이다. 원인 측정을 위해 기록한다.
LOOP_STALL_WARN_SECONDS = 10.0
# 재생 시도 뒤 이 시간 안에 플레이어가 만들어지지 않으면 로더에서 멈춘 것으로 본다.
PLAYER_INIT_WAIT_SECONDS = 12.0
# 멈춘 플레이어를 되살릴 때 지우는 저장소. 쿠키는 지우지 않는다.
PLAYER_RESET_STORAGE_TYPES = "service_workers,indexeddb,cache_storage,local_storage"
# 플레이어 프레임의 초기화 상태. 값을 읽다 난 오류는 'ERR:이름' 으로 남겨 저장소 차단 여부를 알 수 있게 한다.
_PLAYER_STATE_JS = r"""() => {
    const out = {};
    const safe = (key, fn) => {
        try { out[key] = fn(); } catch (e) { out[key] = 'ERR:' + String((e && e.name) || e).slice(0, 60); }
    };
    safe('ready_state', () => document.readyState);
    safe('visibility', () => document.visibilityState);
    safe('loader', () => !!document.querySelector('#player .loader'));
    safe('overlay', () => !!document.getElementById('overlay'));
    safe('jw_dom', () => !!document.querySelector('.jwplayer'));
    safe('jw_lib', () => typeof jwplayer === 'function');
    safe('sotrym', () => typeof window.SoTrym === 'function');
    safe('jw', () => {
        if (typeof jwplayer !== 'function') return null;
        const player = jwplayer();
        if (!player || typeof player.getState !== 'function') return null;
        const list = typeof player.getPlaylist === 'function' ? player.getPlaylist() : null;
        return {
            state: String(player.getState() || ''),
            playlist: list ? list.length : 0,
            position: typeof player.getPosition === 'function' ? Math.round(player.getPosition() || 0) : null,
        };
    });
    safe('videos', () => Array.from(document.querySelectorAll('video')).slice(0, 4).map(v => ({
        ready: v.readyState, network: v.networkState, paused: v.paused,
        time: Math.round(v.currentTime || 0), error: v.error ? v.error.code : 0,
        has_src: !!(v.currentSrc || v.src),
    })));
    safe('notice', () => {
        const box = document.querySelector('iframe.notify');
        if (!box) return '';
        let text = '';
        try { text = (box.contentDocument && box.contentDocument.body && box.contentDocument.body.innerText) || ''; } catch (e) {}
        return (text || 'notify').replace(/\s+/g, ' ').slice(0, 300);
    });
    safe('body_text', () => ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').slice(0, 200));
    safe('devtool_flag', () => sessionStorage.getItem('devtool') || '');
    safe('local_storage', () => {
        localStorage.setItem('__vp_probe', '1');
        localStorage.removeItem('__vp_probe');
        return 'ok';
    });
    safe('sw_controller', () => {
        if (!('serviceWorker' in navigator)) return 'unsupported';
        const worker = navigator.serviceWorker.controller;
        return worker ? worker.scriptURL.split('/').pop() + ':' + worker.state : 'none';
    });
    return out;
}"""
# 서비스워커 등록과 IndexedDB 상태. 응답이 없는 호출은 2초 뒤 'timeout' 으로 남긴다.
# IndexedDB 는 이미 있는 DB 만 버전 없이 열어 보고 바로 닫는다. 'timeout' 이면 다른 열기 요청 뒤에서 대기 중이다.
_PLAYER_STORAGE_JS = r"""async () => {
    const out = {};
    const LATE = '__timeout__';
    const timed = (promise, ms) => Promise.race([promise, new Promise(done => setTimeout(() => done(LATE), ms))]);
    const fail = (e) => 'ERR:' + String((e && e.name) || e).slice(0, 60) + ': ' + String((e && e.message) || '').slice(0, 120);
    const worker = (w) => w ? w.scriptURL.split('/').pop() + ':' + w.state : '';
    try {
        if (!('serviceWorker' in navigator)) out.sw_registrations = 'unsupported';
        else {
            const regs = await timed(navigator.serviceWorker.getRegistrations(), 2000);
            out.sw_registrations = regs === LATE ? 'timeout' : regs.map(r => ({
                scope: r.scope, active: worker(r.active), waiting: worker(r.waiting), installing: worker(r.installing),
            }));
        }
    } catch (e) { out.sw_registrations = fail(e); }
    const openProbe = (name) => new Promise(done => {
        let req;
        try { req = indexedDB.open(name); } catch (e) { return done(fail(e)); }
        req.onupgradeneeded = () => { try { req.transaction.abort(); } catch (e) {} done('missing'); };
        req.onsuccess = () => {
            const db = req.result;
            const info = 'ok v' + db.version + ' stores=' + Array.from(db.objectStoreNames).join(',');
            db.close();
            done(info);
        };
        req.onerror = () => done('ERR:' + String((req.error && req.error.name) || 'error'));
        req.onblocked = () => done('blocked');
    });
    try {
        if (!('indexedDB' in window)) out.idb = 'unsupported';
        else if (typeof indexedDB.databases !== 'function') out.idb = 'no_databases_api';
        else {
            const dbs = await timed(indexedDB.databases(), 2000);
            if (dbs === LATE) out.idb = 'timeout';
            else {
                out.idb = dbs.map(d => ({ name: d.name, version: d.version }));
                out.idb_open = {};
                for (const d of dbs.slice(0, 3)) {
                    const opened = await timed(openProbe(d.name), 2000);
                    out.idb_open[d.name] = opened === LATE ? 'timeout' : opened;
                }
            }
        }
    } catch (e) { out.idb = fail(e); }
    try {
        if (window.caches) {
            const keys = await timed(caches.keys(), 2000);
            out.caches = keys === LATE ? 'timeout' : keys.length;
        }
    } catch (e) { out.caches = fail(e); }
    return out;
}"""
# CDP 로 지우지 못했을 때만 쓴다. 열려 있는 연결 때문에 삭제가 밀리면 프레임을 다시 불러온 뒤 진행된다.
_CLEAR_PLAYER_STORAGE_JS = r"""async () => {
    const out = { sw: 0, idb: [], caches: 0, local: false, errors: [] };
    const LATE = '__timeout__';
    const timed = (promise, ms) => Promise.race([promise, new Promise(done => setTimeout(() => done(LATE), ms))]);
    const note = (step, e) => out.errors.push(step + ':' + String((e && e.name) || e).slice(0, 60));
    try {
        const regs = await timed(navigator.serviceWorker.getRegistrations(), 2000);
        if (Array.isArray(regs)) {
            for (const reg of regs) {
                if (await timed(reg.unregister(), 2000) === true) out.sw++;
            }
        }
    } catch (e) { note('sw', e); }
    try {
        const dbs = await timed(indexedDB.databases(), 2000);
        if (Array.isArray(dbs)) {
            for (const d of dbs) {
                const result = await timed(new Promise(done => {
                    const req = indexedDB.deleteDatabase(d.name);
                    req.onsuccess = () => done('deleted');
                    req.onerror = () => done('error');
                    req.onblocked = () => done('blocked');
                }), 2000);
                out.idb.push(d.name + ':' + (result === LATE ? 'timeout' : result));
            }
        }
    } catch (e) { note('idb', e); }
    try {
        const keys = await timed(caches.keys(), 2000);
        if (Array.isArray(keys)) {
            for (const key of keys) {
                if (await caches.delete(key)) out.caches++;
            }
        }
    } catch (e) { note('caches', e); }
    try { localStorage.clear(); out.local = true; } catch (e) { note('local', e); }
    return out;
}"""
_PAUSE_DIRECT_JS = """(url) => {
    let n = 0;
    document.querySelectorAll('video').forEach(v => {
        const src = (v.currentSrc || v.src || '').split('#')[0];
        if (src !== url) return;
        try { v.pause(); n++; } catch (e) {}
    });
    return n;
}"""
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


_active_captures: set[str] = set()
_active_captures_lock = threading.Lock()


@contextmanager
def _protect_capture(slug: str):
    with _active_captures_lock:
        _active_captures.add(slug)
    try:
        yield
    finally:
        with _active_captures_lock:
            _active_captures.discard(slug)


def _minimize_chrome_window(session) -> None:
    """CDP로 Chrome 창을 최소화해 사용자 화면을 가리지 않는다 (단일 실행 보장)."""
    global _WINDOW_MINIMIZED
    if session is None or _WINDOW_MINIMIZED:
        return
    with _WINDOW_MINIMIZED_LOCK:
        if _WINDOW_MINIMIZED:
            return
        try:
            windows = session.send("Browser.getWindowForTarget")
            window_id = (windows or {}).get("windowId")
            if window_id:
                session.send("Browser.setWindowBounds", {
                    "windowId": window_id,
                    "bounds": {"windowState": "minimized"},
                })
                _WINDOW_MINIMIZED = True
        except Exception:
            pass


@contextmanager
def _handoff_slot(limit: int):
    """사용자 Chrome에서 동시에 재생·캡처하는 수를 제한한다."""
    global _HANDOFF_ACTIVE
    slots = max(1, min(int(limit), MAX_CONCURRENT_HANDOFFS))
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


@contextmanager
def _handoff_start():
    """병렬 슬롯(_handoff_slot) 내에서 각 탭이 지체 없이 병렬로 시작되도록 허용한다."""
    def release():
        pass
    try:
        yield release
    finally:
        pass


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
        except Exception as exc:
            # wrapper 에서는 플레이어 저장소가 막혀 로더에서 멈출 수 있으므로 폴백한 이유를 남긴다.
            log_event("playback_capture", "embed_article_fallback",
                      f"글 페이지에 임베드를 올리지 못해 wrapper 로 엽니다: {type(exc).__name__}: {mask_text(str(exc))[:200]}",
                      level="WARNING", page_url=article)
    page.set_content(_wrapper_html(embed_url), wait_until="domcontentloaded")
    return "wrapper"


def _attach_player_frame(page, capture) -> None:
    """플레이어가 미디어를 요청하기 전에 iframe의 네트워크 관찰을 붙인다.

    요청이 시작된 뒤에 붙은 CDP 세션은 그 응답 본문을 받지 못한다.
    """
    observed = getattr(capture, "frame_observed", None)
    if observed is None:
        capture.attach_frames(page)
        return
    for _ in range(100):
        try:
            handle = page.query_selector("#player_frame")
            frame = handle.content_frame() if handle is not None else None
            capture.attach_frames(page, frames=[frame] if frame is not None else [])
            if frame is not None and observed(frame):
                break
            page.wait_for_timeout(50)
        except Exception:
            break
    capture.attach_frames(page)


class _AdPopupWatch:
    """작업 탭과 그 팝업이 연 추가 탭만 닫는다. 브라우저 전체의 새 탭은 건드리지 않는다."""

    def __init__(self, page: Page):
        self._watched = {page}
        self._pending: set[Page] = set()
        page.on("popup", self._on_popup)

    def _on_popup(self, popup: Page) -> None:
        if popup in self._watched:
            return
        self._watched.add(popup)
        self._pending.add(popup)
        popup.on("popup", self._on_popup)

    def close(self) -> int:
        """이벤트에서는 기록만 하고 대기 루프에서 닫는다. about:blank 도 대상이다."""
        closed = 0
        for popup in list(self._pending):
            try:
                if popup.is_closed():
                    self._pending.discard(popup)
                    continue
                url = popup.url
                popup.close()
            except Exception as exc:
                log_event("browser", "popup_close_failed", mask_text(str(exc)), level="WARNING")
                continue
            self._pending.discard(popup)
            closed += 1
            log_event("browser", "popup_closed", "재생 탭에서 열린 광고 팝업을 닫음", frame_url=url)
        if closed:
            log(f"  [광고 탭] 재생 페이지에서 열린 팝업 {closed}개를 닫았습니다.")
        return closed

    def stop(self) -> None:
        self.close()
        for page in self._watched:
            try:
                page.remove_listener("popup", self._on_popup)
            except Exception as exc:
                log_event("browser", "popup_watch_stop_failed", mask_text(str(exc)), level="WARNING")
        self._watched.clear()


def _wait_for_download_button(
    page,
    timeout_ms: int,
    capture=None,
    popup_watch: _AdPopupWatch | None = None,
    cancel_event=None,
) -> None:
    """버튼 대기 중에도 본문 읽기를 이벤트 밖에서 한다. 즉시 실패하면 재시도로 시간을 끌지 않는다."""
    import time

    locator = page.frame_locator("#player_frame").locator(DOWNLOAD_BUTTON_SELECTOR).first
    deadline = time.monotonic() + max(int(timeout_ms), 0) / 1000
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt("cancelled")
        if popup_watch is not None:
            popup_watch.close()
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError("download button wait timed out")
        started = time.monotonic()
        try:
            locator.wait_for(state="attached", timeout=min(500, remaining_ms))
            return
        except Exception as exc:
            err_name = type(exc).__name__
            err_msg = str(exc).lower()
            if "TargetClosed" in err_name or "target closed" in err_msg:
                log_event("playback_capture", "page_wait_failed",
                          f"{err_name}: {mask_text(str(exc))[:300]}", level="ERROR",
                          capture_dir=str(getattr(capture, "out_dir", "")))
                if capture is not None:
                    capture.stop_reason = "target_closed"
                raise
            if err_name != "TimeoutError":
                log_event("playback_capture", "page_wait_failed",
                          f"{err_name}: {mask_text(str(exc))[:300]}", level="ERROR",
                          capture_dir=str(getattr(capture, "out_dir", "")))
                raise
            if time.monotonic() - started < 0.05 and remaining_ms > 500:
                raise
            if popup_watch is not None:
                popup_watch.close()
        if capture is not None:
            attach = getattr(capture, "attach_frames", None)
            if attach is not None:
                attach(page)
            drain = getattr(capture, "drain", None)
            if drain is not None:
                drain()


def streaming_stop_reason(
    *,
    quiet: float,
    complete: bool,
    covered: int,
    total: int,
    media_seen: int,
    inflight: int,
    tap_bytes: int = 0,
    stream_bytes: int = 0,
    recovery_failed: bool = False,
) -> str:
    """수신이 멈춘 이유. 빈 문자열이면 계속 기다린다.

    미디어 응답을 보기만 하고 본문이 아직 없으면 45초에 끊지 않는다.
    그때 끊으면 재생은 됐는데 저장은 0인 채로 끝난다.
    """
    if inflight:
        return ""
    if complete and quiet >= 3:
        return "complete"
    # 대체 수신까지 실패했고 받은 바이트가 없으면 더 기다려도 처음 구간을 채울 수 없다.
    if recovery_failed and quiet >= 10 and not (covered or tap_bytes or stream_bytes):
        return "recovery_failed"
    # 99% 이상 수신된 상태에서 10초 이상 추가 패킷이 없으면(재생 완료 상태) 조립 완료로 판정
    if total and (covered / total >= 0.99) and quiet >= 10:
        return "complete"
    if quiet >= 90 and (covered > 0 or tap_bytes > 0 or stream_bytes > 0 or total):
        return "stalled"
    if quiet >= 45 and media_seen <= 0 and not (covered or total or tap_bytes or stream_bytes):
        return "no_media"
    if quiet >= 90 and not (covered or total or tap_bytes or stream_bytes):
        return "media_unreadable"
    return ""


def _player_duration(page, capture) -> float | None:
    try:
        if page.is_closed():
            return None
        urls = capture.observed_media_urls()
        if not isinstance(urls, list) or not urls:
            return None
        handle = page.query_selector("#player_frame")
        frame = handle.content_frame() if handle is not None else None
        if frame is None:
            return None
        value = frame.evaluate("""(urls) => {
            try {
                const videos = document.querySelectorAll('video');
                if (videos.length !== 1) return null;
                const src = (videos[0].currentSrc || videos[0].src || '').split('#')[0];
                if (!urls.includes(src)) return null;
                const duration = videos[0].duration;
                return Number.isFinite(duration) && duration > 0 ? duration : null;
            } catch (e) {
                return null;
            }
        }""", urls)
    except PlaywrightError as exc:
        log_event("playback_capture", "duration_read_failed", mask_text(str(exc)), level="WARNING")
        return None
    except Exception:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def _player_playback_ended(page) -> bool:
    try:
        if page.is_closed():
            return False
        handle = page.query_selector("#player_frame")
        frame = handle.content_frame() if handle is not None else None
        if frame is None:
            return False
        return bool(frame.evaluate("""() => {
            try {
                const videos = document.querySelectorAll('video');
                if (!videos.length) return false;
                for (const v of videos) {
                    if (v.ended) return true;
                    if (v.duration > 0 && v.currentTime > 0 && v.currentTime >= v.duration - 1.5) return true;
                }
            } catch (e) {}
            return false;
        }"""))
    except Exception:
        return False


def _pause_player_for_direct(page, capture, url: str) -> int:
    """직접 다운로드가 같은 파일을 받는 동안 브라우저 재생을 멈춘다.

    재생을 멈추면 디코딩과 렌더링, 그리고 같은 바이트를 CDP 로 한 번 더 넘기는 일이 함께 줄어든다.
    광고 영상처럼 주소가 다른 video 는 건드리지 않는다.
    """
    frames = []
    getter = getattr(capture, "media_frame", None)
    frame = getter(url) if callable(getter) else None
    if frame is not None:
        frames.append(frame)
    try:
        handle = page.query_selector("#player_frame")
        player = handle.content_frame() if handle is not None else None
        if player is not None and player not in frames:
            frames.append(player)
    except Exception:
        pass
    paused = 0
    for fr in frames:
        try:
            value = fr.evaluate(_PAUSE_DIRECT_JS, url)
        except Exception:
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            paused += value
    return paused


def _capture_until_full(
    page,
    capture,
    max_seconds: int | None = None,
    popup_watch: _AdPopupWatch | None = None,
    on_started: Callable[[], None] | None = None,
    cancel_event=None,
    max_duration_seconds: float = DEFAULT_STREAMING_MAX_DURATION_SECONDS,
) -> None:
    """버튼이 없다고 판정한 뒤의 스트리밍 수신이다. 이후 버튼이 생겨도 download 페이지로 바꾸지 않는다."""
    import time

    if max_seconds is None:
        max_seconds = CAPTURE_MAX_SECONDS
    deadline = time.monotonic() + max(int(max_seconds), 1)
    last_signature = None
    quiet_since = time.monotonic()
    last_log = 0.0
    last_duration_check = float("-inf")
    last_ended_check = 0.0
    last_pause_check = float("-inf")
    paused_for_direct = False
    # 이 탭을 앞으로 가져오면 다른 재생 탭이 백그라운드로 내려가 요청이 멈출 수 있다.
    log("  [재생 캡처] 다른 재생 탭을 멈추지 않도록 이 탭을 앞으로 가져오지 않습니다.")
    while time.monotonic() < deadline:
        now = time.monotonic()
        if cancel_event is not None and cancel_event.is_set():
            capture.stop_reason = "cancelled"
            log("  [재생 캡처] 사용자 중단(Ctrl+C)으로 캡처를 중단합니다.")
            return
        if popup_watch is not None:
            popup_watch.close()
        if max_duration_seconds > 0 and now - last_duration_check >= DURATION_CHECK_SECONDS:
            last_duration_check = now
            duration = _player_duration(page, capture)
            if duration is not None:
                capture.video_duration = duration
                if duration >= max_duration_seconds:
                    capture.stop_reason = "duration_limit"
                    log(f"  [재생 캡처] 영상 길이 {duration:.1f}초가 상한 {max_duration_seconds:.1f}초 이상이므로 건너뜁니다.")
                    return
        capture.attach_frames(page)
        primed = False
        reload = getattr(capture, "reload_untracked_player", None)
        if reload is not None and reload() is True:
            # 버튼 판정이 끝난 뒤에만 다시 불러온다. 다시 불러온 플레이어도 깨워야 재생이 시작된다.
            log("  [재생 캡처] 캡처하지 못한 미디어 구간이 있어 플레이어를 다시 불러옵니다.")
            _prime_embed_player(page, popup_watch, capture)
            primed = True
        drain_started = time.monotonic()
        capture.drain(allow_browser_fallback=True)
        drain_seconds = time.monotonic() - drain_started

        state_getter = getattr(capture, "direct_state", None)
        direct_state = state_getter() if callable(state_getter) else ""
        if (PAUSE_PLAYER_DURING_DIRECT and direct_state == "streaming"
                and now - last_pause_check >= DIRECT_PAUSE_CHECK_SECONDS):
            last_pause_check = now
            direct_url = capture.direct_url()
            if isinstance(direct_url, str) and direct_url and _pause_player_for_direct(page, capture, direct_url):
                if not paused_for_direct:
                    log("  [재생 캡처] 직접 다운로드가 본편을 받고 있어 브라우저 재생을 일시정지합니다(Chrome 부하 감소).")
                paused_for_direct = True
        elif paused_for_direct and direct_state != "streaming":
            paused_for_direct = False
            if not capture.media_progress().get("complete"):
                log(f"  [재생 캡처] 직접 다운로드가 끝나지 못해({direct_state}) 브라우저 재생으로 다시 받습니다.")
                _prime_embed_player(page, popup_watch, capture)
                primed = True

        progress = capture.media_progress()
        covered = int(progress.get("covered") or 0)
        total = int(progress.get("total") or 0)
        media_seen = int(getattr(capture, "media_seen", 0) or 0)
        inflight = int(getattr(capture, "tap_inflight", 0) or 0)
        tap_bytes = int(getattr(capture, "tap_bytes", 0) or 0)
        stream_bytes = int(progress.get("stream_bytes") or 0)
        expected = int(progress.get("expected_total") or 0)
        received = max(covered, tap_bytes, stream_bytes)
        if on_started is not None and (covered > 0 or tap_bytes > 0 or stream_bytes > 0):
            on_started()
            on_started = None
        signature = (covered, tap_bytes, stream_bytes)
        if signature != last_signature:
            last_signature = signature
            quiet_since = now
        quiet = now - quiet_since

        # 플레이어 종료 여부는 패킷 수신이 멈추었거나(quiet >= 3초) 15초 이상 경과 시에만 스로틀링하여 확인한다
        if total > 0 and (quiet >= 3.0 or now - last_ended_check >= 15.0):
            last_ended_check = now
            if _player_playback_ended(page):
                if hasattr(capture, "fill_gaps"):
                    capture.fill_gaps()
                progress = capture.media_progress()
                c_covered = int(progress.get("covered") or 0)
                c_total = int(progress.get("total") or 0)
                if progress.get("complete") or (c_total and c_covered / c_total >= 0.99):
                    log(f"  [재생 캡처] 브라우저 재생 종료 감지: 본편 수신 완료({c_covered}/{c_total} bytes)")
                    capture.stop_reason = "complete"
                    return
        reason = streaming_stop_reason(
            quiet=quiet,
            complete=bool(progress.get("complete")),
            covered=covered,
            total=total,
            media_seen=media_seen,
            inflight=inflight,
            tap_bytes=tap_bytes,
            stream_bytes=stream_bytes,
            recovery_failed=bool(progress.get("recovery_failed")),
        )
        if reason == "complete":
            log(f"  [재생 캡처] 본편 전체 수신 {covered}/{total} bytes")
            capture.stop_reason = reason
            return
        if reason:
            capture.stop_reason = reason
            errors = " | ".join(progress.get("errors") or []) or getattr(capture, "cache_error", "") or "-"
            log(
                f"  [재생 캡처] 중단 사유: {reason}. 받은 본문 {covered}/{total or expected} bytes, "
                f"스트림 수신 {stream_bytes} bytes, 미디어 응답 {media_seen}건, 오류 {errors}"
            )
            stopped = _log_player_state(page, "player_state_at_stop", f"중단 시점 플레이어 상태: {reason}",
                                        level="WARNING", storage=True, reason=reason)
            if stopped is not None:
                log(f"  [플레이어] 중단 시점 상태: {_state_brief(stopped)}")
            return
        if now - last_log >= 5:
            last_log = now
            if stream_bytes > covered:
                log(f"  [재생 캡처] 스트림 누적 수신 {stream_bytes} bytes, "
                    f"완료된 본문 {covered}/{total or expected} bytes")
            elif total:
                log(f"  [재생 캡처] 받는 중 {covered}/{total} bytes ({covered * 100 // total}%)")
            elif tap_bytes:
                log(f"  [재생 캡처] 미디어 응답을 파일로 받는 중 {tap_bytes} bytes")
            elif expected:
                log(f"  [재생 캡처] 본문 수신 대기 중 0/{expected} bytes (미디어 응답 {media_seen}건)")
            else:
                log(f"  [재생 캡처] 받는 중 {covered} bytes, 전체 길이 아직 모름 (미디어 응답 {media_seen}건)")
        for _ in range(5):
            if cancel_event is not None and cancel_event.is_set():
                capture.stop_reason = "cancelled"
                return
            try:
                page.wait_for_timeout(200)
            except Exception as exc:
                err_name = type(exc).__name__
                err_msg = str(exc).lower()
                if "TargetClosed" in err_name or "target closed" in err_msg:
                    capture.stop_reason = "target_closed"
                else:
                    capture.stop_reason = capture.stop_reason or "page_closed"
                return
        if popup_watch is not None:
            popup_watch.close()
        iteration = time.monotonic() - now
        # 플레이어를 다시 깨운 바퀴는 원래 몇 초 걸린다.
        if iteration >= LOOP_STALL_WARN_SECONDS and not primed:
            log_event(
                "playback_capture", "loop_stall",
                f"재생 캡처 루프 한 바퀴 {iteration:.1f}s (drain {drain_seconds:.1f}s)",
                level="WARNING", capture_dir=str(getattr(capture, "out_dir", "")),
                iteration_seconds=round(iteration, 1), drain_seconds=round(drain_seconds, 1),
                direct_state=direct_state if isinstance(direct_state, str) else "",
            )
    capture.stop_reason = getattr(capture, "stop_reason", "") or "limit"
    log("  [재생 캡처] 최대 대기 시간에 도달해 중단")


def total_known(progress: dict) -> bool:
    return bool(int(progress.get("total") or 0))


def _prime_embed_player(page, popup_watch: _AdPopupWatch | None = None, capture=None) -> None:
    """로더에 멈춘 플레이어를 깨운다. 수동 재생은 웹페이지 클릭 뒤에야 컨트롤바가 생긴다."""
    for rnd in range(_PRIME_ROUNDS):
        if rnd == 0 and capture is not None:
            log("  [재생 캡처] 음소거로 재생합니다. 자동재생용이며 mp4 Range 수신량에는 영향 없습니다.")
        if capture is not None:
            capture.attach_frames(page)
        if popup_watch is not None:
            popup_watch.close()
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
        if popup_watch is not None:
            popup_watch.close()


def _keep_console_message(msg) -> bool:
    """플레이어의 DevTools 탐지가 0.5초마다 찍는 clear/table/객체 덤프는 console 기록 한도를 채우므로 남기지 않는다."""
    kind = str(getattr(msg, "type", "") or "")
    if kind in ("clear", "table"):
        return False
    text = str(getattr(msg, "text", "") or "")
    return not (kind == "log" and (text == "{}" or text.startswith("[Object")))


def _player_frame(page):
    try:
        handle = page.query_selector("#player_frame")
        return handle.content_frame() if handle is not None else None
    except Exception:
        return None


def _player_state(page) -> dict[str, Any] | None:
    """플레이어 프레임의 초기화 상태. 읽지 못하면 None."""
    frame = _player_frame(page)
    if frame is None:
        return None
    try:
        state = frame.evaluate(_PLAYER_STATE_JS)
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _player_storage_state(page) -> dict[str, Any] | None:
    frame = _player_frame(page)
    if frame is None:
        return None
    try:
        state = frame.evaluate(_PLAYER_STORAGE_JS)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {mask_text(str(exc))[:200]}"}
    return state if isinstance(state, dict) else None


def _player_phase(state: dict[str, Any]) -> str:
    """ready: 플레이어가 만들어짐. error: 플레이어가 안내 창을 띄움. loading: 아직 로더. unknown: 알 수 없는 페이지."""
    if state.get("notice"):
        return "error"
    if isinstance(state.get("jw"), dict) or state.get("jw_dom") is True or state.get("videos"):
        return "ready"
    if state.get("loader") is True or state.get("sotrym") is True or state.get("jw_lib") is True:
        return "loading"
    return "unknown"


def _state_brief(state: dict[str, Any]) -> str:
    jw = state.get("jw")
    videos = state.get("videos")
    parts = [
        f"loader={state.get('loader')}",
        f"jw={jw.get('state') or '?'}/{jw.get('playlist')}" if isinstance(jw, dict) else "jw=-",
        f"video={len(videos) if isinstance(videos, list) else '-'}",
        f"sw={state.get('sw_controller') or '-'}",
        f"visible={state.get('visibility') or '-'}",
    ]
    if state.get("notice"):
        parts.append(f"notice={str(state['notice'])[:80]}")
    return " ".join(parts)


def _log_player_state(page, event: str, message: str = "", level: str = "INFO",
                      storage: bool = False, **fields) -> dict[str, Any] | None:
    """플레이어 상태를 진단 이벤트로 남긴다. 상태를 읽지 못하면 남기지 않는다."""
    state = _player_state(page)
    if state is None:
        return None
    details: dict[str, Any] = dict(fields, player=state)
    if storage:
        details["storage"] = _player_storage_state(page)
    brief = _state_brief(state)
    log_event("playback_capture", event, f"{message} ({brief})" if message else brief, level=level, **details)
    return state


def _media_seen(capture) -> int:
    seen = getattr(capture, "media_seen", 0)
    return seen if isinstance(seen, int) and not isinstance(seen, bool) else 0


def _wait_player_init(page, capture=None, popup_watch: _AdPopupWatch | None = None, cancel_event=None,
                      reloading: bool = False) -> tuple[str, dict[str, Any] | None, float]:
    """플레이어가 만들어질 때까지 기다린다. (phase, 마지막 상태, 기다린 초)를 돌려준다.

    reloading 이면 프레임을 다시 불러오는 중이라 상태를 잠시 읽지 못해도 계속 기다린다.
    """
    seconds = PLAYER_INIT_WAIT_SECONDS
    started = time.monotonic()
    state: dict[str, Any] | None = None
    while True:
        waited = time.monotonic() - started
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled", state, waited
        fresh = _player_state(page)
        if fresh is not None:
            state = fresh
            phase = _player_phase(state)
            # 다시 불러오는 중에는 아직 비어 있는 문서(unknown)도 기다린다.
            if phase in ("ready", "error") or (phase == "unknown" and not reloading):
                return phase, state, waited
        elif not reloading:
            return "unknown", state, waited
        if _media_seen(capture) > 0:
            return "ready", state, waited
        if waited >= seconds:
            return (_player_phase(state) if state is not None else "unknown"), state, waited
        if popup_watch is not None:
            popup_watch.close()
        try:
            page.wait_for_timeout(500)
        except Exception:
            return "unknown", state, time.monotonic() - started
        if capture is not None:
            capture.attach_frames(page)
            capture.drain()


def _frame_origin(frame) -> str:
    try:
        parts = urlsplit(str(getattr(frame, "url", "") or ""))
    except ValueError:
        return ""
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("http", "https") and parts.netloc else ""


def _find_frame_id(node, url: str) -> str:
    if not isinstance(node, dict):
        return ""
    frame = node.get("frame") or {}
    if str(frame.get("url") or "") == url.split("#", 1)[0]:
        return str(frame.get("id") or "")
    for child in node.get("childFrames") or []:
        found = _find_frame_id(child, url)
        if found:
            return found
    return ""


def _reset_player_storage(page) -> dict[str, Any]:
    """플레이어 프레임의 저장소(임베드 origin + 글 페이지 site 조합)만 지운다.

    남아 있는 서비스워커·IndexedDB 상태 때문에 플레이어가 로더에서 멈추는 경우를 위한 것이다.
    쿠키와 다른 사이트의 저장소는 건드리지 않는다. 저장소 키가 플레이어 origin 이 아니면 지우지 않는다.
    """
    result: dict[str, Any] = {"ok": False, "method": "", "storage_key": "", "errors": []}

    def note(step: str, exc: Exception) -> None:
        if len(result["errors"]) < 8:
            result["errors"].append(f"{step}: {type(exc).__name__}: {mask_text(str(exc))[:160]}")

    frame = _player_frame(page)
    origin = _frame_origin(frame) if frame is not None else ""
    if not origin:
        result["errors"].append("player_frame_missing")
        return result
    context = getattr(page, "context", None)
    sessions: list[Any] = []
    frame_id = ""
    try:
        if context is not None and hasattr(context, "new_cdp_session"):
            try:
                own = context.new_cdp_session(frame)
                sessions.append(own)
                tree = own.send("Page.getFrameTree") or {}
                frame_id = str(((tree.get("frameTree") or {}).get("frame") or {}).get("id") or "")
            except Exception as exc:
                # 같은 프로세스 iframe 은 별도 세션이 없다. 페이지 세션의 프레임 트리에서 찾는다.
                note("frame_session", exc)
            try:
                top = context.new_cdp_session(page)
                sessions.append(top)
                if not frame_id:
                    tree = top.send("Page.getFrameTree") or {}
                    for child in (tree.get("frameTree") or {}).get("childFrames") or []:
                        frame_id = _find_frame_id(child, str(frame.url))
                        if frame_id:
                            break
            except Exception as exc:
                note("page_session", exc)
        for session in sessions if frame_id else []:
            key = ""
            for method in ("Storage.getStorageKeyForFrame", "Storage.getStorageKey"):
                try:
                    key = str((session.send(method, {"frameId": frame_id}) or {}).get("storageKey") or "")
                except Exception as exc:
                    note(method, exc)
                    continue
                if key:
                    break
            if not key:
                continue
            if not key.startswith(origin + "/"):
                result["errors"].append("storage_key_origin_mismatch")
                continue
            try:
                session.send("Storage.clearDataForStorageKey",
                             {"storageKey": key, "storageTypes": PLAYER_RESET_STORAGE_TYPES})
            except Exception as exc:
                note("clear", exc)
                continue
            result.update(ok=True, method="cdp", storage_key=key)
            break
    finally:
        for session in sessions:
            try:
                session.detach()
            except Exception:
                pass
    if not result["ok"]:
        try:
            cleared = frame.evaluate(_CLEAR_PLAYER_STORAGE_JS)
        except Exception as exc:
            note("js_clear", exc)
        else:
            if isinstance(cleared, dict):
                result.update(ok=True, method="js", cleared=cleared)
    return result


def _reload_player_frame(page) -> bool:
    """플레이어 iframe 을 원래 주소로 다시 연다. 프레임이 오류 페이지로 바뀌어 있어도 되돌아온다."""
    try:
        return bool(page.evaluate("""() => {
            const frame = document.getElementById('player_frame');
            if (!frame) return false;
            frame.src = frame.src;
            return true;
        }"""))
    except Exception:
        return False


def _ensure_player_initialized(page, capture=None, popup_watch: _AdPopupWatch | None = None,
                               cancel_event=None) -> dict[str, Any]:
    """재생 시도 뒤 플레이어가 실제로 만들어졌는지 확인한다.

    로더에서 멈춰 있으면 플레이어 저장소를 지우고 프레임을 한 번 다시 불러온다.
    status: ready / stuck / error / unknown / cancelled. 상태를 읽을 수 없는 페이지는 unknown 으로 두고 건드리지 않는다.
    """
    info: dict[str, Any] = {"status": "unknown", "reset": False}
    first = _player_state(page)
    if first is None:
        return info
    late = _player_phase(first) == "loading"
    phase, state, waited = _wait_player_init(page, capture, popup_watch, cancel_event)
    info.update(status=phase, waited=round(waited, 1), state=state)
    if phase == "ready":
        log_event("playback_capture", "player_ready", _state_brief(state or {}),
                  waited=round(waited, 1), late=late, player=state)
        jw = (state or {}).get("jw")
        if late and _media_seen(capture) <= 0 and isinstance(jw, dict) and jw.get("state") == "idle":
            log("  [플레이어] 초기화가 재생 시도보다 늦게 끝나 재생을 다시 시도합니다.")
            _prime_embed_player(page, popup_watch, capture)
        return info
    if phase == "error":
        log(f"  [플레이어] 플레이어가 안내 창을 띄웠습니다: {mask_text(str((state or {}).get('notice') or ''))[:160]}")
        _log_player_state(page, "player_notice", "플레이어가 안내 창을 띄움", level="ERROR", storage=True)
        return info
    if phase != "loading":
        return info

    info["status"] = "stuck"
    storage = _player_storage_state(page)
    log(f"  [플레이어] {waited:.0f}초가 지나도 로더에서 멈춰 있습니다 ({_state_brief(state or {})}). "
        "플레이어 저장소(서비스워커·IndexedDB)를 지우고 다시 불러옵니다.")
    log_event("playback_capture", "player_init_stuck", f"플레이어가 로더에서 멈춤 ({_state_brief(state or {})})",
              level="WARNING", waited=round(waited, 1), player=state, storage=storage)
    reset = _reset_player_storage(page)
    try:
        # 서비스워커 삭제가 끝나기 전에 다시 열면 탐색이 오류 페이지로 끝날 수 있다.
        page.wait_for_timeout(500)
    except Exception:
        pass
    reloaded = _reload_player_frame(page)
    info.update(reset=True, reset_result=reset, before_reset={"player": state, "storage": storage})
    log(f"  [플레이어] 저장소 초기화: {'성공' if reset.get('ok') else '실패'} "
        f"(방법 {reset.get('method') or '-'}, 키 {reset.get('storage_key') or '-'}, "
        f"오류 {' | '.join(reset.get('errors') or []) or '-'}), 다시 불러오기 {'요청함' if reloaded else '실패'}")
    log_event("playback_capture", "player_storage_reset", "플레이어 저장소를 지우고 프레임을 다시 불러옴",
              level="WARNING", reset=reset, reloaded=reloaded)
    if capture is not None:
        _attach_player_frame(page, capture)
    phase, state, waited = _wait_player_init(page, capture, popup_watch, cancel_event, reloading=True)
    if phase == "unknown":
        # 플레이어 문서가 아니다(탐색 실패 등). 한 번만 더 연다.
        log_event("playback_capture", "player_reload_retry", "다시 불러온 프레임이 플레이어 문서가 아니어서 한 번 더 엽니다",
                  level="WARNING", player=state)
        _reload_player_frame(page)
        if capture is not None:
            _attach_player_frame(page, capture)
        phase, state, waited = _wait_player_init(page, capture, popup_watch, cancel_event, reloading=True)
    info.update(waited_after_reset=round(waited, 1), state=state)
    if phase == "ready":
        info["status"] = "ready"
        log(f"  [플레이어] 저장소 초기화 후 {waited:.0f}초 만에 플레이어가 만들어졌습니다. 재생을 시도합니다.")
        log_event("playback_capture", "player_init_recovered", _state_brief(state or {}),
                  waited=round(waited, 1), player=state)
        _prime_embed_player(page, popup_watch, capture)
        return info
    if phase == "cancelled":
        info["status"] = "cancelled"
        return info
    after = _player_storage_state(page)
    info["after_reset"] = {"player": state, "storage": after}
    log(f"  [플레이어] 저장소 초기화 후에도 플레이어가 만들어지지 않았습니다 "
        f"({phase}, {_state_brief(state or {})}). 로그의 player_init_still_stuck 을 확인하세요.")
    log_event("playback_capture", "player_init_still_stuck",
              f"저장소 초기화 후에도 플레이어가 만들어지지 않음 ({phase}, {_state_brief(state or {})})",
              level="ERROR", waited=round(waited, 1), phase=phase, player=state, storage=after)
    return info


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
    minimize_window: bool = False,
    cancel_event=None,
    max_duration_seconds: float = DEFAULT_STREAMING_MAX_DURATION_SECONDS,
) -> dict[str, Any]:
    """30초 안에 download 버튼이 있으면 download 페이지로 연결하고, 없으면 다운로드 불가로 처리한다.

    버튼 판정이 끝난 뒤에는 경로를 바꾸지 않는다.
    스트리밍 fallback을 다시 활성화하면 반환 ok는 스트리밍 완료를 뜻하지 않으며, 파일 완료는 playback.complete 로 본다.
    연결한 Chrome 은 사용자의 브라우저이므로 닫지 않는다.
    download 탭 상한은 download 페이지를 새로 여는 것을 막는다.
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
    # 수신은 병렬로 유지하되 탭 생성 및 플레이어 초기화는 시차 락(_HANDOFF_INIT_LOCK)으로 직렬화한다.
    with _handoff_slot(max_concurrent_handoffs):
        with _slug_lock(slug), _handoff_start() as release_start, ExitStack() as connection:
            try:
                p = connection.enter_context(playwright or sync_playwright())
                with ExitStack() as popup_cleanup:
                    with _HANDOFF_INIT_LOCK:
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
                        wrapper = context.new_page()
                        # 재생 탭의 console 오류와 실패한 요청을 browser/ 로그에 남긴다.
                        attach_page_diagnostics(wrapper, f"handoff:{slug}", console_filter=_keep_console_message)
                        popup_watch = _AdPopupWatch(wrapper)
                        popup_cleanup.callback(popup_watch.stop)
                        if capture_dir and ENABLE_STREAMING_FALLBACK:
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
                        log_event("playback_capture", "embed_opened", f"임베드를 {embed_host} 위에 열었음",
                                  page_url=embed_url, embed_host=embed_host)
                        if capture is not None:
                            _attach_player_frame(wrapper, capture)
                        if minimize_window and session is not None:
                            _minimize_chrome_window(session)
                        _prime_embed_player(wrapper, popup_watch, capture)
                        if capture is not None:
                            log(
                                f"  [재생 캡처] CDP 세션 {getattr(capture, 'cdp_sessions', 0)}개 "
                                f"(iframe 별도 세션이 없으면 1개), "
                                f"같은 세션 프레임 {getattr(capture, 'frame_attach_skipped', 0)}개"
                            )
                        time.sleep(2.0)
                    # 플레이어가 로더에서 멈추면 버튼도 미디어 요청도 생기지 않는다. 판정 전에 확인하고 필요하면 되살린다.
                    try:
                        player_init = _ensure_player_initialized(wrapper, capture, popup_watch, cancel_event)
                    except Exception as exc:
                        # 확인·복구가 실패해도 기존 판정은 그대로 진행한다.
                        player_init = {"status": "unknown", "reset": False,
                                       "error": f"{type(exc).__name__}: {mask_text(str(exc))[:200]}"}
                        log_event("playback_capture", "player_init_check_failed", player_init["error"],
                                  level="WARNING", page_url=embed_url)
                    decision_ms = max(0, int(timeout_ms))
                    log(f"  [분기] download 버튼은 {decision_ms // 1000}초만 확인합니다.")
                    try:
                        _wait_for_download_button(wrapper, decision_ms, capture, popup_watch, cancel_event=cancel_event)
                    except Exception as exc:
                        if type(exc).__name__ != "TimeoutError":
                            raise
                        if capture is None:
                            reason = f"{decision_ms // 1000}초 안에 download 버튼이 없어 다운로드할 수 없습니다."
                            log(f"  [분기] {reason}")
                            log_event(
                                "download",
                                "unavailable_no_download_button",
                                reason,
                                level="ERROR",
                                page_url=embed_url,
                            )
                            _log_player_state(wrapper, "player_state_at_stop", "버튼 없음 판정 시점 플레이어 상태",
                                              level="WARNING", storage=True, reason="no_download_button")
                            return {
                                "ok": False,
                                "no_download_button": True,
                                "slug": slug,
                                "download_page": None,
                                "player_init": player_init,
                                "reason": reason,
                            }
                        log(f"  [분기] {decision_ms // 1000}초 안에 download 버튼이 없어 스트리밍 다운로드를 유지합니다.")
                        with _protect_capture(slug):
                            _capture_until_full(
                                wrapper, capture, popup_watch=popup_watch, on_started=release_start,
                                cancel_event=cancel_event, max_duration_seconds=max_duration_seconds,
                            )
                        keep_capture = True
                        playback = capture.summary()
                        if isinstance(playback, dict):
                            playback["player_init"] = player_init
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
                            capture_dir=str(capture.out_dir),
                        )
                        return {
                            "ok": True,
                            "no_download_button": True,
                            "skipped_long": capture.stop_reason == "duration_limit",
                            "duration": getattr(capture, "video_duration", None),
                            "reused": False,
                            "slug": slug,
                            "download_page": None,
                            "playback": playback,
                            "player_init": player_init,
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
                            "download",
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
                    try:
                        capture.uninstall(wrapper)
                        if not keep_capture:
                            capture.discard()
                    except Exception as exc:
                        log_event(
                            "playback_capture", "cleanup_failed", mask_text(str(exc)),
                            level="ERROR", slug=slug,
                        )
                if session is not None:
                    try:
                        session.detach()
                    except Exception:
                        pass
                if wrapper is not None:
                    try:
                        wrapper.close()
                    except Exception as exc:
                        log_event(
                            "browser",
                            "handoff_wrapper_close_failed",
                            f"handoff 재생 탭을 닫지 못함: {mask_text(str(exc))}",
                            level="ERROR",
                            slug=slug,
                        )
