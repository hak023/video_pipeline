from __future__ import annotations

import glob
import ipaddress
import os
import pathlib
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable
from urllib.parse import urlparse

from media_tools import resolve_ffmpeg

# ---------------------------------------------------------------- defaults
DEFAULT_TOTAL_TIMEOUT = 60 * 60          # 한 파일 최대 소요 시간 (초)
DEFAULT_STALL_TIMEOUT = 90               # 출력/파일 크기 변화가 없는 최대 시간 (초)
DEFAULT_MAX_BYTES = 8 * 1024 ** 3        # 한 파일 최대 크기 (8GB)

_RESERVED = {"con", "prn", "aux", "nul",
             *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}
_EOF = object()


# ---------------------------------------------------------------- utilities
def disable_quickedit() -> None:
    """Windows 콘솔 QuickEdit 모드 해제.

    콘솔을 클릭하면 선택 모드가 되어 프로세스가 멈추고,
    엔터를 눌러야 재개되는 현상을 방지한다. main() 시작 시 1회 호출.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return
        # ENABLE_QUICK_EDIT_MODE(0x40) 제거, ENABLE_EXTENDED_FLAGS(0x80) 설정
        k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)
    except Exception:
        pass


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Windows/Linux 모두에서 안전한 파일명으로 변환."""
    s = re.sub(r'[\x00-\x1f\\/*?:"<>|]', "_", name)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.lstrip(". ")
    if len(s) > max_len:
        s = s[:max_len]
    s = s.rstrip(". ")  # Windows는 끝의 점/공백을 허용하지 않음
    if s.split(".")[0].lower() in _RESERVED:
        s = f"_{s}"
    return s or "video"


def validate_url(url: str, allow_private: bool = False) -> None:
    """http(s)만 허용하고, 내부망/루프백 대상(SSRF)을 차단."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"허용되지 않는 URL 스킴/형식: {url[:80]}")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        return  # 로컬 DNS 실패 (프록시가 해석할 수 있음) → yt-dlp에 맡김
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"내부/예약 주소 접근 차단: {p.hostname} -> {ip}")


def _clean_header(v: str) -> str:
    return re.sub(r"[\r\n\x00]", "", v or "").strip()


def _yt_dlp_cmd() -> list[str]:
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


def _kill_tree(proc: subprocess.Popen) -> None:
    """yt-dlp + 자식(ffmpeg)까지 모두 종료."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _related_files(out: pathlib.Path, safe_name: str) -> list[pathlib.Path]:
    return [pathlib.Path(p) for p in glob.glob(str(out / (glob.escape(safe_name) + ".*")))]


def _dir_size(files: list[pathlib.Path]) -> int:
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def _cleanup_partial(out: pathlib.Path, safe_name: str, since: float) -> None:
    """실패/타임아웃 시 이번 시도에서 생긴 .part, .ytdl, 조각 파일만 제거
    (이전에 완료된 동일 이름 파일은 보존)."""
    for f in _related_files(out, safe_name):
        try:
            if f.stat().st_mtime >= since - 1:
                f.unlink()
        except OSError:
            pass


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"


class _Progress:
    """한 줄짜리 진행 표시 (\\r 로 덮어쓰기)."""

    def __init__(self) -> None:
        self._last = 0.0
        self.pct = "  0.0%"
        self.speed = "-"
        self.eta = "-"
        self.status = ""

    def feed(self, line: str) -> None:
        if line.startswith("[PROG]"):
            parts = line[6:].strip().split("|")
            if len(parts) >= 3:
                self.pct, self.speed, self.eta = (x.strip() for x in parts[:3])
        elif line.startswith("["):
            self.status = line[:50]

    def render(self, size: int, elapsed: float, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < 0.5:
            return
        self._last = now
        width = max(shutil.get_terminal_size((100, 20)).columns - 1, 40)
        text = (f"  >> {self.pct:>7} | {self.speed:>11} | ETA {self.eta:>8} "
                f"| {_fmt_size(size):>9} | {int(elapsed)}s")
        sys.stdout.write("\r" + text[:width].ljust(width))
        sys.stdout.flush()

    @staticmethod
    def finish(msg: str) -> None:
        width = max(shutil.get_terminal_size((100, 20)).columns - 1, 40)
        sys.stdout.write("\r" + msg[:width].ljust(width) + "\n")
        sys.stdout.flush()


def _reader(stream, q: queue.Queue) -> None:
    try:
        for line in stream:
            q.put(line.rstrip("\r\n"))
    except Exception:
        pass
    finally:
        q.put(_EOF)


def _download_ffmpeg_direct(
    url: str,
    out_file: pathlib.Path,
    headers: dict[str, str],
    proxy: str | None = None,
    total_timeout: int = DEFAULT_TOTAL_TIMEOUT,
    stall_timeout: int = DEFAULT_STALL_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    show_progress: bool = True,
    on_progress: Callable[[str, str, str, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[bool, str]:
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        return False, "FFmpeg executable not found"

    hdr_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    cmd = [
        str(ffmpeg),
        "-y",
        "-nostdin",
        "-headers", hdr_str,
    ]
    if proxy and proxy.startswith("http"):
        cmd.extend(["-http_proxy", proxy])
    cmd.extend([
        "-i", url,
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_file),
    ])

    popen_kwargs: dict[str, Any] = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

    start = last_activity = time.monotonic()
    last_size = 0
    abort_reason: str | None = None
    tail: deque[str] = deque(maxlen=30)
    prog = _Progress()

    while True:
        try:
            line = q.get(timeout=1)
        except queue.Empty:
            line = ""

        now = time.monotonic()
        if line is _EOF:
            break
        if line:
            last_activity = now
            tail.append(line)

        size = out_file.stat().st_size if out_file.exists() else 0
        if size != last_size:
            last_size = size
            last_activity = now

        if on_progress is not None:
            on_progress("ffmpeg", "-", "-", size)
        elif show_progress:
            prog.render(size, now - start)

        if cancel is not None and cancel.is_set():
            abort_reason = "CANCELLED: 사용자 중단"
        elif size > max_bytes:
            abort_reason = f"SIZE LIMIT: {_fmt_size(size)} > {_fmt_size(max_bytes)}"
        elif now - start > total_timeout:
            abort_reason = f"TIMEOUT: 총 {total_timeout}s 초과"
        elif now - last_activity > stall_timeout:
            abort_reason = f"STALLED: {stall_timeout}s 동안 진행 없음"

        if abort_reason:
            _kill_tree(proc)
            break

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)

    ok = (abort_reason is None) and (proc.returncode == 0) and (out_file.exists() and out_file.stat().st_size > 0)
    if show_progress and on_progress is None:
        prog.finish("  [OK] FFmpeg 다운로드 완료" if ok else f"  [FAIL] {abort_reason or f'exit={proc.returncode}'}")
    return ok, "\n".join(tail)


# ---------------------------------------------------------------- main API
def _download_once(
    info: dict[str, Any],
    out_dir: str = "out",
    name: str = "video",
    proxy: str | None = None,
    total_timeout: int = DEFAULT_TOTAL_TIMEOUT,
    stall_timeout: int = DEFAULT_STALL_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_private: bool = False,
    show_progress: bool = True,
    on_progress: Callable[[str, str, str, int], None] | None = None,
    cancel: threading.Event | None = None,
    headers: dict[str, str] | None = None,
    cookie_file: str | None = None,
    events: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    out = pathlib.Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(name)

    # --- 입력 검증
    try:
        validate_url(info["url"], allow_private=allow_private)
    except ValueError as e:
        return False, f"[SECURITY] {e}"

    hdrs = {k.lower(): _clean_header(v) for k, v in (headers or {}).items() if _clean_header(v)}
    ua = hdrs.pop("user-agent", "") or _clean_header(info.get("user_agent") or "") or DEFAULT_UA
    extra_headers: list[str] = []
    for k, v in hdrs.items():
        if k in ("referer", "origin") and urlparse(v).scheme not in ("http", "https"):
            continue
        extra_headers += ["--add-header", f"{k}:{v}"]
    cookie_args = ["--cookies", cookie_file] if cookie_file else []

    ffmpeg = resolve_ffmpeg()
    ffmpeg_args = ["--ffmpeg-location", str(ffmpeg)] if ffmpeg else []

    # '%' 는 yt-dlp output template 문자이므로 이스케이프 (템플릿 인젝션 방지)
    out_tpl = str(out / f"{safe_name}.%(ext)s").replace("%", "%%").replace("%%(ext)s", "%(ext)s")

    cmd = [
        *_yt_dlp_cmd(),
        "--ignore-config",            # 외부 설정 파일(exec 등) 무시
        "--no-playlist",
        *ffmpeg_args,
        *extra_headers,
        *cookie_args,
        "--user-agent", ua,
        "--merge-output-format", "mp4",
        "--force-overwrites",
        "--retries", "5",
        "--fragment-retries", "5",
        "--socket-timeout", "30",
        "--limit-rate", "5M",
        "--max-filesize", str(max_bytes),
        "--newline",
        "--progress-template",
        "download:[PROG] %(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        "-o", out_tpl,
    ]
    if proxy:
        cmd.extend(["--proxy", proxy])
    cmd.extend(["--", info["url"]])  # '--' : URL이 옵션으로 해석되는 것 방지

    popen_kwargs: dict[str, Any] = dict(
        stdin=subprocess.DEVNULL,        # 입력 대기로 인한 멈춤 방지
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

    tail: deque[str] = deque(maxlen=30)
    prog = _Progress()
    start = last_activity = time.monotonic()
    start_wall = time.time()
    last_size = 0
    abort_reason: str | None = None

    while True:
        try:
            line = q.get(timeout=1)
        except queue.Empty:
            line = ""

        now = time.monotonic()
        if line is _EOF:
            break
        if line:
            last_activity = now
            prog.feed(line)
            if not line.startswith("[PROG]"):
                tail.append(line)

        # 파일 크기 증가 = 활동 (병합 중처럼 출력이 없는 구간도 감지)
        size = _dir_size(_related_files(out, safe_name))
        if size != last_size:
            last_size = size
            last_activity = now

        if on_progress is not None:
            on_progress(prog.pct, prog.speed, prog.eta, size)
        elif show_progress:
            prog.render(size, now - start)

        if cancel is not None and cancel.is_set():
            abort_reason = "CANCELLED: 사용자 중단"
        elif size > max_bytes:
            abort_reason = f"SIZE LIMIT: {_fmt_size(size)} > {_fmt_size(max_bytes)}"
        elif now - start > total_timeout:
            abort_reason = f"TIMEOUT: 총 {total_timeout}s 초과"
        elif now - last_activity > stall_timeout:
            abort_reason = f"STALLED: {stall_timeout}s 동안 진행 없음"

        if abort_reason:
            _kill_tree(proc)
            break

    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)

    ok = abort_reason is None and proc.returncode == 0
    if events is not None:
        events.append({"engine": "yt-dlp", "ok": ok, "returncode": proc.returncode, "abort": abort_reason,
                       "log_tail": "\n".join(tail)[-600:]})
    if not ok and ffmpeg and not (cancel and cancel.is_set()):
        log_txt = "\n".join(tail)
        # yt-dlp 가 해당 URL 을 처리하지 못한 경우(generic extractor 오류 등)에만 FFmpeg 직접 다운로드로 전환.
        # (이전의 'generic' 단순 포함 검사는 거의 모든 실패에서 참이 되어 정체/타임아웃 뒤에도 fallback 을 유발했다)
        if abort_reason is None and ("ERROR: [generic]" in log_txt or "404" in log_txt
                                     or "Unable to download webpage" in log_txt or "Unsupported URL" in log_txt):
            out_file = out / f"{safe_name}.mp4"
            remaining = int(total_timeout - (time.monotonic() - start))
            if remaining > 10:
                if show_progress and on_progress is None:
                    sys.stdout.write(f"\r  [Fallback] yt-dlp 404/generic 오류 -> FFmpeg 직접 다운로드 전환...\n")
                    sys.stdout.flush()
                # FFmpeg 직접 다운로드: referer 가 hdrs 에 없으면 info 에서 보충
                ffmpeg_hdrs = dict(hdrs)
                if "referer" not in ffmpeg_hdrs:
                    ref = _clean_header(info.get("referer") or "")
                    if ref and urlparse(ref).scheme in ("http", "https"):
                        ffmpeg_hdrs["referer"] = ref
                f_ok, f_log = _download_ffmpeg_direct(
                    info["url"],
                    out_file,
                    headers=ffmpeg_hdrs,
                    proxy=proxy,
                    total_timeout=remaining,
                    stall_timeout=stall_timeout,
                    max_bytes=max_bytes,
                    show_progress=show_progress,
                    on_progress=on_progress,
                    cancel=cancel,
                )
                if events is not None:
                    events.append({"engine": "ffmpeg", "ok": f_ok, "log_tail": (f_log or "")[-600:]})
                if f_ok:
                    ok = True
                    abort_reason = None
                    tail.append("[FFmpeg Direct Download Success]")

    if show_progress and on_progress is None:
        prog.finish("  [OK] 다운로드 완료" if ok else f"  [FAIL] {abort_reason or f'exit={proc.returncode}'}")

    if not ok:
        _cleanup_partial(out, safe_name, start_wall)

    log = "\n".join(tail)[-1000:]
    if abort_reason:
        log = f"[{abort_reason}]\n{log}"
    return ok, log


# ---------------------------------------------------------------- 403 대응 (헤더 조합 재시도 + 진단)
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_REPLAY_OK = {
    "referer", "origin", "accept", "accept-language", "user-agent",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "authorization",
}
_HTTP_AUTH_ERR = re.compile(r"HTTP Error (401|403)")
_HTTP_ANY_ERR = re.compile(r"HTTP Error (401|403|404|410)")
# 서버가 "누구에게도 제공되지 않는 파일"이라고 답한 경우 (결제 중지/삭제/없음) → 재시도 무의미
_DEAD_RE = re.compile(r"UserProjectAccountProblem|NoSuchKey|NoSuchBucket|BillingNotEnabled|AccountDisabled|HTTP (404|410)")


def _origin_of(u: str) -> str:
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def _build_profiles(info: dict[str, Any]) -> list[dict[str, str]]:
    """403/401 일 때 차례로 시도할 요청 헤더 조합.

    1) 브라우저가 실제로 보낸 헤더 그대로  2) Referer  3) Referer+Origin
    4) 임베드 origin Referer  5) 글 페이지를 Referer 로  6) Referer 없이
    """
    ua = _clean_header(info.get("user_agent") or "") or DEFAULT_UA
    referer = _clean_header(info.get("referer") or "")
    page_url = _clean_header(info.get("page_url") or "")
    base = {"user-agent": ua}
    profiles: list[dict[str, str]] = []

    replay = {k.lower(): _clean_header(v) for k, v in (info.get("request_headers") or {}).items()
              if k.lower() in _REPLAY_OK and _clean_header(v)}
    if replay:
        replay.setdefault("user-agent", ua)
        profiles.append(replay)
    if referer:
        profiles.append({**base, "referer": referer})
        o = _origin_of(referer)
        if o:
            profiles.append({**base, "referer": referer, "origin": o, "accept": "*/*"})
            profiles.append({**base, "referer": o + "/"})   # Referer 를 임베드 origin 으로 (CDN 이 origin 만 검사하는 경우)
    if page_url:
        profiles.append({**base, "referer": page_url})
    profiles.append(dict(base))

    seen: set[tuple] = set()
    uniq = []
    for pr in profiles:
        k = tuple(sorted(pr.items()))
        if k not in seen:
            seen.add(k)
            uniq.append(pr)
    return uniq


def _write_cookie_file(cookies: list[dict[str, Any]]) -> str | None:
    """브라우저가 받은 쿠키를 yt-dlp 용 Netscape 쿠키 파일로 저장 (임시 파일, 종료 시 삭제).
    명령행 인자에 쿠키 값이 노출되지 않도록 헤더 대신 파일을 사용한다."""
    rows = []
    now = int(time.time())
    for c in cookies or []:
        try:
            dom, name, val = str(c.get("domain") or ""), str(c.get("name") or ""), str(c.get("value") or "")
            if not dom or not name or any(ch in dom + name + val for ch in "\t\r\n"):
                continue
            exp = c.get("expires")
            exp = int(exp) if isinstance(exp, (int, float)) and exp > 0 else now + 86400  # 세션 쿠키
            rows.append("\t".join([
                ("#HttpOnly_" if c.get("httpOnly") else "") + dom,
                "TRUE" if dom.startswith(".") else "FALSE",
                str(c.get("path") or "/"),
                "TRUE" if c.get("secure") else "FALSE",
                str(exp), name, val,
            ]))
        except Exception:
            continue
    if not rows:
        return None
    fd, path = tempfile.mkstemp(prefix="cookies_", suffix=".txt")  # POSIX 에서 0600
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Netscape HTTP Cookie File\n" + "\n".join(rows) + "\n")
    return path


def _diagnose_http(url: str, headers: dict[str, str], proxy: str | None = None) -> str:
    """실패 원인 확인용 1회 요청(Range 0-0). 상태코드와 서버(GCS 등)의 XML 오류 코드를 요약."""
    h = {"Range": "bytes=0-0", "User-Agent": headers.get("user-agent") or DEFAULT_UA}
    for k, v in headers.items():
        if k != "user-agent":
            h[k.title()] = v
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(urllib.request.Request(url, headers=h), timeout=10) as resp:
            return f"HTTP {resp.status} (진단 요청은 성공: 쿠키/세션 문제 가능)"
    except urllib.error.HTTPError as e:
        body = e.read(2048).decode("utf-8", errors="replace")
        code = re.search(r"<Code>([^<]+)</Code>", body)
        msg = re.search(r"<Message>([^<]+)</Message>", body)
        out = f"HTTP {e.code}"
        if code:
            out += f" {code.group(1)}"
        if msg:
            out += f": {msg.group(1)[:100]}"
        return out
    except Exception as e:
        return type(e).__name__


def download(
    info: dict[str, Any],
    out_dir: str = "out",
    name: str = "video",
    proxy: str | None = None,
    total_timeout: int = DEFAULT_TOTAL_TIMEOUT,
    stall_timeout: int = DEFAULT_STALL_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_private: bool = False,
    show_progress: bool = True,
    on_progress: Callable[[str, str, str, int], None] | None = None,
    cancel: threading.Event | None = None,
    attempt_log: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """yt-dlp 다운로드. 401/403 이면 요청 헤더 조합을 바꿔 재시도하고, 끝내 실패하면 원인을 진단한다.

    attempt_log 를 주면 시도마다(헤더 프로필 번호, 엔진, 결과, 로그 꼬리)를 추가해 준다 (진단용; 반환값은 그대로).
    """
    try:
        validate_url(info["url"], allow_private=allow_private)
    except ValueError as e:
        if attempt_log is not None:
            attempt_log.append({"profile_no": 0, "engine": "validate", "ok": False, "log_tail": str(e)})
        return False, f"[SECURITY] {e}"

    profiles = _build_profiles(info)
    cookie_file = _write_cookie_file(info.get("cookies") or [])
    deadline = time.monotonic() + total_timeout
    last_log, tried, diag = "", 0, ""
    try:
        for tried, prof in enumerate(profiles, 1):
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                return False, f"[TIMEOUT: 총 {total_timeout}s 초과]\n{last_log}"
            events: list[dict[str, Any]] = []
            ok, last_log = _download_once(
                info, out_dir, name, proxy=proxy, total_timeout=remaining,
                stall_timeout=stall_timeout, max_bytes=max_bytes, allow_private=allow_private,
                show_progress=show_progress, on_progress=on_progress, cancel=cancel,
                headers=prof, cookie_file=cookie_file, events=events,
            )
            if attempt_log is not None:
                for ev in events:
                    attempt_log.append({"profile_no": tried, "profile_headers": sorted(prof.keys()), **ev})
            if ok:
                return True, last_log
            if not _HTTP_AUTH_ERR.search(last_log):
                break  # 헤더 문제가 아니면 조합을 바꿔도 소용없음
            if not diag:
                diag = _diagnose_http(info["url"], profiles[0], proxy)
                if attempt_log is not None:
                    attempt_log.append({"profile_no": tried, "engine": "diagnose", "ok": False, "log_tail": diag})
                if _DEAD_RE.search(diag):
                    return False, f"[DEAD {diag}]\n{last_log}"
        if _HTTP_ANY_ERR.search(last_log):
            diag = diag or _diagnose_http(info["url"], profiles[0], proxy)
            last_log = f"[DIAG {diag} | 헤더 조합 {tried}/{len(profiles)}회 시도]\n{last_log}"
        return False, last_log
    finally:
        if cookie_file:
            try:
                os.unlink(cookie_file)
            except OSError:
                pass