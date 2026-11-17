"""재생 중에 브라우저가 받은 미디어 응답을 파일로 남긴다.

직접 URL 재요청이 403이어도, 페이지가 재생하면서 받은 본문은 그 응답에만 있을 수 있다.
그때는 같은 URL을 다시 받지 않고 이미 도착한 응답 바이트를 저장한다.
이 플레이어의 본편은 Range 요청의 206일 수 있으므로 200과 206을 모두 남긴다.
이미 열린 Chrome의 디스크 캐시·서비스워커 응답은 page.route에 안 잡힐 수 있다.
그때는 응답 이벤트에서 본문을 읽고, 둘 다 없으면 관찰 건수를 매니페스트에 남긴다.
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
        self._seen: set[str] = set()
        self._claimed: set[str] = set()
        self._page = None
        self._cdp = None
        self._saved_logs = 0
        self._skipped_logs = 0

    def install(self, page) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._page = page
        self.cache_disabled = self._disable_cache(page)
        try:
            page.route("**/*", self._handle)
            self.route_installed = True
        except Exception as exc:
            self.route_error = type(exc).__name__
            log_event(
                "playback_capture",
                "route_failed",
                f"route 설치 실패: {type(exc).__name__}",
                level="ERROR",
                capture_dir=str(self.out_dir),
            )
        try:
            page.on("request", self._on_request)
            page.on("response", self._on_response)
        except Exception as exc:
            log_event(
                "playback_capture",
                "listener_failed",
                f"요청/응답 관찰 설치 실패: {type(exc).__name__}",
                level="ERROR",
                capture_dir=str(self.out_dir),
            )
        log_event(
            "playback_capture",
            "installed",
            "재생 패킷 캡처를 재생 시작 전에 설치",
            capture_dir=str(self.out_dir),
            cache_disabled=self.cache_disabled,
            service_worker_bypassed=self.service_worker_bypassed,
            route_installed=self.route_installed,
        )

    def _disable_cache(self, page) -> bool:
        """이 페이지만 캐시와 서비스워커를 우회한다. 사용자 Chrome 전체를 건드리지 않는다."""
        try:
            session = page.context.new_cdp_session(page)
        except Exception as exc:
            self.cache_error = type(exc).__name__
            return False
        self._cdp = session
        ok = False
        try:
            session.send("Network.enable")
            session.send("Network.setCacheDisabled", {"cacheDisabled": True})
            ok = True
        except Exception as exc:
            self.cache_error = type(exc).__name__
        try:
            session.send("Network.setBypassServiceWorker", {"bypass": True})
            self.service_worker_bypassed = True
        except Exception:
            self.service_worker_bypassed = False
        return ok

    def uninstall(self, page=None) -> None:
        page = page or self._page
        if page is None:
            return
        try:
            page.remove_listener("request", self._on_request)
            page.remove_listener("response", self._on_response)
        except Exception:
            pass
        try:
            page.unroute("**/*", self._handle)
        except Exception:
            pass
        if self._cdp is not None:
            try:
                self._cdp.send("Network.setCacheDisabled", {"cacheDisabled": False})
            except Exception:
                pass
            try:
                self._cdp.detach()
            except Exception:
                pass
            self._cdp = None
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
            seen=self.seen_count,
            requests=self.request_count,
            responses=self.response_count,
            cache_disabled=self.cache_disabled,
            route_installed=self.route_installed,
        )
        return payload

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

    def _claim_key(self, url: str, request_range: str) -> str:
        return f"{url}|{request_range}"

    def _on_request(self, request) -> None:
        self.request_count += 1
        try:
            url = request.url
            resource_type = getattr(request, "resource_type", "") or ""
            headers = getattr(request, "headers", None)
            request_range = _header(headers, "range")
        except Exception:
            return
        interesting = looks_like_playback_request(url, resource_type, headers)
        self._sample(self.request_samples, {
            "url": url,
            "method": getattr(request, "method", "") or "",
            "resource_type": resource_type,
            "range": request_range,
            "interesting": interesting,
        }, interesting=interesting)

    def _on_response(self, response) -> None:
        """route가 캐시 때문에 못 본 재생 응답을 여기서 저장한다.

        route.fetch() 도중인 같은 요청의 body()는 동기 Playwright에서 멈출 수 있어 읽지 않는다.
        """
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
        interesting = self._response_might_be_media(url, resource_type, ctype, content_range)
        self._sample(self.response_samples, {
            "url": url,
            "status": status,
            "resource_type": resource_type,
            "content_type": ctype,
            "content_range": content_range,
            "range": request_range,
            "interesting": interesting,
        }, interesting=interesting)
        if self._claim_key(url, request_range) in self._claimed:
            return
        if not self._response_might_be_media(url, resource_type, ctype, content_range):
            return
        try:
            body = response.body()
        except Exception as exc:
            self._note_skipped(url, status, ctype, 0, f"body_error:{type(exc).__name__}")
            return
        self._store(url, status, ctype, content_range, request_range, body, resource_type)

    def _response_might_be_media(self, url: str, resource_type: str, content_type: str, content_range: str) -> bool:
        if looks_like_playback_request(url, resource_type) or is_media_target(url, content_type):
            return True
        ctype = (content_type or "").lower()
        return bool(content_range) and ("octet-stream" in ctype or ctype.startswith("video/") or ctype.startswith("audio/"))

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
        key = f"{url}|{request_range}|{content_range}"
        if key in self._seen:
            self._note_skipped(url, status, ctype, len(body), "duplicate")
            return
        if not should_save_media(url, status, ctype, body, resource_type):
            reason = "empty" if not body else f"status_{status}"
            if status in (200, 206) and body:
                reason = "not_media"
            self._note_skipped(url, status, ctype, len(body), reason)
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
        page.wait_for_timeout(min(timeout_ms, 1500))
    finally:
        capture.uninstall(page)
    return capture.summary()
