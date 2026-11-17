"""재생 중에 브라우저가 받은 미디어 응답을 파일로 남긴다.

직접 URL 재요청이 403이어도, 페이지가 재생하면서 받은 본문은 그 응답에만 있을 수 있다.
그때는 같은 URL을 다시 받지 않고 이미 도착한 응답 바이트를 저장한다.
이 플레이어의 본편은 Range 요청의 206일 수 있으므로 200과 206을 모두 남긴다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from diagnostics import log_event, mask_url, sanitize

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


def is_media_target(url: str, content_type: str = "") -> bool:
    path = urlsplit(url).path.lower()
    ctype = (content_type or "").lower()
    return path.endswith(MEDIA_EXTENSIONS) or any(token in ctype for token in MEDIA_TYPES)


def looks_like_playback_request(url: str, resource_type: str = "") -> bool:
    """응답 본문을 보기 전에, 재생 요청으로 가로챌 후보인지 판정한다."""
    path = urlsplit(url).path.lower()
    return is_media_target(url) or resource_type == "media" or "videoplayback" in path


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


def saved_media_name(url: str, index: int, content_range: str = "") -> str:
    """쿼리(일회용 토큰)는 파일명에 넣지 않는다. Range가 다르면 파일도 나눈다."""
    stem = Path(urlsplit(url).path).name or f"media_{index}"
    stem = _NAME_RE.sub("_", stem).strip("._")[:80] or f"media_{index}"
    tag = _NAME_RE.sub("_", content_range).strip("._")[:40]
    if tag:
        return f"{index:03d}_{tag}_{stem}"
    return f"{index:03d}_{stem}"


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
    """재생 요청을 가로채 응답 바이트를 저장한 뒤 플레이어에 그대로 전달한다.

    동기 Playwright에서 response.body()를 나중에 읽으면 멈출 수 있어, route.fetch()로 받는다.
    설치는 재생 클릭보다 먼저 해야 한다. 버튼 확인이 끝난 뒤에 켜면 이미 지나간 본문은 없다.
    """

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.files: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self.skipped_count = 0
        self._seen: set[str] = set()
        self._page = None
        self._saved_logs = 0
        self._skipped_logs = 0

    def install(self, page) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._page = page
        page.route("**/*", self._handle)
        log_event(
            "playback_capture",
            "installed",
            "재생 패킷 캡처를 재생 시작 전에 설치",
            capture_dir=str(self.out_dir),
        )

    def uninstall(self, page=None) -> None:
        page = page or self._page
        if page is None:
            return
        try:
            page.unroute("**/*", self._handle)
        except Exception:
            pass
        self._page = None

    def discard(self) -> None:
        """download 버튼이 있어 패킷이 필요 없을 때 임시 파일을 지운다."""
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

    def summary(self) -> dict[str, Any]:
        payload = {
            "ok": bool(self.files),
            "files": list(self.files),
            "skipped": list(self.skipped),
            "skipped_count": self.skipped_count,
            "bytes": sum(item["size"] for item in self.files),
            "capture_dir": str(self.out_dir),
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
        )
        return payload

    def _note_skipped(self, url: str, status: int, content_type: str, size: int, reason: str) -> None:
        self.skipped_count += 1
        if len(self.skipped) < 80:
            self.skipped.append({
                "url": url,
                "status": status,
                "content_type": content_type,
                "size": size,
                "reason": reason,
            })
        if self._skipped_logs < 40:
            self._skipped_logs += 1
            log_event(
                "playback_capture",
                "packet_skipped",
                f"{reason} status={status} {size} bytes {mask_url(url)}",
                media_url=url,
                status=status,
                bytes=size,
                content_type=content_type,
                reason=reason,
            )

    def _handle(self, route) -> None:
        request = route.request
        url = request.url
        resource_type = getattr(request, "resource_type", "") or ""
        if not looks_like_playback_request(url, resource_type):
            route.continue_()
            return
        request_range = _header(getattr(request, "headers", None), "range")
        try:
            fetched = route.fetch()
            body = fetched.body()
            ctype = _header(fetched.headers, "content-type")
            content_range = _header(fetched.headers, "content-range")
            status = int(fetched.status)
        except Exception as exc:
            self._note_skipped(url, 0, "", 0, f"fetch_error:{type(exc).__name__}")
            try:
                route.continue_()
            except Exception:
                pass
            return
        key = f"{url}|{request_range}|{content_range}"
        try:
            if key in self._seen:
                self._note_skipped(url, status, ctype, len(body), "duplicate")
            elif should_save_media(url, status, ctype, body, resource_type):
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
                }
                self.files.append(item)
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
            else:
                reason = "empty" if not body else f"status_{status}"
                if status in (200, 206) and body:
                    reason = "not_media"
                self._note_skipped(url, status, ctype, len(body), reason)
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
        page.wait_for_timeout(min(timeout_ms, 1500))
    finally:
        capture.uninstall(page)
    return capture.summary()
