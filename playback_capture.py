"""재생 중 브라우저가 받은 미디어 바이트를 CDP Network 이벤트로 저장한다.

기존 요청은 가로채거나 변경하지 않는다. CDP 스트림 연결이 실패하면 플레이어 프레임에서
별도의 범위 요청을 시도한다.
"""
from __future__ import annotations

import base64
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from diagnostics import log_event, mask_text, mask_url, sanitize

MEDIA_EXTENSIONS = (".m3u8", ".mpd", ".mp4", ".m4v", ".webm", ".ts", ".m4s", ".aac")
MEDIA_TYPES = (
    "video/",
    "audio/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
    "video/mp2t",
    "application/mp2t",
)
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Fetch interception pauses media responses. Keep the tap implementation available,
# but do not use it in the normal browser capture path.
ENABLE_CDP_MEDIA_TAP = False
BROWSER_FALLBACK_CHUNK_BYTES = 1024 * 1024
# 네트워크 이벤트의 URL에는 fragment가 없다. 서비스워커가 fragment로 영상을 구분하면 그대로 돌려줘야 한다.
_MEDIA_FRAGMENT_JS = """(url) => {
    for (const el of document.querySelectorAll('video, audio, source')) {
        const src = el.currentSrc || el.src || '';
        const hash = src.indexOf('#');
        if (hash > 0 && src.slice(0, hash) === url) return src.slice(hash);
    }
    return '';
}"""
_BROWSER_RANGE_FETCH_JS = """async ([url, start, end]) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
        const response = await fetch(url, {
            headers: {Range: `bytes=${start}-${end}`},
            credentials: 'include',
            signal: controller.signal,
        });
        if (response.status !== 206) throw new Error(`Range response HTTP ${response.status}`);
        const contentRange = response.headers.get('content-range');
        if (!contentRange) throw new Error('Content-Range is not exposed');
        if (!response.body) throw new Error('Response body is unavailable');
        const reader = response.body.getReader();
        const chunks = [];
        let length = 0;
        while (true) {
            const part = await reader.read();
            if (part.done) break;
            length += part.value.length;
            if (length > end - start + 1) {
                controller.abort();
                throw new Error('Range response is larger than requested');
            }
            chunks.push(part.value);
        }
        let binary = '';
        for (const chunk of chunks) {
            for (let i = 0; i < chunk.length; i += 8192) {
                binary += String.fromCharCode(...chunk.subarray(i, i + 8192));
            }
        }
        return {status: response.status, content_range: contentRange, body: btoa(binary)};
    } finally {
        clearTimeout(timer);
    }
}"""


def is_media_target(url: str, content_type: str = "") -> bool:
    path = urlsplit(url).path.lower()
    ctype = (content_type or "").lower()
    return path.endswith(MEDIA_EXTENSIONS) or any(token in ctype for token in MEDIA_TYPES)


_PLAYBACK_HOSTS = ("storage.googleapis.com", "googlevideo.com")
_PLAYBACK_PATH_TOKENS = ("videoplayback", "mediastorage", "/hls/", "/dash/")


def looks_like_playback_request(url: str, resource_type: str = "", headers: Any = None) -> bool:
    """응답 본문을 보기 전에, 재생 요청으로 가로챌 후보인지 판정한다."""
    parts = urlsplit(url)
    path = parts.path.lower()
    host = (parts.netloc or "").lower().split("@")[-1]
    if is_media_target(url) or resource_type == "media" or any(token in path for token in _PLAYBACK_PATH_TOKENS):
        return True
    if any(host == item or host.endswith("." + item) for item in _PLAYBACK_HOSTS):
        return True
    return bool(_header(headers, "range"))


def should_save_media(
    url: str,
    status: int,
    content_type: str,
    body: bytes,
    resource_type: str = "",
) -> bool:
    """재생으로 실제 받은 본문만 저장한다. 403/빈 응답은 다시 받을 후보가 아니다.

    수동 재생은 Range 요청에 206으로 응답하는 경우가 있다. 그 본문도 저장한다.
    """
    if status not in (200, 206) or not body:
        return False
    return is_media_target(url, content_type) or resource_type == "media"


_BYTE_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


def parse_byte_range(value: str) -> tuple[int, int, int | None] | None:
    """Content-Range를 반열림 구간 [start, end)와 전체 길이로 해석한다."""
    match = _BYTE_RANGE_RE.search(value or "")
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2)) + 1
    if end <= start:
        return None
    total = None if match.group(3) == "*" else int(match.group(3))
    return start, end, total


def request_range_from_response(value: str) -> str:
    parsed = parse_byte_range(value)
    if parsed:
        return f"bytes={parsed[0]}-{parsed[1] - 1}"
    if re.fullmatch(r"bytes=\d+-\d*", value, re.IGNORECASE):
        return value
    return ""


def clamp_byte_range(content_range: str, body_len: int) -> tuple[str, bool]:
    """헤더가 약속한 길이보다 짧게 읽은 본문은 그 길이까지만 받은 것으로 본다."""
    parsed = parse_byte_range(content_range)
    if parsed is None or body_len <= 0:
        return content_range, False
    start, end, total = parsed
    if body_len >= end - start:
        return content_range, False
    total_text = "*" if total is None else str(total)
    return f"bytes {start}-{start + body_len - 1}/{total_text}", True


def range_coverage(spans: list[tuple[int, int]], total: int | None) -> tuple[int, float | None]:
    """겹치는 구간을 합쳐 받은 바이트와, 아직 비어 있는 첫 위치의 비율을 돌려준다."""
    ordered = sorted((start, end) for start, end in spans if end > start)
    if not ordered:
        return 0, 0.0 if total else None
    merged: list[list[int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    covered = 0
    for start, end in merged:
        lo = start
        hi = end if total is None else min(end, total)
        if total is not None and lo >= total:
            continue
        if hi > lo:
            covered += hi - lo
    cursor = 0
    gap_at: int | None = None
    for start, end in merged:
        if start > cursor:
            gap_at = cursor
            break
        cursor = max(cursor, end)
        if total is not None and cursor >= total:
            cursor = total
            break
    if gap_at is None and total is not None and cursor < total:
        gap_at = cursor
    gap_ratio = None if gap_at is None or not total else min(1.0, gap_at / total)
    return covered, gap_ratio


def saved_media_name(url: str, index: int, content_range: str = "") -> str:
    """쿼리(일회용 토큰)는 파일명에 넣지 않는다. Range가 다르면 파일도 나눈다."""
    stem = Path(urlsplit(url).path).name or f"media_{index}"
    stem = _NAME_RE.sub("_", stem).strip("._")[:80] or f"media_{index}"
    tag = _NAME_RE.sub("_", content_range).strip("._")[:40]
    if tag:
        return f"{index:03d}_{tag}_{stem}"
    return f"{index:03d}_{stem}"


def _cdp_header(headers: Any, name: str) -> str:
    if isinstance(headers, list):
        wanted = name.lower()
        for item in headers:
            if str((item or {}).get("name") or "").lower() == wanted:
                return str((item or {}).get("value") or "")
        return ""
    return _header(headers, name)


def _cdp_resource_type(value: str) -> str:
    kind = (value or "").lower()
    return "media" if kind == "media" else kind


def _header(headers: Any, name: str) -> str:
    if not headers:
        return ""
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        if value is None:
            value = headers.get(name.title())
    except Exception:
        return ""
    return str(value or "")


class PlaybackCapture:
    """재생 중 받은 미디어 본문을 저장한다.

    교차 출처 플레이어는 별도 CDP 세션이다. page.route 로 멈추지 않고 그 세션에서 본문을 읽는다.
    설치는 재생 클릭보다 먼저 해야 한다. 버튼 확인이 끝난 뒤에 켜면 이미 지나간 본문은 없다.
    """

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.files: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self.skipped_count = 0
        self.seen_count = 0
        self.request_count = 0
        self.response_count = 0
        self.seen_samples: list[dict[str, Any]] = []
        self.request_samples: list[dict[str, Any]] = []
        self.response_samples: list[dict[str, Any]] = []
        self.cache_disabled = False
        self.service_worker_bypassed = False
        self.route_installed = False
        self.route_error = ""
        self.cache_error = ""
        self.capture_mode = "cdp_network"
        self.cdp_sessions = 0
        self.cdp_responses = 0
        self.cdp_finished = 0
        self.frame_attach_skipped = 0
        self.frame_errors: list[str] = []
        self.media_seen = 0
        self.tap_bytes = 0
        self.stream_bytes = 0
        self.tap_inflight = 0
        self.tap_status = "disabled_nonblocking"
        self.tap_error = ""
        self.tap_sessions = 0
        self.tap_paused = 0
        self.tap_saved = 0
        self.stop_reason = ""
        self.last_range_received = ""
        self.chunks_attempted = 0
        self.tap_cancel = False
        self._direct_download_thread = None
        self._direct_download_url = ""
        self._direct_bytes = 0
        self._direct_total = 0
        self._direct_complete = False
        self._direct_lock = threading.Lock()
        self._direct_path = None
        self._io_lock = threading.Lock()
        self._trace_lock = threading.Lock()
        self._capture_trace: list[dict[str, Any]] = []
        self._trace_count = 0
        self._tap = None
        self._cdp_url = ""
        self._seen: set[str] = set()
        self._claimed: set[str] = set()
        self._page = None
        self._attached_ids: set[str] = set()
        self._dead_frames: set[str] = set()
        self._skipped_frame_url: dict[str, str] = {}
        self._chunk_paths: dict[tuple[int, str], Path] = {}
        self._cdp_sessions: list[Any] = []
        self._cdp_session_by_id: dict[int, Any] = {}
        self._cdp_meta: dict[tuple[int, str], dict[str, Any]] = {}
        self._cdp_queue: list[dict[str, Any]] = []
        self._stream_queue: list[tuple[int, str]] = []
        self._stream_attempted: set[tuple[int, str]] = set()
        self._pending_chunks: list[tuple[int, str, bytes]] = []
        self._response_queue: list[Any] = []
        self._media_frames: dict[str, Any] = {}
        self._browser_fallback_queue: list[dict[str, Any]] = []
        self._browser_fallback_urls: set[str] = set()
        self._browser_fallback_failed = False
        self._untracked_media: list[str] = []
        self._player_reloaded = False
        self.expected_total = 0
        self.capture_errors: list[str] = []
        self._pending_frames: list[Any] = []
        self._draining = False
        self._saved_logs = 0
        self._skipped_logs = 0

    def install(self, page, cdp_url: str = "") -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._page = page
        self._cdp_url = cdp_url or ""
        # 라우트, Fetch interception, 캐시 우회는 설치하거나 변경하지 않는다.
        try:
            page.on("frameattached", self._on_frame_attached)
            page.on("framenavigated", self._on_frame_navigated)
            page.on("request", self._on_request)
            page.on("response", self._on_response)
            page.on("requestfinished", self._on_request_finished)
        except Exception as exc:
            log_event(
                "playback_capture",
                "listener_failed",
                f"요청/응답 관찰 설치 실패: {type(exc).__name__}",
                level="ERROR",
                capture_dir=str(self.out_dir),
            )
        self.attach_frames(page)
        if ENABLE_CDP_MEDIA_TAP:
            self._start_tap(page)
        log_event(
            "playback_capture",
            "installed",
            "재생 패킷 캡처를 재생 시작 전에 설치",
            capture_dir=str(self.out_dir),
            cache_disabled=self.cache_disabled,
            service_worker_bypassed=self.service_worker_bypassed,
            route_installed=self.route_installed,
            capture_mode=self.capture_mode,
            cdp_sessions=self.cdp_sessions,
        )

    def _start_tap(self, page) -> None:
        """페이지 세션이 못 읽는 교차 출처 미디어를 이 탭에서만 받는다."""
        if not self._cdp_url:
            self.tap_status = "no_endpoint"
            return
        target_id = self._page_target_id(page)
        from cdp_media_tap import MediaTap

        tap = MediaTap(self, self._cdp_url)
        tap.start(target_id)
        self._tap = tap
        self.tap_status = tap.status
        self.tap_error = tap.error
        self.tap_sessions = tap.sessions

    def _finish_tap(self, abort: bool = False) -> None:
        tap = self._tap
        if tap is None:
            return
        self._tap = None
        if abort:
            self.tap_cancel = True
        try:
            tap.finish(abort=abort)
        except Exception as exc:
            self.tap_error = self.tap_error or f"{type(exc).__name__}: {exc}"[:240]
        self.tap_status = tap.status or self.tap_status
        self.tap_error = tap.error or self.tap_error
        self.tap_sessions = tap.sessions
        self.tap_paused = tap.paused
        self.tap_saved = tap.saved

    def _page_target_id(self, page) -> str:
        context = getattr(page, "context", None)
        if context is None or not hasattr(context, "new_cdp_session"):
            self.tap_error = "no_cdp_context"
            return ""
        session = None
        try:
            session = context.new_cdp_session(page)
            info = session.send("Target.getTargetInfo")
            return str((info.get("targetInfo") or {}).get("targetId") or "")
        except Exception as exc:
            self.tap_error = f"{type(exc).__name__}: {exc}"[:240]
            return ""
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass

    def note_tap_bytes(self, size: int) -> None:
        if size > 0:
            self.tap_bytes += int(size)

    def temp_media_path(self, request_id: str) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        safe_id = _NAME_RE.sub("_", request_id)[:40] or "request"
        return self.out_dir / f".tap_{safe_id}.bin"

    def accept_tap_file(
        self,
        url: str,
        status: int,
        content_type: str,
        content_range: str,
        request_range: str,
        path: Path,
        size: int,
        resource_type: str,
    ) -> str:
        """탭에서 받은 본문 파일을 조각으로 남긴다. 저장한 경로를 돌려준다."""
        content_range, truncated = clamp_byte_range(content_range, size)
        if not path.is_file() or size <= 0:
            return ""
        if not should_save_media(url, status, content_type, b"x", resource_type):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            self._note_skipped(url, status, content_type, size, "not_media")
            return ""
        key = f"{url}|{request_range}|{content_range}|{size}|tap"
        with self._io_lock:
            if key in self._seen:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                self._note_skipped(url, status, content_type, size, "duplicate")
                return ""
            self._seen.add(key)
            dest = self.out_dir / saved_media_name(url, len(self.files) + 1, content_range)
            path.replace(dest)
            item = {
                "url": url,
                "status": status,
                "content_type": content_type,
                "content_range": content_range,
                "request_range": request_range,
                "path": str(dest),
                "size": size,
                "truncated": truncated,
                "via": "tap",
            }
            self.files.append(item)
            if content_range:
                self.last_range_received = content_range
            elif request_range:
                self.last_range_received = request_range
        if self._saved_logs < 80:
            self._saved_logs += 1
            log_event(
                "playback_capture",
                "packet_saved",
                f"tap {status} {size} bytes {mask_url(url)}",
                media_url=url,
                status=status,
                bytes=size,
                content_type=content_type,
                content_range=content_range,
                request_range=request_range,
                output=dest.name,
            )
        return str(dest)

    def uninstall(self, page=None) -> None:
        self._stop_direct_download()
        self._finish_tap()
        page = page or self._page
        if page is None:
            return
        try:
            page.remove_listener("frameattached", self._on_frame_attached)
            page.remove_listener("framenavigated", self._on_frame_navigated)
            page.remove_listener("request", self._on_request)
            page.remove_listener("response", self._on_response)
            page.remove_listener("requestfinished", self._on_request_finished)
        except Exception:
            pass
        for session in self._cdp_sessions:
            try:
                session.detach()
            except Exception:
                pass
        self._cdp_sessions.clear()
        self._cdp_session_by_id.clear()
        self._cdp_meta.clear()
        self._cdp_queue.clear()
        self._stream_queue.clear()
        self._stream_attempted.clear()
        self._pending_chunks.clear()
        self._response_queue.clear()
        self._media_frames.clear()
        self._browser_fallback_queue.clear()
        self._untracked_media.clear()
        for path in self._chunk_paths.values():
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        self._chunk_paths.clear()
        self._page = None

    def drain(self, *, allow_browser_fallback: bool = False) -> None:
        """이벤트 밖에서 CDP 명령과 파일 쓰기를 처리한다."""
        if self._draining:
            return
        self._draining = True
        try:
            streams = self._stream_queue
            self._stream_queue = []
            for sid, request_id in streams:
                self._start_stream(sid, request_id)
            chunks = self._pending_chunks
            self._pending_chunks = []
            for sid, request_id, raw in chunks:
                self._write_chunk(sid, request_id, raw)
            queued = self._cdp_queue
            self._cdp_queue = []
            for item in queued:
                self._read_cdp_body(item)
            responses = self._response_queue
            self._response_queue = []
            for request in responses:
                try:
                    response = request.response()
                except Exception as exc:
                    self._note_skipped(request.url, 0, "", 0, f"response_error:{type(exc).__name__}")
                    continue
                if response is not None:
                    self._read_response_body(response)
            if allow_browser_fallback:
                self._drain_browser_fallback()
        finally:
            self._draining = False

    def discard(self) -> None:
        """download 버튼이 있어 패킷이 필요 없을 때 임시 파일을 지운다."""
        self._stop_direct_download()
        self._finish_tap(abort=True)
        for item in self.files:
            try:
                Path(item["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        self.files.clear()
        try:
            (self.out_dir / "playback_capture.json").unlink(missing_ok=True)
        except Exception:
            pass
        try:
            self.out_dir.rmdir()
        except Exception:
            pass
        log_event("playback_capture", "discarded", "download 버튼이 있어 임시 재생 패킷을 삭제", capture_dir=str(self.out_dir))

    def media_progress(self) -> dict[str, Any]:
        """가장 큰 본편이 0부터 끝까지 모였는지 본다. 광고처럼 작은 완성 파일은 끝으로 치지 않는다."""
        with self._io_lock:
            files = list(self.files)
        groups: dict[str, dict[str, Any]] = {}
        if self._direct_bytes > 0 and self._direct_download_url:
            with self._direct_lock:
                d_url = self._direct_download_url
                d_bytes = self._direct_bytes
                d_total = self._direct_total
                d_complete = self._direct_complete
            groups[d_url] = {
                "spans": [(0, d_bytes)],
                "file_spans": [],
                "total": d_total,
                "status": 200 if d_complete else 206,
                "size": d_bytes,
                "item": {"url": d_url, "size": d_bytes, "status": 200 if d_complete else 206, "path": str(self._direct_path or "")},
                "pending": not d_complete,
            }
        for item in files:
            url = str(item.get("url") or "")
            group = groups.setdefault(url, {"spans": [], "file_spans": [], "total": None, "status": 0, "size": 0, "item": item})
            group["size"] += int(item.get("size") or 0)
            status = int(item.get("status") or 0)
            if status == 200:
                group["status"] = 200
                group["total"] = int(item.get("size") or 0)
                group["spans"] = [(0, group["total"])]
                group["file_spans"] = [(0, group["total"])]
                group["item"] = item
                continue
            parsed = parse_byte_range(str(item.get("content_range") or ""))
            if parsed is None:
                continue
            start, end, total = parsed
            size = int(item.get("size") or 0)
            if size and end - start > size:
                end = start + size
            if end <= start:
                continue
            group["spans"].append((start, end))
            group["file_spans"].append((start, end))
            if total is not None:
                group["total"] = max(int(group["total"] or 0), total)
            if group["status"] != 200:
                group["status"] = status
                group["item"] = item
        extra = {
            "stream_bytes": self.stream_bytes,
            "expected_total": self.expected_total,
            "recovery_failed": self._browser_fallback_failed and not self._browser_fallback_queue,
            "errors": list(self.capture_errors),
        }
        if not groups:
            return {"complete": False, "covered": 0, "total": 0, "gap_ratio": None,
                    "file_count": 0, "url": "", **extra}
        def rank(group: dict[str, Any]) -> tuple[int, int]:
            return int(group["total"] or 0), int(group["size"] or 0)
        url, group = max(groups.items(), key=lambda pair: rank(pair[1]))
        total = int(group["total"] or 0)
        covered, gap_ratio = range_coverage(group["spans"], total or None)
        file_covered, file_gap = range_coverage(group["file_spans"], total or None)
        complete = bool(total and gap_ratio is None and covered >= total and (
            not group.get("pending") or (file_gap is None and file_covered >= total)
        ))
        return {
            "complete": complete,
            "covered": covered,
            "total": total,
            "gap_ratio": gap_ratio,
            "file_count": len(files),
            "url": url,
            **extra,
        }

    def assemble(self) -> dict[str, Any] | None:
        """전체 범위가 모인 본편만 하나의 mp4로 합친다. 구멍 난 조각은 합치지 않는다."""
        progress = self.media_progress()
        if not progress["complete"]:
            return None
        url = progress["url"]
        pieces = []
        direct = None
        with self._io_lock:
            files = list(self.files)
        for item in files:
            if item.get("url") != url:
                continue
            if int(item.get("status") or 0) == 200:
                direct = item
                continue
            parsed = parse_byte_range(str(item.get("content_range") or ""))
            if parsed is None:
                continue
            pieces.append((parsed[0], parsed[1], item))
        if direct is not None and int(direct.get("size") or 0) >= int(progress["total"] or 0):
            return {"path": direct["path"], "size": direct["size"], "url": url}
        pieces.sort(key=lambda item: item[0])
        if not pieces or pieces[0][0] != 0:
            return None
        stem = Path(urlsplit(url).path).name or "media.mp4"
        stem = _NAME_RE.sub("_", stem).strip("._")[:80] or "media.mp4"
        if not stem.lower().endswith(".mp4"):
            stem = f"{stem}.mp4"
        path = self.out_dir / f"full_{stem}"
        cursor = 0
        try:
            with path.open("wb") as handle:
                for start, end, item in pieces:
                    if start > cursor:
                        path.unlink(missing_ok=True)
                        return None
                    body = Path(item["path"]).read_bytes()
                    if start + len(body) < end:
                        end = start + len(body)
                    if end <= start:
                        path.unlink(missing_ok=True)
                        return None
                    if start < cursor:
                        skip = cursor - start
                        if skip >= len(body):
                            continue
                        body = body[skip:]
                    handle.write(body)
                    cursor += len(body)
        except Exception:
            path.unlink(missing_ok=True)
            return None
        if progress["total"] and (cursor < int(progress["total"]) or path.stat().st_size < int(progress["total"])):
            path.unlink(missing_ok=True)
            return None
        return {"path": str(path), "size": path.stat().st_size, "url": url}

    def summary(self) -> dict[str, Any]:
        self._stop_direct_download()
        self._finish_tap()
        self.drain()
        assembled = self.assemble()
        progress = self.media_progress()
        output_url = (assembled or {}).get("url") or ""
        output_content_type = next(
            (item.get("content_type") or "" for item in self.files if item.get("url") == output_url), ""
        )
        with self._trace_lock:
            capture_trace = list(self._capture_trace)
            trace_count = self._trace_count
        payload = {
            "ok": bool(self.files),
            "files": list(self.files),
            "skipped": list(self.skipped),
            "skipped_count": self.skipped_count,
            "seen_count": self.seen_count,
            "request_count": self.request_count,
            "response_count": self.response_count,
            "seen_samples": list(self.seen_samples),
            "request_samples": list(self.request_samples),
            "response_samples": list(self.response_samples),
            "cache_disabled": self.cache_disabled,
            "service_worker_bypassed": self.service_worker_bypassed,
            "route_installed": self.route_installed,
            "route_error": self.route_error,
            "cache_error": self.cache_error,
            "capture_errors": list(self.capture_errors),
            "capture_mode": self.capture_mode,
            "cdp_sessions": self.cdp_sessions,
            "cdp_responses": self.cdp_responses,
            "cdp_finished": self.cdp_finished,
            "frame_attach_skipped": self.frame_attach_skipped,
            "frame_errors": list(self.frame_errors),
            "media_seen": self.media_seen,
            "tap_status": self.tap_status,
            "tap_error": self.tap_error,
            "tap_sessions": self.tap_sessions,
            "tap_paused": self.tap_paused,
            "tap_saved": self.tap_saved,
            "tap_bytes": self.tap_bytes,
            "stream_bytes": self.stream_bytes,
            "stop_reason": self.stop_reason,
            "last_range_received": self.last_range_received,
            "chunks_attempted": self.chunks_attempted,
            "chunks_saved": len(self.files),
            "bytes": sum(item["size"] for item in self.files),
            "covered": progress["covered"],
            "total": progress["total"],
            "complete": bool(assembled),
            "output": (assembled or {}).get("path") or "",
            "output_content_type": output_content_type,
            "output_extension": Path(urlsplit(output_url).path).suffix.lower(),
            "capture_dir": str(self.out_dir),
            "capture_trace": capture_trace,
            "capture_trace_dropped": max(0, trace_count - len(capture_trace)),
        }
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            (self.out_dir / "playback_capture.json").write_text(
                json.dumps(sanitize(payload), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        log_event(
            "playback_capture",
            "summary",
            f"저장 {len(self.files)}개 / {payload['bytes']} bytes, 건너뜀 {self.skipped_count}개",
            capture_dir=str(self.out_dir),
            saved=len(self.files),
            skipped=self.skipped_count,
            bytes=payload["bytes"],
            seen=self.seen_count,
            requests=self.request_count,
            responses=self.response_count,
            cache_disabled=self.cache_disabled,
            route_installed=self.route_installed,
            capture_mode=self.capture_mode,
            cdp_sessions=self.cdp_sessions,
            cdp_responses=self.cdp_responses,
            cdp_finished=self.cdp_finished,
        )
        return payload

    def _note_error(self, detail: str) -> None:
        """cache_error는 첫 오류만 담는다. 중단 사유를 가리지 않게 서로 다른 오류를 함께 남긴다."""
        self.cache_error = self.cache_error or detail
        if detail not in self.capture_errors and len(self.capture_errors) < 8:
            self.capture_errors.append(detail)

    def _trace(self, event: str, **fields: Any) -> None:
        entry = sanitize({
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event": event,
            **fields,
        })
        with self._trace_lock:
            self._trace_count += 1
            count = self._trace_count
            self._capture_trace.append(entry)
            if len(self._capture_trace) > 200:
                self._capture_trace.pop(0)
        if count <= 200 or event.endswith("_error") or event.endswith("_failed"):
            log_event("playback_capture", f"trace_{event}", level="DEBUG",
                      capture_dir=str(self.out_dir), **fields)

    def _sample(self, bucket: list[dict[str, Any]], item: dict[str, Any], limit: int = 40, *, interesting: bool = False) -> None:
        """일반 요청은 앞부분만, 재생으로 보이는 요청은 밀려나지 않게 남긴다."""
        hits = sum(1 for saved in bucket if saved.get("interesting"))
        if interesting:
            if hits < limit:
                bucket.append(item)
            return
        plain = len(bucket) - hits
        if plain < 15:
            bucket.append(item)

    def _claim_key(self, url: str, request_range: str, content_range: str = "", request_id: str = "") -> str:
        """같은 URL의 다른 Range를 하나로 합치지 않는다. 요청 Range가 비어 있어도 본문 범위로 구분한다."""
        if request_id:
            return f"{url}|id:{request_id}"
        if content_range:
            return f"{url}|cr:{content_range}"
        if request_range:
            return f"{url}|rr:{request_range}"
        return ""

    def _on_frame_attached(self, frame) -> None:
        self._pending_frames.append(frame)

    def _on_frame_navigated(self, frame) -> None:
        self._pending_frames.append(frame)

    def attach_frames(self, page=None, frames=None) -> None:
        """페이지와 교차 출처 iframe에 Network 관찰을 붙인다. 이벤트 안에서는 호출하지 않는다."""
        page = page or self._page
        targets: list[Any] = []
        pending = self._pending_frames
        self._pending_frames = []
        targets.extend(pending)
        if frames is not None:
            targets.extend(frames)
        elif page is not None:
            targets.append(page)
            try:
                targets.extend(list(page.frames))
            except Exception:
                pass
        main = getattr(page, "main_frame", None) if page is not None else None
        for target in targets:
            if target is None or target is main:
                continue
            self._try_attach(target)

    def _frame_key(self, target) -> str:
        for owner in (target, getattr(target, "_impl_obj", None)):
            guid = getattr(owner, "_guid", None)
            if guid:
                return str(guid)
        return f"id:{id(target)}"

    def _try_attach(self, target) -> None:
        key = self._frame_key(target)
        if key in self._attached_ids or key in self._dead_frames:
            return
        url = str(getattr(target, "url", "") or "")
        if self._skipped_frame_url.get(key) == url:
            self.frame_attach_skipped += 1
            return
        context = getattr(target, "context", None)
        if context is None:
            page = getattr(target, "page", None)
            context = getattr(page, "context", None)
        if context is None or not hasattr(context, "new_cdp_session"):
            self._note_frame_error("no_cdp_context")
            return
        try:
            session = context.new_cdp_session(target)
        except Exception as exc:
            text = str(exc)
            if "separate CDP session" in text or "No target with given id" in text or "Frame has been detached" in text:
                self.frame_attach_skipped += 1
                if "separate CDP session" in text:
                    self._skipped_frame_url[key] = url
                else:
                    self._dead_frames.add(key)
                return
            self._note_frame_error(f"{type(exc).__name__}: {text[:120]}")
            return
        self._attached_ids.add(key)
        self._wire_session(session)

    def _note_frame_error(self, message: str) -> None:
        if len(self.frame_errors) < 12:
            self.frame_errors.append(message)
    def _wire_session(self, session) -> None:
        sid = id(session)
        self._cdp_sessions.append(session)
        self._cdp_session_by_id[sid] = session
        self.cdp_sessions += 1
        try:
            session.send("Network.enable")
        except Exception as exc:
            self._note_error(f"cdp_enable:{type(exc).__name__}")
            self._trace("cdp_enable_error", session_id=sid, error=mask_text(str(exc))[:300],
                        error_type=type(exc).__name__)
        session.on("Network.requestWillBeSent", lambda event, key=sid: self._on_cdp_request(key, event))
        session.on("Network.responseReceived", lambda event, key=sid: self._on_cdp_response(key, event))
        session.on("Network.dataReceived", lambda event, key=sid: self._on_cdp_data(key, event))
        session.on(
            "Network.loadingFinished",
            lambda event, sess=session, key=sid: self._on_cdp_finished(sess, key, event),
        )
        session.on("Network.loadingFailed", lambda event, key=sid: self._on_cdp_failed(key, event))

    def _release_interception(self, session) -> None:
        """이전 page.route 가 Chrome에 남긴 요청 멈춤을 푼다. 캐시와 서비스워커는 건드리지 않는다."""
        for method, params in (
            ("Fetch.disable", {}),
            ("Network.setRequestInterception", {"patterns": []}),
        ):
            try:
                session.send(method, params)
            except Exception:
                continue

    def _meta_key(self, sid: int, request_id: str) -> tuple[int, str]:
        return sid, str(request_id or "")

    def _on_cdp_request(self, sid: int, event: dict[str, Any]) -> None:
        request = event.get("request") or {}
        url = str(request.get("url") or "")
        request_id = str(event.get("requestId") or "")
        if not url or not request_id:
            return
        headers = request.get("headers")
        slot = self._cdp_meta.setdefault(self._meta_key(sid, request_id), {})
        slot["url"] = url
        slot["method"] = str(request.get("method") or "").upper()
        slot["request_range"] = _cdp_header(headers, "range")
        slot["fragment"] = str(request.get("urlFragment") or "")
        slot["resource_type"] = _cdp_resource_type(str(event.get("type") or slot.get("resource_type") or ""))
        if (slot["method"] != "OPTIONS" and slot["resource_type"] != "preflight"
                and looks_like_playback_request(url, slot["resource_type"], headers)):
            self._trace("cdp_request", session_id=sid, request_id=request_id, url=mask_url(url),
                        method=slot["method"], resource_type=slot["resource_type"],
                        request_range=slot["request_range"], has_fragment=bool(slot["fragment"]))

    def _on_cdp_data(self, sid: int, event: dict[str, Any]) -> None:
        """이벤트에서는 청크만 큐에 넣고, 파일 쓰기는 drain에서 한다."""
        request_id = str(event.get("requestId") or "")
        slot = self._cdp_meta.get(self._meta_key(sid, request_id))
        if not request_id or slot is None:
            return
        if slot.get("method") == "OPTIONS" or slot.get("resource_type") == "preflight":
            return
        url = str(slot.get("url") or "")
        resource_type = str(slot.get("resource_type") or "")
        content_type = str(slot.get("content_type") or "")
        content_range = str(slot.get("content_range") or "")
        if not self._response_might_be_media(url, resource_type, content_type, content_range):
            if not looks_like_playback_request(
                url, resource_type, {"range": slot.get("request_range") or ""},
            ):
                return
        slot["data_events"] = int(slot.get("data_events") or 0) + 1
        slot["network_data_bytes"] = int(slot.get("network_data_bytes") or 0) + int(event.get("dataLength") or 0)
        encoded = event.get("data")
        if not encoded or not request_id:
            return
        try:
            raw = base64.b64decode(encoded) if event.get("base64Encoded", True) else str(encoded).encode("utf-8", "replace")
        except Exception:
            return
        if not raw:
            return
        slot["payload_bytes"] = int(slot.get("payload_bytes") or 0) + len(raw)
        self._pending_chunks.append((sid, request_id, raw))

    def _write_chunk(self, sid: int, request_id: str, raw: bytes) -> None:
        key = self._meta_key(sid, request_id)
        try:
            path = self._chunk_paths.get(key)
            if path is None:
                self.out_dir.mkdir(parents=True, exist_ok=True)
                safe_id = _NAME_RE.sub("_", request_id)[:40] or "request"
                path = self.out_dir / f".chunk_{sid}_{safe_id}.bin"
                self._chunk_paths[key] = path
            with path.open("ab") as handle:
                handle.write(raw)
            self.stream_bytes += len(raw)
        except Exception as exc:
            self._note_error(f"chunk_write:{type(exc).__name__}: {mask_text(str(exc))[:200]}")
            self._trace("chunk_write_error", session_id=sid, request_id=request_id,
                        error_type=type(exc).__name__, error=mask_text(str(exc))[:300])

    def _start_direct_media_download(self, url: str, content_range: str = "", content_type: str = "") -> None:
        """포착된 본편 미디어 URL에 대해 백그라운드 직접 스트림 다운로드를 개시한다."""
        if self._page is None:
            return
        referer = str(getattr(self._page, "url", "") or "")
        with self._direct_lock:
            if self._direct_download_url:
                return
            self._direct_download_url = url
            parsed = parse_byte_range(content_range)
            if parsed and parsed[2]:
                self._direct_total = parsed[2]
            dest = self.out_dir / "media_stream.mp4"
            self._direct_path = dest

        self._trace("direct_start", url=mask_url(url), content_range=content_range,
                    content_type=content_type, request_range="bytes=0-")
        thread = threading.Thread(
            target=self._run_direct_download,
            args=(url, content_range, content_type, dest, referer),
            name="direct-media-downloader",
            daemon=True,
        )
        self._direct_download_thread = thread
        thread.start()

    def _stop_direct_download(self) -> None:
        thread = self._direct_download_thread
        if thread is None:
            return
        self.tap_cancel = True
        thread.join(timeout=20)
        if thread.is_alive():
            log_event("playback_capture", "direct_download_stop_failed",
                      "직접 다운로드 스레드가 종료되지 않았습니다.", level="ERROR", capture_dir=str(self.out_dir))
        else:
            self._direct_download_thread = None

    def _run_direct_download(self, url: str, content_range: str, content_type: str, dest: Path, referer: str) -> None:
        import urllib.request

        total = self._direct_total
        parsed = parse_byte_range(content_range)
        if parsed and parsed[2]:
            total = parsed[2]
            with self._direct_lock:
                self._direct_total = total

        success = False

        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
            if referer:
                req.add_header("Referer", referer)
            req.add_header("Range", "bytes=0-")
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_range = resp.headers.get("Content-Range", "")
                self._trace("direct_response", url=mask_url(url), status=resp.status,
                            content_range=resp_range, content_length=resp.headers.get("Content-Length", ""))
                p = parse_byte_range(resp_range)
                if resp.status == 206 and (p is None or p[0] != 0):
                    raise ValueError("direct response has an invalid Content-Range")
                if p and p[2]:
                    total = p[2]
                    with self._direct_lock:
                        self._direct_total = total
                elif not total:
                    cl = resp.headers.get("Content-Length", "")
                    if cl.isdigit():
                        total = int(cl)
                        with self._direct_lock:
                            self._direct_total = total

                with dest.open("wb") as handle:
                    while True:
                        if getattr(self, "tap_cancel", False):
                            break
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        with self._direct_lock:
                            self._direct_bytes += len(chunk)
                            self.last_range_received = f"bytes 0-{self._direct_bytes - 1}/{total or '*'}"
                success = bool(
                    self._direct_bytes > 0 and not self.tap_cancel
                    and (resp.status != 206 or (p is not None and p[2] is not None))
                )
        except Exception as exc:
            success = False
            detail = f"direct_download:{type(exc).__name__}: {mask_text(str(exc))[:200]}"
            self._note_error(detail)
            log_event("playback_capture", "direct_download_error", detail, level="ERROR", capture_dir=str(self.out_dir))
            self._trace("direct_error", url=mask_url(url), error_type=type(exc).__name__,
                        error=mask_text(str(exc))[:300], status=getattr(exc, "code", None))

        with self._direct_lock:
            final_bytes = self._direct_bytes
            final_total = self._direct_total or (final_bytes if success else 0)
            self._direct_total = final_total
            self._direct_complete = bool(success and final_total and final_bytes == final_total)
        self._trace("direct_finished", url=mask_url(url), bytes=final_bytes, total=final_total,
                    complete=self._direct_complete, cancelled=self.tap_cancel)

        if final_bytes > 0:
            with self._io_lock:
                item = {
                    "url": url,
                    "status": 200 if self._direct_complete else 206,
                    "content_type": content_type or "video/mp4",
                    "content_range": f"bytes 0-{final_bytes - 1}/{final_total or '*'}",
                    "request_range": "bytes=0-",
                    "path": str(dest),
                    "size": final_bytes,
                    "truncated": not self._direct_complete,
                    "via": "direct_stream",
                }
                if self._direct_complete:
                    self.files = [f for f in self.files if f.get("url") != url]
                self.files.append(item)

    def _fetch_media_direct(self, url: str, content_range: str = "", referer: str = "") -> bytes:
        """포착된 미디어 URL에 대해 브라우저 세션/헤더 기반으로 직접 본문을 수신한다."""
        import urllib.request
        range_header = request_range_from_response(content_range)
        self._trace("fallback_start", url=mask_url(url), request_range=range_header,
                    content_range=content_range)
        if content_range and not range_header:
            log_event("playback_capture", "direct_range_error", "잘못된 Range 헤더", level="ERROR",
                      capture_dir=str(self.out_dir))
            self._trace("fallback_range_error", url=mask_url(url), content_range=content_range)
            return b""

        def validate(body: bytes, status: int, response_range: str) -> bytes:
            if range_header and status != 206:
                raise ValueError(f"Range 요청에 HTTP {status} 응답")
            expected = parse_byte_range(content_range)
            actual = parse_byte_range(response_range)
            if expected and len(body) != expected[1] - expected[0]:
                raise ValueError("Range 응답 본문 길이 불일치")
            if expected and actual and actual[:2] != expected[:2]:
                raise ValueError("Content-Range 응답 범위 불일치")
            return body

        try:
            req = urllib.request.Request(url)
            if range_header:
                req.add_header("Range", range_header)
            if referer:
                req.add_header("Referer", referer)
            req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = validate(resp.read(), resp.status, resp.headers.get("Content-Range", ""))
                self._trace("fallback_direct_finished", url=mask_url(url), status=resp.status, bytes=len(body))
                return body
        except Exception as exc:
            detail = f"direct_fetch:{type(exc).__name__}: {mask_text(str(exc))[:200]}"
            log_event("playback_capture", "direct_fetch_error", detail, level="ERROR", capture_dir=str(self.out_dir))
            self._trace("fallback_direct_error", url=mask_url(url), error_type=type(exc).__name__,
                        error=mask_text(str(exc))[:300], status=getattr(exc, "code", None))

        # 파이썬 직접 연결 실패 시 브라우저 evaluate fetch 시도
        if self._page and not self._page.is_closed():
            try:
                raw_b64 = self._page.evaluate(
                    """async ([targetUrl, rangeHeader]) => {
                            const opts = { credentials: 'include' };
                            if (rangeHeader) opts.headers = { 'Range': rangeHeader };
                            const r = await fetch(targetUrl, opts);
                            if (!r.ok) throw new Error(`HTTP ${r.status}`);
                            const buf = await r.arrayBuffer();
                            const arr = new Uint8Array(buf);
                            let binary = '';
                            const chunk = 8192;
                            for (let i = 0; i < arr.length; i += chunk) {
                                binary += String.fromCharCode.apply(null, arr.subarray(i, i + chunk));
                            }
                            return {body: btoa(binary), status: r.status, range: r.headers.get('content-range') || ''};
                    }""",
                    [url, range_header],
                )
                if raw_b64 and raw_b64.get("body"):
                    body = validate(base64.b64decode(raw_b64["body"]), int(raw_b64["status"]), raw_b64["range"])
                    self._trace("fallback_browser_finished", url=mask_url(url), status=int(raw_b64["status"]),
                                content_range=raw_b64["range"], bytes=len(body))
                    return body
                self._trace("fallback_browser_empty", url=mask_url(url),
                            status=raw_b64.get("status") if raw_b64 else None)
            except Exception as exc:
                detail = f"browser_fetch:{type(exc).__name__}: {mask_text(str(exc))[:200]}"
                log_event("playback_capture", "browser_fetch_error", detail, level="ERROR",
                          capture_dir=str(self.out_dir))
                self._trace("fallback_browser_error", url=mask_url(url), error_type=type(exc).__name__,
                            error=mask_text(str(exc))[:300])
        else:
            self._trace("fallback_browser_unavailable", url=mask_url(url))
        return b""

    def _start_stream(self, sid: int, request_id: str) -> None:
        key = self._meta_key(sid, request_id)
        if key not in self._cdp_meta or key in self._stream_attempted:
            return
        self._stream_attempted.add(key)
        session = self._cdp_session_by_id.get(sid)
        if session is None:
            return
        try:
            result = session.send("Network.streamResourceContent", {"requestId": request_id}) or {}
            buffered = result.get("bufferedData") or ""
            if buffered:
                raw = base64.b64decode(buffered)
                self._write_chunk(sid, request_id, raw)
            self._trace("cdp_stream_started", session_id=sid, request_id=request_id,
                        buffered_bytes=len(raw) if buffered else 0)
        except Exception as exc:
            detail = f"stream_body:{type(exc).__name__}: {mask_text(str(exc))[:200]}"
            self._note_error(detail)
            log_event("playback_capture", "stream_body_error", detail, level="ERROR",
                      capture_dir=str(self.out_dir))
            slot = self._cdp_meta.get(key) or {}
            # 요청 시작을 보지 못한 세션은 그 본문을 받을 수 없다. 세션이 요청보다 늦게 붙은 경우다.
            tracked = "method" in slot
            self._trace("cdp_stream_error", session_id=sid, request_id=request_id,
                        error_type=type(exc).__name__, error=mask_text(str(exc))[:300],
                        request_tracked=tracked)
            self._queue_browser_fallback(slot)
            url = str(slot.get("url") or "")
            if not tracked and url and not self._player_reloaded and url not in self._untracked_media:
                self._untracked_media.append(url)

    def reload_untracked_player(self) -> bool:
        """CDP 연결 전에 시작된 미디어 요청이 있으면 그 프레임을 한 번 다시 불러온다.

        다시 불러온 뒤의 요청은 시작부터 관찰되므로 본문 스트림을 받을 수 있다.
        """
        if self._player_reloaded:
            return False
        for url in self._untracked_media:
            frame = self._media_frames.get(url)
            if frame is None:
                continue
            self._player_reloaded = True
            try:
                frame.evaluate("() => { location.reload(); }")
            except Exception as exc:
                detail = f"player_reload:{type(exc).__name__}: {mask_text(str(exc))[:200]}"
                self._note_error(detail)
                self._trace("player_reload_error", url=mask_url(url), error=detail)
                log_event("playback_capture", "player_reload_error", detail, level="ERROR",
                          capture_dir=str(self.out_dir))
                return False
            # 다시 불러오면 이전 문서의 미디어 주소는 더 쓰이지 않는다.
            self._untracked_media.clear()
            self._browser_fallback_queue.clear()
            self._browser_fallback_failed = False
            self._trace("player_reloaded", url=mask_url(url))
            return True
        return False

    def frame_observed(self, frame) -> bool:
        """문서를 연 프레임의 네트워크 이벤트를 받을 CDP 세션이 준비됐는지."""
        url = str(getattr(frame, "url", "") or "")
        if not url.startswith(("http://", "https://")):
            return False
        key = self._frame_key(frame)
        return key in self._attached_ids or self._skipped_frame_url.get(key) == url

    def _queue_browser_fallback(self, slot: dict[str, Any]) -> None:
        url = str(slot.get("url") or "")
        parsed = parse_byte_range(str(slot.get("content_range") or ""))
        if not url or url in self._browser_fallback_urls or parsed is None or not parsed[2]:
            return
        self._browser_fallback_urls.add(url)
        self._browser_fallback_queue.append({
            "url": url, "total": parsed[2], "offset": 0,
            "content_type": str(slot.get("content_type") or ""),
            # 요청 시작을 본 경우에만 fragment 유무가 확정된다. 모르면 플레이어 DOM에서 찾는다.
            "fragment": slot.get("fragment"),
        })
        self._trace("browser_stream_queued", url=mask_url(url), total=parsed[2])

    def _drain_browser_fallback(self) -> None:
        if not self._browser_fallback_queue:
            return
        item = self._browser_fallback_queue.pop(0)
        url = item["url"]
        progress = self.media_progress()
        if progress["complete"] and progress["url"] == url:
            return
        frame = self._media_frames.get(url)
        if frame is None:
            if not item.get("waiting_for_frame"):
                item["waiting_for_frame"] = True
                self._trace("browser_stream_waiting_for_frame", url=mask_url(url))
            self._browser_fallback_queue.append(item)
            return
        start = item["offset"]
        end = min(start + BROWSER_FALLBACK_CHUNK_BYTES, item["total"]) - 1
        try:
            if not item.get("resolved"):
                item["resolved"] = True
                if item["fragment"] is None:
                    item["fragment"] = ""
                    found = frame.evaluate(_MEDIA_FRAGMENT_JS, url)
                    if isinstance(found, str) and found.startswith("#"):
                        item["fragment"] = found
                # fragment 값에는 실제 영상 위치가 들어 있을 수 있어 유무만 남긴다.
                self._trace("browser_stream_source", url=mask_url(url), has_fragment=bool(item["fragment"]))
            response = frame.evaluate(_BROWSER_RANGE_FETCH_JS, [url + item["fragment"], start, end])
            if not isinstance(response, dict) or response.get("status") != 206:
                raise ValueError("invalid browser Range response")
            content_range = str(response.get("content_range") or "")
            parsed = parse_byte_range(content_range)
            body = base64.b64decode(response.get("body") or "", validate=True)
            if parsed != (start, end + 1, item["total"]) or len(body) != end - start + 1:
                raise ValueError("browser Range response does not match the requested bytes")
            self._store(url, 206, item["content_type"], content_range, f"bytes={start}-{end}", body, "media")
        except Exception as exc:
            detail = f"{type(exc).__name__}: {mask_text(str(exc))[:300]}"
            self._browser_fallback_failed = True
            self._note_error(f"browser_stream:{detail.splitlines()[0][:200]}")
            self._trace("browser_stream_error", url=mask_url(url), request_range=f"bytes={start}-{end}",
                        error=detail, has_fragment=bool(item["fragment"]))
            log_event("playback_capture", "browser_stream_error", detail, level="ERROR",
                      capture_dir=str(self.out_dir), media_url=url)
            return
        self._trace("browser_stream_chunk", url=mask_url(url), content_range=content_range, bytes=len(body))
        item["offset"] = end + 1
        if item["offset"] < item["total"]:
            self._browser_fallback_queue.append(item)

    def _on_cdp_response(self, sid: int, event: dict[str, Any]) -> None:
        self.cdp_responses += 1
        self.response_count += 1
        response = event.get("response") or {}
        request_id = str(event.get("requestId") or "")
        url = str(response.get("url") or "")
        slot = self._cdp_meta.setdefault(self._meta_key(sid, request_id), {})
        slot["url"] = url or slot.get("url") or ""
        slot["status"] = int(response.get("status") or 0)
        headers = response.get("headers")
        slot["content_type"] = response.get("mimeType") or _cdp_header(headers, "content-type")
        slot["content_range"] = _cdp_header(headers, "content-range")
        slot["content_length"] = _cdp_header(headers, "content-length")
        slot["resource_type"] = _cdp_resource_type(str(event.get("type") or slot.get("resource_type") or ""))
        if slot.get("method") == "OPTIONS" or slot["resource_type"] == "preflight":
            return
        interesting = self._response_might_be_media(
            slot["url"], slot["resource_type"], slot["content_type"], slot["content_range"],
        ) or looks_like_playback_request(slot["url"], slot["resource_type"], {"range": slot.get("request_range", "")})
        if interesting:
            self.media_seen += 1
            known = parse_byte_range(slot["content_range"])
            if known and known[2]:
                self.expected_total = max(self.expected_total, known[2])
            self._trace("cdp_response", session_id=sid, request_id=request_id, url=mask_url(slot["url"]),
                        status=slot["status"], resource_type=slot["resource_type"],
                        content_type=slot["content_type"], content_range=slot["content_range"],
                        content_length=slot["content_length"], request_range=slot.get("request_range") or "",
                        protocol=response.get("protocol") or "",
                        from_disk_cache=bool(response.get("fromDiskCache")),
                        from_service_worker=bool(response.get("fromServiceWorker")),
                        request_tracked="method" in slot)
        self._sample(self.response_samples, {
            "url": slot["url"],
            "status": slot["status"],
            "resource_type": slot["resource_type"],
            "content_type": slot["content_type"],
            "content_range": slot["content_range"],
            "range": slot.get("request_range") or "",
            "interesting": interesting,
            "via": "cdp",
        }, interesting=interesting)
        if interesting and request_id and slot["status"] in (200, 206):
            self._stream_queue.append((sid, request_id))
            if looks_like_playback_request(slot["url"], slot["resource_type"], {"range": slot.get("request_range", "")}):
                self._start_direct_media_download(slot["url"], slot.get("content_range") or "", slot.get("content_type") or "")

    def _on_cdp_failed(self, sid: int, event: dict[str, Any]) -> None:
        request_id = str(event.get("requestId") or "")
        key = self._meta_key(sid, request_id)
        self._stream_attempted.discard(key)
        slot = self._cdp_meta.get(key) or {}
        if not slot:
            return
        if slot.get("method") == "OPTIONS" or slot.get("resource_type") == "preflight":
            return
        url = str(slot.get("url") or "")
        status = int(slot.get("status") or 0)
        ctype = str(slot.get("content_type") or "")
        content_range = str(slot.get("content_range") or "")
        request_range = str(slot.get("request_range") or "")
        resource_type = str(slot.get("resource_type") or "")
        if looks_like_playback_request(url, resource_type, {"range": request_range}):
            self._trace("cdp_loading_failed", session_id=sid, request_id=request_id, url=mask_url(url),
                        status=status, error=mask_text(str(event.get("errorText") or ""))[:300],
                        cancelled=bool(event.get("canceled")), blocked_reason=event.get("blockedReason") or "",
                        cors_error=event.get("corsErrorStatus") or {}, data_events=slot.get("data_events", 0),
                        network_data_bytes=slot.get("network_data_bytes", 0),
                        payload_bytes=slot.get("payload_bytes", 0))
        if status in (200, 206) and looks_like_playback_request(url, resource_type, {"range": request_range}):
            self._start_direct_media_download(url, content_range, ctype)
        chunk = self._take_chunk(sid, request_id)
        if chunk and should_save_media(url, status or 206, ctype, chunk, resource_type):
            self._store(url, status or 206, ctype, content_range, request_range, chunk, resource_type)
            self._cdp_meta.pop(key, None)
            return
        if looks_like_playback_request(url, resource_type, {"range": request_range}):
            detail = ", ".join(
                f"{key}={event[key]}" for key in ("errorText", "canceled", "blockedReason", "corsErrorStatus")
                if key in event
            )
            self._note_skipped(url, status, ctype, 0, "cdp_loading_failed", detail)
        self._cdp_meta.pop(key, None)

    def _on_cdp_finished(self, session, sid: int, event: dict[str, Any]) -> None:
        self.cdp_finished += 1
        request_id = str(event.get("requestId") or "")
        key = self._meta_key(sid, request_id)
        self._stream_attempted.discard(key)
        slot = self._cdp_meta.pop(key, None)
        if not slot:
            return
        if slot.get("method") == "OPTIONS" or slot.get("resource_type") == "preflight":
            return
        url = str(slot.get("url") or "")
        status = int(slot.get("status") or 0)
        if self._response_might_be_media(url, slot.get("resource_type") or "",
                                         slot.get("content_type") or "", slot.get("content_range") or ""):
            self._trace("cdp_loading_finished", session_id=sid, request_id=request_id, url=mask_url(url),
                        status=status, encoded_data_length=event.get("encodedDataLength"),
                        data_events=slot.get("data_events", 0), network_data_bytes=slot.get("network_data_bytes", 0),
                        payload_bytes=slot.get("payload_bytes", 0))
        if status not in (200, 206):
            return
        ctype = str(slot.get("content_type") or "")
        content_range = str(slot.get("content_range") or "")
        content_length = str(slot.get("content_length") or "")
        request_range = str(slot.get("request_range") or "")
        resource_type = str(slot.get("resource_type") or "")
        if not self._response_might_be_media(url, resource_type, ctype, content_range):
            if not looks_like_playback_request(url, resource_type, {"range": request_range}):
                return
        claim = self._claim_key(url, "", "", request_id)
        if claim and claim in self._claimed:
            return
        if claim:
            self._claimed.add(claim)
        self._cdp_queue.append({
            "session": session,
            "request_id": request_id,
            "url": url,
            "status": status,
            "content_type": ctype,
            "content_range": content_range,
            "content_length": content_length,
            "request_range": request_range,
            "resource_type": resource_type,
            "claim": claim,
            "sid": sid,
        })

    def _on_request(self, request) -> None:
        self.request_count += 1
        try:
            url = request.url
            resource_type = getattr(request, "resource_type", "") or ""
            headers = getattr(request, "headers", None)
            request_range = _header(headers, "range")
        except Exception:
            return
        interesting = (
            str(getattr(request, "method", "")).upper() != "OPTIONS"
            and resource_type.lower() != "preflight"
            and looks_like_playback_request(url, resource_type, headers)
        )
        self._sample(self.request_samples, {
            "url": url,
            "method": getattr(request, "method", "") or "",
            "resource_type": resource_type,
            "range": request_range,
            "interesting": interesting,
        }, interesting=interesting)

    def _on_response(self, response) -> None:
        """응답 헤더만 기록한다. 본문은 응답 완료 뒤에만 읽는다."""
        self.response_count += 1
        try:
            request = response.request
            url = response.url
            status = int(response.status)
            resource_type = getattr(request, "resource_type", "") or ""
            ctype = _header(response.headers, "content-type")
            content_range = _header(response.headers, "content-range")
            request_range = _header(getattr(request, "headers", None), "range")
        except Exception:
            return
        interesting = (
            str(getattr(request, "method", "")).upper() != "OPTIONS"
            and resource_type.lower() != "preflight"
            and self._response_might_be_media(url, resource_type, ctype, content_range)
        )
        if interesting:
            self.media_seen += 1
            try:
                self._media_frames[url] = request.frame
            except Exception as exc:
                self._trace("media_frame_error", url=mask_url(url), error_type=type(exc).__name__,
                            error=mask_text(str(exc))[:300])
        self._sample(self.response_samples, {
            "url": url,
            "status": status,
            "resource_type": resource_type,
            "content_type": ctype,
            "content_range": content_range,
            "range": request_range,
            "interesting": interesting,
        }, interesting=interesting)
        if interesting and status in (200, 206) and looks_like_playback_request(url, resource_type, {"range": request_range}):
            self._start_direct_media_download(url, content_range or "", ctype or "")
    def _on_request_finished(self, request) -> None:
        """CDP가 없는 경우에만 완료된 응답 본문을 fallback으로 읽는다."""
        if self._cdp_sessions:
            return
        self._response_queue.append(request)

    def _take_chunk(self, sid: int | None, request_id: str) -> bytes:
        keys = [self._meta_key(sid, request_id)] if sid is not None else []
        keys.extend(key for key in self._chunk_paths if key[1] == request_id and key not in keys)
        body = b""
        for key in keys:
            path = self._chunk_paths.pop(key, None)
            if path is None or not path.is_file():
                continue
            try:
                raw = path.read_bytes()
            except Exception:
                raw = b""
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            if len(raw) > len(body):
                body = raw
        return body

    def _read_cdp_body(self, item: dict[str, Any]) -> None:
        self.chunks_attempted += 1
        session = item["session"]
        request_id = item["request_id"]
        url = item["url"]
        status = item["status"]
        ctype = item["content_type"]
        content_range = item["content_range"]
        content_length = item.get("content_length") or ""
        request_range = item["request_range"]
        resource_type = item["resource_type"]
        claim = item["claim"]
        body = self._take_chunk(item.get("sid"), request_id)
        source = "stream" if body else ""
        expected_length = None
        parsed_range = parse_byte_range(content_range)
        if parsed_range is not None:
            expected_length = parsed_range[1] - parsed_range[0]
        elif content_length.isdigit():
            expected_length = int(content_length)
        error_name = ""
        error_detail = ""
        if not body or (expected_length is not None and len(body) < expected_length):
            try:
                payload = session.send("Network.getResponseBody", {"requestId": request_id})
                raw = (payload or {}).get("body") or ""
                decoded = base64.b64decode(raw) if (payload or {}).get("base64Encoded") else raw.encode("utf-8", errors="replace")
                if len(decoded) > len(body):
                    body = decoded
                    source = "cdp_body"
            except Exception as exc:
                error_name = type(exc).__name__
                error_detail = str(exc)
                self._trace("cdp_body_error", session_id=item.get("sid"), request_id=request_id,
                            url=mask_url(url), error_type=error_name, error=mask_text(error_detail)[:300],
                            streamed_bytes=len(body), expected_bytes=expected_length)
                if "TargetClosed" in error_name or "target closed" in str(exc).lower():
                    self.stop_reason = self.stop_reason or "target_closed"
        if not body and looks_like_playback_request(url, resource_type, {"range": request_range}):
            body = self._fetch_media_direct(url, content_range or request_range, referer=str(getattr(self._page, "url", "") or ""))
            if body:
                source = "fallback"
        self._trace("cdp_body_result", session_id=item.get("sid"), request_id=request_id, url=mask_url(url),
                    status=status, source=source or "none", bytes=len(body), expected_bytes=expected_length)
        if not body:
            self._claimed.discard(claim)
            self._note_skipped(url, status, ctype, 0, f"cdp_body_error:{error_name or 'empty'}", error_detail)
            return
        if resource_type != "media" and looks_like_playback_request(url, resource_type, {"range": request_range}):
            resource_type = "media" if not is_media_target(url, ctype) else resource_type
        self._store(url, status, ctype, content_range, request_range, body, resource_type)

    def _read_response_body(self, response) -> None:
        try:
            request = response.request
            url = response.url
            status = int(response.status)
            resource_type = getattr(request, "resource_type", "") or ""
            ctype = _header(response.headers, "content-type")
            content_range = _header(response.headers, "content-range")
            request_range = _header(getattr(request, "headers", None), "range")
        except Exception:
            return
        if str(getattr(request, "method", "")).upper() == "OPTIONS" or resource_type.lower() == "preflight":
            return
        if status not in (200, 206) or not self._response_might_be_media(url, resource_type, ctype, content_range):
            return
        claims = [
            key for key in (self._claim_key(url, request_range), self._claim_key(url, "", content_range)) if key
        ]
        if any(key in self._claimed for key in claims):
            return
        try:
            self.chunks_attempted += 1
            body = response.body()
        except Exception as exc:
            err_name = type(exc).__name__
            if "TargetClosed" in err_name or "target closed" in str(exc).lower():
                self.stop_reason = self.stop_reason or "target_closed"
            self._note_skipped(url, status, ctype, 0, f"body_error:{err_name}")
            return
        self._claimed.update(claims)
        self._store(url, status, ctype, content_range, request_range, body, resource_type)

    def _response_might_be_media(self, url: str, resource_type: str, content_type: str, content_range: str) -> bool:
        if looks_like_playback_request(url, resource_type) or is_media_target(url, content_type):
            return True
        ctype = (content_type or "").lower()
        return bool(content_range) and ("octet-stream" in ctype or ctype.startswith("video/") or ctype.startswith("audio/"))

    def _note_skipped(self, url: str, status: int, content_type: str, size: int, reason: str,
                      detail: str = "") -> None:
        detail = mask_text(detail)[:300]
        self.skipped_count += 1
        if len(self.skipped) < 80:
            self.skipped.append({
                "url": url,
                "status": status,
                "content_type": content_type,
                "size": size,
                "reason": reason,
                "detail": detail,
            })
        if self._skipped_logs < 40:
            self._skipped_logs += 1
            log_event(
                "playback_capture",
                "packet_skipped",
                f"{reason} {detail} status={status} {size} bytes {mask_url(url)}",
                media_url=url,
                status=status,
                bytes=size,
                content_type=content_type,
                reason=reason,
                detail=detail,
            )

    def _store(
        self,
        url: str,
        status: int,
        ctype: str,
        content_range: str,
        request_range: str,
        body: bytes,
        resource_type: str,
    ) -> None:
        content_range, truncated = clamp_byte_range(content_range, len(body))
        key = f"{url}|{request_range}|{content_range}|{len(body)}"
        if key in self._seen:
            self._note_skipped(url, status, ctype, len(body), "duplicate")
            return
        if not should_save_media(url, status, ctype, body, resource_type):
            reason = "empty" if not body else f"status_{status}"
            if status in (200, 206) and body:
                reason = "not_media"
            self._note_skipped(url, status, ctype, len(body), reason)
            return
        with self._io_lock:
            if key in self._seen:
                self._note_skipped(url, status, ctype, len(body), "duplicate")
                return
            self._seen.add(key)
            path = self.out_dir / saved_media_name(url, len(self.files) + 1, content_range)
            path.write_bytes(body)
            item = {
                "url": url,
                "status": status,
                "content_type": ctype,
                "content_range": content_range,
                "request_range": request_range,
                "path": str(path),
                "size": len(body),
                "truncated": truncated,
            }
            self.files.append(item)
            if content_range:
                self.last_range_received = content_range
            elif request_range:
                self.last_range_received = request_range
        if self._saved_logs < 80:
            self._saved_logs += 1
            log_event(
                "playback_capture",
                "packet_saved",
                f"{status} {len(body)} bytes {mask_url(url)}",
                media_url=url,
                status=status,
                bytes=len(body),
                content_type=ctype,
                content_range=content_range,
                request_range=request_range,
                output=path.name,
            )

    def _handle(self, route) -> None:
        request = route.request
        url = request.url
        resource_type = getattr(request, "resource_type", "") or ""
        headers = getattr(request, "headers", None)
        request_range = _header(headers, "range")
        self.seen_count += 1
        interesting = looks_like_playback_request(url, resource_type, headers)
        self._sample(self.seen_samples, {
            "url": url,
            "resource_type": resource_type,
            "range": request_range,
            "interesting": interesting,
        }, interesting=interesting)
        if not looks_like_playback_request(url, resource_type, headers):
            route.continue_()
            return
        claim = self._claim_key(url, request_range)
        if claim:
            self._claimed.add(claim)
        fetched = None
        body = b""
        try:
            fetched = route.fetch()
            body = fetched.body()
            ctype = _header(fetched.headers, "content-type")
            content_range = _header(fetched.headers, "content-range")
            status = int(fetched.status)
        except Exception as exc:
            self._claimed.discard(claim)
            self._note_skipped(url, 0, "", 0, f"fetch_error:{type(exc).__name__}")
            try:
                route.continue_()
            except Exception:
                pass
            return
        try:
            range_claim = self._claim_key(url, "", content_range)
            if range_claim:
                self._claimed.add(range_claim)
            self._store(url, status, ctype, content_range, request_range, body, resource_type)
        except Exception as exc:
            self._note_skipped(url, status, ctype, len(body), f"write_error:{type(exc).__name__}")
        try:
            route.fulfill(response=fetched, body=body)
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass


def capture_playback(page, out_dir: Path, play, *, timeout_ms: int = 8000) -> dict[str, Any]:
    """play()로 재생을 시작한 뒤, 그 사이에 도착한 미디어 본문을 out_dir에 저장한다."""
    capture = PlaybackCapture(out_dir)
    capture.install(page)
    try:
        play()
        waited = 0
        limit = max(0, timeout_ms)
        while waited < limit:
            interval = min(300, limit - waited)
            page.wait_for_timeout(interval)
            capture.drain()
            waited += interval
        return capture.summary()
    finally:
        capture.uninstall(page)
