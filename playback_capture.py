"""재생 중에 브라우저가 받은 미디어 응답을 파일로 남긴다.

직접 URL 재요청이 403이어도, 페이지가 재생하면서 받은 본문은 그 응답에만 있을 수 있다.
그때는 같은 URL을 다시 받지 않고 이미 도착한 응답 바이트를 저장한다.
이 플레이어의 본편은 Range 요청의 206일 수 있으므로 200과 206을 모두 남긴다.
page.route("**/*") 는 교차 출처 iframe 요청을 멈추고 핸들러에 안 넘길 수 있다.
그러면 재생이 시작되지 않고 다운로드 버튼도 생기지 않는다.
그래서 라우트는 걸지 않고, 페이지와 iframe CDP 세션의 Network.getResponseBody 로 본문을 읽는다.
같은 세션에서만 보이는 응답은 response 이벤트로 보완한다.
"""
from __future__ import annotations

import base64
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
        self.capture_mode = "cdp"
        self.cdp_sessions = 0
        self.cdp_responses = 0
        self.cdp_finished = 0
        self.frame_attach_skipped = 0
        self.frame_errors: list[str] = []
        self._seen: set[str] = set()
        self._claimed: set[str] = set()
        self._page = None
        self._attached_ids: set[int] = set()
        self._skipped_frame_url: dict[int, str] = {}
        self._cdp_sessions: list[Any] = []
        self._cdp_meta: dict[tuple[int, str], dict[str, Any]] = {}
        self._cdp_queue: list[dict[str, Any]] = []
        self._response_queue: list[Any] = []
        self._draining = False
        self._saved_logs = 0
        self._skipped_logs = 0

    def install(self, page) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._page = page
        # page.route("**/*") 는 OOPIF 요청을 핸들러 없이 멈출 수 있어 설치하지 않는다.
        try:
            page.on("frameattached", self._on_frame_attached)
            page.on("framenavigated", self._on_frame_navigated)
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
        self.attach_frames(page)
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

    def uninstall(self, page=None) -> None:
        page = page or self._page
        if page is None:
            return
        try:
            page.remove_listener("frameattached", self._on_frame_attached)
            page.remove_listener("framenavigated", self._on_frame_navigated)
            page.remove_listener("request", self._on_request)
            page.remove_listener("response", self._on_response)
        except Exception:
            pass
        for session in self._cdp_sessions:
            try:
                session.send("Network.setCacheDisabled", {"cacheDisabled": False})
            except Exception:
                pass
            try:
                session.detach()
            except Exception:
                pass
        self._cdp_sessions.clear()
        self._page = None

    def drain(self) -> None:
        """이벤트 밖에서 본문을 읽는다. 핸들러 안에서 읽으면 동기 Playwright가 재생을 멈춘다."""
        if self._draining:
            return
        self._draining = True
        try:
            queued = self._cdp_queue
            self._cdp_queue = []
            for item in queued:
                self._read_cdp_body(item)
            responses = self._response_queue
            self._response_queue = []
            for response in responses:
                self._read_response_body(response)
        finally:
            self._draining = False

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
        self.drain()
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
            "capture_mode": self.capture_mode,
            "cdp_sessions": self.cdp_sessions,
            "cdp_responses": self.cdp_responses,
            "cdp_finished": self.cdp_finished,
            "frame_attach_skipped": self.frame_attach_skipped,
            "frame_errors": list(self.frame_errors),
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
            capture_mode=self.capture_mode,
            cdp_sessions=self.cdp_sessions,
            cdp_responses=self.cdp_responses,
            cdp_finished=self.cdp_finished,
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

    def _on_frame_attached(self, frame) -> None:
        self.attach_frames(frames=[frame])

    def _on_frame_navigated(self, frame) -> None:
        self.attach_frames(frames=[frame])

    def attach_frames(self, page=None, frames=None) -> None:
        """페이지와 교차 출처 iframe에 Network 관찰을 붙인다. 이미 붙은 대상은 다시 붙이지 않는다."""
        page = page or self._page
        targets: list[Any] = []
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

    def _try_attach(self, target) -> None:
        key = id(target)
        if key in self._attached_ids:
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
            if "separate CDP session" in text:
                self.frame_attach_skipped += 1
                self._skipped_frame_url[key] = url
                return
            self._note_frame_error(f"{type(exc).__name__}: {text[:120]}")
            return
        self._attached_ids.add(key)
        self._wire_session(session)

    def _note_frame_error(self, message: str) -> None:
        if len(self.frame_errors) < 12:
            self.frame_errors.append(message)
        if not self.cache_error:
            self.cache_error = message.split(":", 1)[0]

    def _wire_session(self, session) -> None:
        sid = id(session)
        self._cdp_sessions.append(session)
        self.cdp_sessions += 1
        try:
            session.send("Network.enable")
            session.send("Network.setCacheDisabled", {"cacheDisabled": True})
            self.cache_disabled = True
        except Exception as exc:
            self.cache_error = self.cache_error or type(exc).__name__
        try:
            session.send("Network.setBypassServiceWorker", {"bypass": True})
            self.service_worker_bypassed = True
        except Exception:
            pass
        session.on("Network.requestWillBeSent", lambda event, key=sid: self._on_cdp_request(key, event))
        session.on("Network.responseReceived", lambda event, key=sid: self._on_cdp_response(key, event))
        session.on(
            "Network.loadingFinished",
            lambda event, sess=session, key=sid: self._on_cdp_finished(sess, key, event),
        )
        session.on("Network.loadingFailed", lambda event, key=sid: self._on_cdp_failed(key, event))

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
        slot["request_range"] = _cdp_header(headers, "range")
        slot["resource_type"] = _cdp_resource_type(str(event.get("type") or slot.get("resource_type") or ""))

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
        slot["resource_type"] = _cdp_resource_type(str(event.get("type") or slot.get("resource_type") or ""))
        interesting = self._response_might_be_media(
            slot["url"], slot["resource_type"], slot["content_type"], slot["content_range"],
        ) or looks_like_playback_request(slot["url"], slot["resource_type"], {"range": slot.get("request_range", "")})
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

    def _on_cdp_failed(self, sid: int, event: dict[str, Any]) -> None:
        slot = self._cdp_meta.pop(self._meta_key(sid, str(event.get("requestId") or "")), None)
        if not slot:
            return
        url = str(slot.get("url") or "")
        if looks_like_playback_request(url, slot.get("resource_type") or "", {"range": slot.get("request_range") or ""}):
            self._note_skipped(url, int(slot.get("status") or 0), str(slot.get("content_type") or ""), 0, "cdp_loading_failed")

    def _on_cdp_finished(self, session, sid: int, event: dict[str, Any]) -> None:
        self.cdp_finished += 1
        request_id = str(event.get("requestId") or "")
        slot = self._cdp_meta.pop(self._meta_key(sid, request_id), None)
        if not slot:
            return
        url = str(slot.get("url") or "")
        status = int(slot.get("status") or 0)
        ctype = str(slot.get("content_type") or "")
        content_range = str(slot.get("content_range") or "")
        request_range = str(slot.get("request_range") or "")
        resource_type = str(slot.get("resource_type") or "")
        if not self._response_might_be_media(url, resource_type, ctype, content_range):
            if not looks_like_playback_request(url, resource_type, {"range": request_range}):
                return
        claim = self._claim_key(url, request_range)
        if claim in self._claimed:
            return
        self._claimed.add(claim)
        self._cdp_queue.append({
            "session": session,
            "request_id": request_id,
            "url": url,
            "status": status,
            "content_type": ctype,
            "content_range": content_range,
            "request_range": request_range,
            "resource_type": resource_type,
            "claim": claim,
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
        interesting = looks_like_playback_request(url, resource_type, headers)
        self._sample(self.request_samples, {
            "url": url,
            "method": getattr(request, "method", "") or "",
            "resource_type": resource_type,
            "range": request_range,
            "interesting": interesting,
        }, interesting=interesting)

    def _on_response(self, response) -> None:
        """응답 이벤트에서는 기록만 한다. body()를 여기서 읽으면 재생이 멈춘다."""
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
        if not interesting:
            return
        self._response_queue.append(response)

    def _read_cdp_body(self, item: dict[str, Any]) -> None:
        session = item["session"]
        request_id = item["request_id"]
        url = item["url"]
        status = item["status"]
        ctype = item["content_type"]
        content_range = item["content_range"]
        request_range = item["request_range"]
        resource_type = item["resource_type"]
        claim = item["claim"]
        try:
            payload = session.send("Network.getResponseBody", {"requestId": request_id})
        except Exception as exc:
            self._claimed.discard(claim)
            self._note_skipped(url, status, ctype, 0, f"cdp_body_error:{type(exc).__name__}")
            return
        raw = (payload or {}).get("body") or ""
        try:
            body = base64.b64decode(raw) if (payload or {}).get("base64Encoded") else raw.encode("utf-8", errors="replace")
        except Exception as exc:
            self._claimed.discard(claim)
            self._note_skipped(url, status, ctype, 0, f"cdp_decode_error:{type(exc).__name__}")
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
        claim = self._claim_key(url, request_range)
        if claim in self._claimed:
            return
        self._claimed.add(claim)
        try:
            body = response.body()
        except Exception as exc:
            self._claimed.discard(claim)
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
        waited = 0
        limit = min(timeout_ms, 1500)
        while waited < limit:
            page.wait_for_timeout(min(300, limit - waited))
            capture.drain()
            waited += 300
    finally:
        capture.uninstall(page)
    return capture.summary()
