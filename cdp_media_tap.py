"""이 탭의 미디어 응답만 브라우저 CDP로 받아 파일로 남긴다.

page.route 나 Playwright 이벤트 안에서의 CDP 호출은 재생을 멈춘다.
그래서 사용자 Chrome에 별도의 소켓을 열고, 이 페이지와 그 iframe에만
응답 단계 Fetch를 건다. 본문을 읽은 뒤에는 반드시 fulfill 또는 fail 로
일시정지를 푼다. 캐시와 서비스워커는 끄지 않는다.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import socket
import ssl
import threading
import urllib.request
from typing import Any
from urllib.parse import urlparse

from diagnostics import log_event, mask_url

# 이보다 큰 본문은 한 CDP 메시지로 재생에 되돌리면 탭이 죽을 수 있다.
# 파일은 이미 디스크에 있다. 재생으로 넘기지 못하고 요청만 푼다.
FULFILL_LIMIT = 32 * 1024 * 1024
_READ_SIZE = 1024 * 1024
_MEDIA_PATTERNS = (
    "*://storage.googleapis.com/*",
    "*://*.googlevideo.com/*",
    "*mediastorage*",
    "*.mp4*",
    "*.m4v*",
    "*.m4s*",
    "*.webm*",
)


class CdpError(RuntimeError):
    pass


def browser_ws_url(cdp_http: str) -> str:
    base = (cdp_http or "").strip()
    if base.startswith(("ws://", "wss://")):
        return base
    if not base:
        raise CdpError("cdp endpoint missing")
    with urllib.request.urlopen(base.rstrip("/") + "/json/version", timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    ws_url = str(payload.get("webSocketDebuggerUrl") or "")
    if not ws_url:
        raise CdpError("webSocketDebuggerUrl missing")
    return ws_url


def encode_client_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(length.to_bytes(8, "big"))
    header.extend(mask)
    header.extend(masked)
    return bytes(header)


def decode_server_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """서버 프레임을 텍스트 payload 목록과 남은 바이트로 나눈다."""
    frames: list[bytes] = []
    offset = 0
    pending = b""
    while offset + 2 <= len(buffer):
        first = buffer[offset]
        second = buffer[offset + 1]
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        start = offset + 2
        if length == 126:
            if start + 2 > len(buffer):
                break
            length = int.from_bytes(buffer[start:start + 2], "big")
            start += 2
        elif length == 127:
            if start + 8 > len(buffer):
                break
            length = int.from_bytes(buffer[start:start + 8], "big")
            start += 8
        mask = b""
        if masked:
            if start + 4 > len(buffer):
                break
            mask = buffer[start:start + 4]
            start += 4
        if start + length > len(buffer):
            break
        data = buffer[start:start + length]
        if mask:
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        offset = start + length
        if opcode == 0x8:
            raise CdpError("websocket closed")
        if opcode in (0x1, 0x2, 0x0):
            pending += data
            if first & 0x80:
                frames.append(pending)
                pending = b""
        elif opcode == 0x9:
            frames.append(b"")  # ping placeholder is handled by the reader via opcode; keep simple
        # pong and unknown control frames are ignored
    return frames, buffer[offset:]


class CdpClient:
    """브라우저 소켓 하나. 읽기 스레드와 명령 스레드를 나눈다."""

    def __init__(self, ws_url: str):
        self._sock, leftover = _connect(ws_url)
        self._write_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next = 1
        self._pending: dict[int, queue.Queue] = {}
        self._events: queue.Queue = queue.Queue()
        self._closed = False
        self._buffer = leftover
        self._reader = threading.Thread(target=self._read_loop, name="cdp-media-read", daemon=True)
        self._reader.start()

    def send(self, method: str, params: dict | None = None, session_id: str | None = None, timeout: float = 30) -> dict:
        if self._closed:
            raise CdpError("closed")
        with self._id_lock:
            msg_id = self._next
            self._next += 1
            box: queue.Queue = queue.Queue(maxsize=1)
            self._pending[msg_id] = box
        payload: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        try:
            self._write(json.dumps(payload).encode("utf-8"))
            item = box.get(timeout=timeout)
        finally:
            with self._id_lock:
                self._pending.pop(msg_id, None)
        if isinstance(item, Exception):
            raise item
        if item.get("error"):
            err = item["error"]
            raise CdpError(str(err.get("message") or err))
        result = item.get("result")
        return result if isinstance(result, dict) else {}

    def next_event(self, timeout: float = 0.5):
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except Exception:
            pass
        with self._id_lock:
            pending = list(self._pending.items())
        for _msg_id, box in pending:
            try:
                box.put_nowait(CdpError("closed"))
            except Exception:
                pass

    def _write(self, payload: bytes) -> None:
        self._send_frame(encode_client_frame(payload))

    def _pong(self, payload: bytes) -> None:
        self._send_frame(encode_client_frame(payload, opcode=0xA))

    def _send_frame(self, frame: bytes) -> None:
        with self._write_lock:
            self._sock.sendall(frame)

    def _read_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    chunk = self._sock.recv(1024 * 1024)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                self._buffer += chunk
                frames, self._buffer = _take_frames(self._buffer, self._pong)
                for payload in frames:
                    self._dispatch(payload)
        except Exception:
            pass
        self._closed = True

    def _dispatch(self, payload: bytes) -> None:
        if not payload:
            return
        try:
            msg = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._id_lock:
                box = self._pending.get(int(msg["id"]))
            if box is not None:
                try:
                    box.put_nowait(msg)
                except Exception:
                    pass
            return
        method = str(msg.get("method") or "")
        if not method:
            return
        self._events.put((method, msg.get("params") or {}, str(msg.get("sessionId") or "")))


def _take_frames(buffer: bytes, write) -> tuple[list[bytes], bytes]:
    frames: list[bytes] = []
    offset = 0
    pending = b""
    while offset + 2 <= len(buffer):
        first = buffer[offset]
        second = buffer[offset + 1]
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        start = offset + 2
        if length == 126:
            if start + 2 > len(buffer):
                break
            length = int.from_bytes(buffer[start:start + 2], "big")
            start += 2
        elif length == 127:
            if start + 8 > len(buffer):
                break
            length = int.from_bytes(buffer[start:start + 8], "big")
            start += 8
        mask = b""
        if masked:
            if start + 4 > len(buffer):
                break
            mask = buffer[start:start + 4]
            start += 4
        if start + length > len(buffer):
            break
        data = buffer[start:start + length]
        if mask:
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        offset = start + length
        if opcode == 0x8:
            raise CdpError("websocket closed")
        if opcode == 0x9:
            try:
                write(data)
            except Exception:
                pass
            continue
        if opcode in (0x0, 0x1, 0x2):
            pending += data
            if first & 0x80:
                frames.append(pending)
                pending = b""
    return frames, buffer[offset:]


def _connect(ws_url: str) -> tuple[socket.socket, bytes]:
    parsed = urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    raw = socket.create_connection((host, port), timeout=10)
    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if parsed.scheme == "wss":
        raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    host_header = host if port in (80, 443) else f"{host}:{port}"
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    raw.sendall(handshake.encode("ascii"))
    raw.settimeout(10)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = raw.recv(4096)
        if not chunk:
            raise CdpError("websocket handshake closed")
        buf += chunk
        if len(buf) > 65536:
            raise CdpError("websocket handshake too large")
    head, rest = buf.split(b"\r\n\r\n", 1)
    status = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
    if " 101 " not in status:
        raise CdpError(f"websocket handshake rejected: {status[:80]}")
    raw.settimeout(1)
    return raw, rest


class MediaTap:
    """한 페이지와 그 하위 iframe의 미디어 본문만 저장한다."""

    def __init__(self, capture, cdp_url: str):
        self.capture = capture
        self.cdp_url = cdp_url
        self.status = ""
        self.error = ""
        self.paused = 0
        self.saved = 0
        self.released = 0
        self.sessions = 0
        self.client: CdpClient | None = None
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._bodies: queue.Queue = queue.Queue()
        self._fetch_sessions: set[str] = set()
        self._session_thread: threading.Thread | None = None
        self._body_thread: threading.Thread | None = None

    def start(self, page_target_id: str) -> None:
        try:
            if not page_target_id:
                raise CdpError("page target missing")
            self.client = CdpClient(browser_ws_url(self.cdp_url))
            self._session_thread = threading.Thread(target=self._session_loop, name="cdp-media-session", daemon=True)
            self._body_thread = threading.Thread(target=self._body_loop, name="cdp-media-body", daemon=True)
            self._session_thread.start()
            self._body_thread.start()
            self._arm_page(page_target_id)
            self.status = "armed"
        except Exception as exc:
            self.status = "failed"
            self.error = f"{type(exc).__name__}: {exc}"[:240]
            log_event(
                "playback_capture",
                "tap_failed",
                f"미디어 저장 연결 실패: {self.error}",
                level="ERROR",
                capture_dir=str(getattr(self.capture, "out_dir", "")),
            )
            self.close()

    def finish(self, abort: bool = False) -> None:
        if abort:
            self._cancel.set()
        self._stop.set()
        if self._session_thread is not None:
            self._session_thread.join(timeout=2)
        if self._body_thread is not None:
            self._body_thread.join(timeout=5 if abort else 180)
        client = self.client
        if client is not None:
            for sid in list(self._fetch_sessions):
                try:
                    client.send("Fetch.disable", {}, session_id=sid, timeout=2)
                except Exception:
                    pass
            client.close()
        self.client = None

    def close(self) -> None:
        self.finish(abort=True)

    def _arm_page(self, page_target_id: str) -> None:
        client = self.client
        if client is None:
            raise CdpError("no client")
        attached = client.send("Target.attachToTarget", {"targetId": page_target_id, "flatten": True}, timeout=10)
        session_id = str(attached.get("sessionId") or "")
        if not session_id:
            raise CdpError("page session missing")
        self._enable_fetch(session_id)
        params = {
            "autoAttach": True,
            # True면 새 iframe이 Fetch 설치 전에 미디어를 요청하지 않는다.
            # attached 처리에서 반드시 runIfWaitingForDebugger로 풀어 준다.
            "waitForDebuggerOnStart": True,
            "flatten": True,
        }
        try:
            client.send("Target.setAutoAttach", {
                **params,
                "filter": [
                    {"type": "iframe", "exclude": False},
                    {"type": "page", "exclude": False},
                    {"type": "worker", "exclude": True},
                    {"type": "service_worker", "exclude": True},
                ],
            }, session_id=session_id, timeout=10)
        except Exception:
            client.send("Target.setAutoAttach", params, session_id=session_id, timeout=10)
        self._focus(session_id)
        self._resume(session_id)
        log_event(
            "playback_capture",
            "tap_armed",
            "이 탭과 하위 iframe의 미디어 응답만 저장하도록 연결",
            capture_dir=str(self.capture.out_dir),
            sessions=self.sessions,
        )

    def _enable_fetch(self, session_id: str) -> None:
        client = self.client
        if client is None or not session_id or session_id in self._fetch_sessions:
            return
        client.send("Network.enable", {}, session_id=session_id, timeout=10)
        client.send("Fetch.enable", {
            "patterns": [
                {"urlPattern": pattern, "requestStage": "Response"}
                for pattern in _MEDIA_PATTERNS
            ],
            "handleAuthRequests": False,
        }, session_id=session_id, timeout=10)
        self._fetch_sessions.add(session_id)
        self.sessions += 1

    def _focus(self, session_id: str) -> None:
        """앞 탭으로 가져오지 않고, 백그라운드에서도 재생 요청이 줄어들지 않게 한다."""
        client = self.client
        if client is None:
            return
        for method, params in (
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ("Emulation.setIdleOverride", {"isUserActive": True, "isScreenUnlocked": True}),
        ):
            try:
                client.send(method, params, session_id=session_id, timeout=5)
            except Exception:
                continue

    def _resume(self, session_id: str) -> None:
        client = self.client
        if client is None or not session_id:
            return
        try:
            client.send("Runtime.runIfWaitingForDebugger", {}, session_id=session_id, timeout=5)
        except Exception:
            pass

    def _session_loop(self) -> None:
        while not self._stop.is_set():
            self._pump_once(0.5)
        for _ in range(30):
            if not self._pump_once(0.05):
                break

    def _pump_once(self, timeout: float) -> bool:
        client = self.client
        if client is None:
            return False
        event = client.next_event(timeout)
        if event is None:
            return False
        method, params, session_id = event
        try:
            if method == "Target.attachedToTarget":
                self._on_attached(params)
            elif method == "Fetch.requestPaused":
                self.paused += 1
                self._bodies.put((session_id, params))
        except Exception as exc:
            log_event(
                "playback_capture",
                "tap_event_error",
                f"{method} {type(exc).__name__}",
                level="ERROR",
            )
        return True

    def _on_attached(self, params: dict) -> None:
        child = str(params.get("sessionId") or "")
        info = params.get("targetInfo") or {}
        kind = str(info.get("type") or "")
        try:
            if child and kind in ("page", "iframe"):
                self._enable_fetch(child)
        finally:
            self._resume(child)

    def _body_loop(self) -> None:
        while True:
            if self._stop.is_set() and self._bodies.empty():
                return
            try:
                item = self._bodies.get(timeout=0.2)
            except queue.Empty:
                continue
            self.capture.tap_inflight += 1
            before = len(self.capture.files)
            try:
                consume_paused(self.client, item[0], item[1], self.capture)
                if len(self.capture.files) > before:
                    self.saved += 1
            except Exception as exc:
                log_event(
                    "playback_capture",
                    "tap_body_error",
                    f"{type(exc).__name__}: {exc}"[:200],
                    level="ERROR",
                    capture_dir=str(self.capture.out_dir),
                )
            finally:
                self.capture.tap_inflight = max(0, self.capture.tap_inflight - 1)
                self.released += 1


def consume_paused(client, session_id: str, params: dict, capture) -> None:
    """일시정지된 미디어 응답을 저장하고, 어떤 경우에도 요청을 풀어 준다."""
    request_id = str(params.get("requestId") or "")
    if client is None or not request_id:
        return
    request = params.get("request") or {}
    url = str(request.get("url") or "")
    status = int(params.get("responseStatusCode") or 0)
    if params.get("responseErrorReason") or status not in (200, 206):
        _release_continue(client, session_id, request_id)
        return
    headers = params.get("responseHeaders") or []
    content_type = _header_value(headers, "content-type")
    content_range = _header_value(headers, "content-range")
    request_range = _header_value(request.get("headers"), "range")
    resource_type = str(params.get("resourceType") or "media").lower()
    path = None
    try:
        path, size = _read_body(client, session_id, request_id, capture)
        saved = ""
        if size and path is not None:
            saved = capture.accept_tap_file(
                url, status, content_type, content_range, request_range, path, size, resource_type,
            )
            if saved:
                path = None
        if size and size <= FULFILL_LIMIT:
            body = b""
            source = saved or (str(path) if path is not None and path.is_file() else "")
            if source:
                with open(source, "rb") as handle:
                    body = handle.read()
            fulfill_headers = _headers_matching_body(headers, content_range, len(body))
            client.send("Fetch.fulfillRequest", {
                "requestId": request_id,
                "responseCode": status,
                "responseHeaders": fulfill_headers,
                "body": base64.b64encode(body).decode("ascii"),
            }, session_id=session_id, timeout=60)
        else:
            if size > FULFILL_LIMIT:
                log_event(
                    "playback_capture",
                    "tap_saved_without_playback",
                    f"본편 {size} bytes는 저장했다. 너무 커서 재생에는 넘기지 않고 요청만 푼다. {mask_url(url)}",
                    media_url=url,
                    bytes=size,
                )
            _release_fail(client, session_id, request_id)
    except Exception:
        _release_continue(client, session_id, request_id)
        raise
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def _headers_matching_body(headers: list, content_range: str, body_len: int) -> list:
    from playback_capture import clamp_byte_range

    adjusted, _truncated = clamp_byte_range(content_range, body_len)
    output = []
    replaced = False
    for item in headers or []:
        name = str((item or {}).get("name") or "")
        value = str((item or {}).get("value") or "")
        if name.lower() == "content-range" and adjusted:
            value = adjusted
            replaced = True
        if name.lower() == "content-length":
            value = str(body_len)
        output.append({"name": name, "value": value})
    if adjusted and not replaced:
        output.append({"name": "Content-Range", "value": adjusted})
    return output


def _read_body(client, session_id: str, request_id: str, capture) -> tuple[Any, int]:
    try:
        opened = client.send(
            "Fetch.takeResponseBodyAsStream",
            {"requestId": request_id},
            session_id=session_id,
            timeout=30,
        )
    except Exception:
        payload = client.send(
            "Fetch.getResponseBody",
            {"requestId": request_id},
            session_id=session_id,
            timeout=60,
        )
        raw = (payload or {}).get("body") or ""
        data = base64.b64decode(raw) if (payload or {}).get("base64Encoded") else raw.encode("utf-8", "replace")
        path = capture.temp_media_path(request_id)
        path.write_bytes(data)
        capture.note_tap_bytes(len(data))
        return path, len(data)
    handle = str(opened.get("stream") or "")
    if not handle:
        raise CdpError("response stream missing")
    path = capture.temp_media_path(request_id)
    size = 0
    try:
        with path.open("wb") as handle_out:
            while True:
                chunk = client.send(
                    "IO.read",
                    {"handle": handle, "size": _READ_SIZE},
                    session_id=session_id,
                    timeout=180,
                )
                data = chunk.get("data") or ""
                if data:
                    raw = base64.b64decode(data) if chunk.get("base64Encoded", True) else data.encode("utf-8", "replace")
                    handle_out.write(raw)
                    size += len(raw)
                    capture.note_tap_bytes(len(raw))
                if getattr(capture, "tap_cancel", False):
                    raise CdpError("aborted")
                if chunk.get("eof"):
                    break
    finally:
        try:
            client.send("IO.close", {"handle": handle}, session_id=session_id, timeout=10)
        except Exception:
            pass
    return path, size


def _release_continue(client, session_id: str, request_id: str) -> None:
    try:
        client.send("Fetch.continueRequest", {"requestId": request_id}, session_id=session_id, timeout=10)
    except Exception:
        _release_fail(client, session_id, request_id)


def _release_fail(client, session_id: str, request_id: str) -> None:
    try:
        client.send(
            "Fetch.failRequest",
            {"requestId": request_id, "errorReason": "Aborted"},
            session_id=session_id,
            timeout=10,
        )
    except Exception:
        pass


def _header_value(headers: Any, name: str) -> str:
    wanted = name.lower()
    if isinstance(headers, list):
        for item in headers:
            if str((item or {}).get("name") or "").lower() == wanted:
                return str((item or {}).get("value") or "")
        return ""
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return str(value or "")
    return ""
