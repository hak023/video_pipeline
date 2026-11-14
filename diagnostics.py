"""실행별 진단 로그 모듈.

main.py 실행마다 Run ID 와 로그 디렉터리(logs/runs/<run_id>)를 만들고, 탐색·판별·다운로드 전 과정의
이벤트/후보/플레이어/다운로드/브라우저 진단을 기록한다.

원칙
- 기록 실패가 본 작업을 중단시키지 않는다 (모든 쓰기는 예외를 삼키고 stderr 로 1회만 경고).
- 로그에는 쿠키/Authorization/서명 토큰이 남지 않는다. 후보·플레이어는 허용 목록(allowlist) 필드만 직렬화하고,
  URL/텍스트는 mask_url/mask_text 를 거친다.
- 파일은 쓸 때마다 flush 되므로 비정상 종료 시에도 가능한 범위의 자료가 남는다.
- 이 모듈은 프로젝트의 다른 모듈을 import 하지 않는다 (순환 import 방지).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parent
DEFAULT_LOG_DIR = ROOT / "logs"
DEFAULT_RETAIN = 30

MAX_EVENTS = 100_000
MAX_RESPONSES = 5_000
MAX_CONSOLE = 2_000
MAX_FAILURES = 2_000

# ------------------------------------------------------------------ masking
_SENSITIVE_KEY_RE = re.compile(
    r"(token|key|sig|signature|auth|session|sess|sid|secret|passw|pwd|credential|jwt|policy|hdnts|expires|hmac|cookie|ticket)",
    re.I,
)
_MASK_MIN_LEN = 20                       # 이 길이 이상의 쿼리 값은 키 이름과 무관하게 마스킹
_SENSITIVE_HEADERS = {"cookie", "set-cookie", "authorization", "proxy-authorization", "x-api-key"}
_URL_HEADERS = {"referer", "origin", "location"}
_DROP_KEYS = {"cookies", "cookie", "request_headers", "authorization", "set-cookie", "headers"}
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+")
_HEADER_IN_TEXT_RE = re.compile(r"(?i)\b(cookie|set-cookie|authorization|proxy-authorization)\b(\s*[:=]\s*)[^\r\n]+")
_BARE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{24,}(?![A-Za-z0-9])")
_TOKEN_SEG_RE = re.compile(r"[A-Za-z0-9_=-]+")


def _tag(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()[:6]


def _masked(value: str) -> str:
    return f"<masked:{_tag(value)}>"


def _looks_like_token(seg: str) -> bool:
    stem = seg.rsplit(".", 1)[0] if "." in seg else seg
    return (len(stem) >= 32 and _TOKEN_SEG_RE.fullmatch(stem) is not None
            and any(ch.isdigit() for ch in stem) and stem.count("-") <= 2)


def mask_url(url: Any) -> Any:
    """host/path 는 보존하고 인증 토큰·서명·긴 값은 마스킹. 같은 값은 같은 태그로 바뀌어 상관 분석이 가능하다."""
    if not isinstance(url, str) or not url:
        return url
    if not re.match(r"(?i)^(https?|wss?)://", url):
        return url if len(url) <= 80 else url[:60] + "…"
    try:
        s = urlsplit(url)
        host = s.hostname or ""
        netloc = host + (f":{s.port}" if s.port else "")
    except ValueError:
        return url[:80] + "…"
    path = "/".join(_masked(seg) if _looks_like_token(seg) else seg for seg in s.path.split("/"))
    query = ""
    if s.query:
        parts = []
        for k, v in parse_qsl(s.query, keep_blank_values=True):
            if v and (_SENSITIVE_KEY_RE.search(k) or len(v) >= _MASK_MIN_LEN):
                v = _masked(v)
            parts.append(f"{k}={v}" if v != "" else k)
        query = "?" + "&".join(parts)
    return f"{s.scheme}://{netloc}{path}{query}"


def mask_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        lk = str(k).lower()
        if lk in _SENSITIVE_HEADERS:
            out[lk] = "<masked>"
        elif lk in _URL_HEADERS:
            out[lk] = str(mask_url(str(v)))
        else:
            out[lk] = str(v)[:200]
    return out


def mask_text(text: Any) -> Any:
    """자유 텍스트(로그 줄, yt-dlp 출력 등)에서 URL 과 인증 헤더 값을 마스킹."""
    if not isinstance(text, str) or not text:
        return text

    def _url(m: re.Match) -> str:
        raw = m.group(0)
        tail = ""
        while raw and raw[-1] in ").,;]}":
            tail = raw[-1] + tail
            raw = raw[:-1]
        return str(mask_url(raw)) + tail

    text = _URL_IN_TEXT_RE.sub(_url, text)
    text = _HEADER_IN_TEXT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<masked>", text)
    # URL/헤더 형태가 아닌 평문 토큰(예: JS 예외 메시지에 섞인 키)도 길고 영숫자가 섞여 있으면 마스킹
    return _BARE_TOKEN_RE.sub(
        lambda m: _masked(m.group(0)) if any(c.isdigit() for c in m.group(0)) and any(c.isalpha() for c in m.group(0))
        else m.group(0), text)


def sanitize(obj: Any, _depth: int = 0) -> Any:
    """dict/list 를 재귀적으로 순회해 쿠키·요청 헤더 필드를 제거하고 문자열을 마스킹 (report.json 등 출력용)."""
    if _depth > 12:
        return "<depth-limit>"
    if isinstance(obj, dict):
        return {k: sanitize(v, _depth + 1) for k, v in obj.items() if str(k).lower() not in _DROP_KEYS}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v, _depth + 1) for v in obj]
    if isinstance(obj, str):
        return mask_text(obj)
    return obj


# ------------------------------------------------------------------ helpers
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def gen_run_id() -> str:
    return f"{datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}"


# 후보/플레이어 로그에 남길 필드 허용 목록 (cookies, request_headers 등은 구조적으로 제외된다)
_CANDIDATE_FIELDS = (
    "candidate_id", "url", "content_type", "status", "content_length", "duration", "dom_duration",
    "m3u8_kind", "page_url", "frame_url", "embed_url", "player_id", "playlist_index", "from_playlist",
    "first_seen", "last_seen", "play_order", "source", "classification", "main_score", "ad_score",
    "main_evidence", "ad_evidence", "rules", "blacklist_match", "group_id", "duplicate_of",
    "excluded_reason", "target", "item",
)
_URL_FIELDS = {"url", "page_url", "frame_url", "embed_url"}


def candidate_view(cand: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {}
    for k in _CANDIDATE_FIELDS:
        if k in cand and cand[k] is not None:
            v = cand[k]
            view[k] = mask_url(v) if k in _URL_FIELDS else sanitize(v)
    return view


class Diagnostics:
    def __init__(self, base_dir: Path | str | None = None, run_id: str | None = None,
                 meta: dict[str, Any] | None = None, retain: int = DEFAULT_RETAIN,
                 enabled: bool = True):
        self.enabled = enabled
        self.run_id = run_id or gen_run_id()
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_LOG_DIR
        self.runs_dir = self.base_dir / "runs"
        self.dir = self.runs_dir / self.run_id
        self.meta = meta or {}
        self.started_at = now_iso()
        self._t0 = time.monotonic()
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._warned: set[str] = set()
        self._cand_seq = 0
        self._player_seq = 0
        self._exc_seq = 0
        self._counts = {"events": 0, "responses": 0, "console": 0, "failures": 0}
        self._candidates: dict[str, dict[str, Any]] = {}
        self._players: dict[str, dict[str, Any]] = {}
        self._downloads: list[dict[str, Any]] = []
        self._pages: set[str] = set()
        self._frames: set[str] = set()
        self._stages: dict[str, dict[str, Any]] = {}
        self._failure: dict[str, Any] = {}
        self._snapshots: dict[str, Callable[[], Any]] = {}
        self._finalized = False
        self._status = "RUNNING"
        self.zip_path: Path | None = None
        if self.enabled:
            try:
                (self.dir / "browser").mkdir(parents=True, exist_ok=True)
                (self.dir / "exceptions").mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self.enabled = False
                self._warn("init", f"로그 디렉터리 생성 실패 → 진단 로그 비활성화: {e}")
        if self.enabled:
            self._cleanup_old_runs(retain)
            self.write_summary()

    # ------------------------------------------------------------ plumbing
    def _warn(self, key: str, msg: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        try:
            print(f"[진단 로그 경고] {msg}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def _append(self, rel: str, text: str) -> None:
        if not self.enabled:
            return
        try:
            with self._lock, (self.dir / rel).open("a", encoding="utf-8", newline="\n") as f:
                f.write(text if text.endswith("\n") else text + "\n")
                f.flush()
        except Exception as e:
            self._warn(rel, f"{rel} 기록 실패: {e}")

    def _write_json(self, rel: str, data: Any) -> None:
        if not self.enabled:
            return
        path = self.dir / rel
        try:
            with self._lock:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                try:
                    os.replace(tmp, path)
                except OSError:
                    path.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
                    tmp.unlink(missing_ok=True)
        except Exception as e:
            self._warn(rel, f"{rel} 기록 실패: {e}")

    def write_json(self, rel: str, data: Any) -> None:
        """임의의 JSON 산출물을 실행 디렉터리에 기록 (마스킹 적용)."""
        self._write_json(rel, sanitize(data))

    def _cleanup_old_runs(self, retain: int) -> None:
        try:
            if retain <= 0:
                return
            runs = sorted((p for p in self.runs_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
            for p in runs[:-retain] if len(runs) > retain else []:
                if p.name != self.run_id:
                    shutil.rmtree(p, ignore_errors=True)
                    (self.base_dir / "zips" / f"{p.name}.zip").unlink(missing_ok=True)
        except Exception as e:
            self._warn("cleanup", f"오래된 로그 정리 실패: {e}")

    # ------------------------------------------------------------ context
    def set_context(self, **kw: Any) -> None:
        ctx = dict(getattr(self._tls, "ctx", {}) or {})
        ctx.update({k: v for k, v in kw.items() if v is not None})
        self._tls.ctx = ctx

    @contextlib.contextmanager
    def context(self, **kw: Any) -> Iterator[None]:
        prev = dict(getattr(self._tls, "ctx", {}) or {})
        self.set_context(**kw)
        try:
            yield
        finally:
            self._tls.ctx = prev

    def get_context(self) -> dict[str, Any]:
        return dict(getattr(self._tls, "ctx", {}) or {})

    # ------------------------------------------------------------ events
    def event(self, stage: str, event: str, message: str = "", level: str = "INFO", **fields: Any) -> None:
        """표준 이벤트. page_url/frame_url/candidate_id/player_id 는 필드로, 나머지는 details 로 들어간다."""
        if not self.enabled:
            return
        try:
            ctx = self.get_context()
            std = {k: fields.pop(k, None) or ctx.get(k) for k in ("page_url", "frame_url", "candidate_id", "player_id")}
            rec = {
                "timestamp": now_iso(), "run_id": self.run_id, "stage": stage, "event": event,
                "level": level.upper(),
                **{k: mask_url(v) if k.endswith("_url") else v for k, v in std.items() if v},
                **({"target": ctx["target"]} if ctx.get("target") else {}),
                **({"item": ctx["item"]} if ctx.get("item") is not None else {}),
                "message": mask_text(message),
            }
            if fields:
                rec["details"] = sanitize(fields)
            with self._lock:
                self._counts["events"] += 1
                n = self._counts["events"]
            if n == MAX_EVENTS + 1:
                self._append("events.jsonl", json.dumps({"event": "events_truncated"}))
            if n <= MAX_EVENTS:
                self._append("events.jsonl", json.dumps(rec, ensure_ascii=False, default=str))
            if rec["level"] != "DEBUG":
                self._append("run.log", f"{datetime.now():%H:%M:%S} [{rec['level']:<7}] {stage}/{event} {rec['message']}".rstrip())
            if rec["level"] in ("ERROR", "CRITICAL") and not self._failure:
                self._failure = {"failure_stage": stage, "failure_reason": rec["message"] or event}
        except Exception as e:
            self._warn("event", f"이벤트 기록 실패: {e}")

    def console_line(self, msg: str) -> None:
        """progress.log() 가 콘솔에 낸 메시지를 run.log 에도 남긴다 (진행 보드 제어문자는 제외)."""
        if not self.enabled or not msg:
            return
        self._append("run.log", f"{datetime.now():%H:%M:%S} {mask_text(msg.rstrip())}")

    def exception(self, stage: str, exc: BaseException, **fields: Any) -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                self._exc_seq += 1
                seq = self._exc_seq
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            ctx = {**self.get_context(), **fields}
            body = (f"stage: {stage}\ntime: {now_iso()}\ntype: {type(exc).__name__}\n"
                    f"context: {json.dumps(sanitize(ctx), ensure_ascii=False, default=str)}\n\n{mask_text(tb)}")
            self._append(f"exceptions/exception_{seq:03d}.txt", body)
            self.event(stage, "exception", f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
                       level="ERROR", exception_file=f"exceptions/exception_{seq:03d}.txt", **fields)
        except Exception as e:
            self._warn("exception", f"예외 기록 실패: {e}")

    # ------------------------------------------------------------ stages
    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t0 = time.monotonic()
        self.event(name, "stage_start", level="DEBUG")
        status = "ok"
        try:
            yield
        except BaseException:
            status = "error"
            raise
        finally:
            dt = time.monotonic() - t0
            with self._lock:
                st = self._stages.setdefault(name, {"count": 0, "total_sec": 0.0, "errors": 0})
                st["count"] += 1
                st["total_sec"] = round(st["total_sec"] + dt, 2)
                st["errors"] += status == "error"
            self.event(name, "stage_end", level="DEBUG", status=status, seconds=round(dt, 2))

    def note_page(self, url: str) -> None:
        with self._lock:
            self._pages.add(str(mask_url(url)))

    def note_frames(self, urls: list[str]) -> None:
        with self._lock:
            self._frames.update(str(mask_url(u)) for u in urls if u and u != "about:blank")

    def fail(self, stage: str, reason: str) -> None:
        with self._lock:
            self._failure = {"failure_stage": stage, "failure_reason": mask_text(reason)}

    # ------------------------------------------------------------ candidates / players / downloads
    def new_candidate_id(self) -> str:
        with self._lock:
            self._cand_seq += 1
            return f"candidate_{self._cand_seq:04d}"

    def new_player_id(self) -> str:
        with self._lock:
            self._player_seq += 1
            return f"player_{self._player_seq:03d}"

    def candidate(self, cand: dict[str, Any], **extra: Any) -> str:
        """후보를 candidates.json 에 기록/갱신. candidate_id 가 없으면 부여한다."""
        cid = cand.get("candidate_id") or self.new_candidate_id()
        cand["candidate_id"] = cid
        if not self.enabled:
            return cid
        try:
            ctx = self.get_context()
            view = candidate_view({**ctx, **cand, **extra})
            view["last_seen"] = now_iso()
            with self._lock:
                self._candidates.setdefault(cid, {}).update(view)
            self._write_json("candidates.json", list(self._candidates.values()))
        except Exception as e:
            self._warn("candidates", f"후보 기록 실패: {e}")
        return cid

    def update_candidate(self, cid: str, **fields: Any) -> None:
        if not self.enabled or not cid:
            return
        try:
            with self._lock:
                rec = self._candidates.setdefault(cid, {"candidate_id": cid})
                rec.update(candidate_view(fields))
            self._write_json("candidates.json", list(self._candidates.values()))
        except Exception as e:
            self._warn("candidates", f"후보 갱신 실패: {e}")

    def player(self, player_id: str, **fields: Any) -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                rec = self._players.setdefault(player_id, {"player_id": player_id})
                rec.update(sanitize({k: (mask_url(v) if k.endswith("_url") else v) for k, v in fields.items()}))
            self._write_json("players.json", list(self._players.values()))
        except Exception as e:
            self._warn("players", f"플레이어 기록 실패: {e}")

    def download(self, **fields: Any) -> None:
        if not self.enabled:
            return
        try:
            ctx = self.get_context()
            rec = sanitize({"time": now_iso(), "run_id": self.run_id, **ctx, **fields})
            if "url" in rec:
                rec["url"] = mask_url(rec["url"])
            with self._lock:
                self._downloads.append(rec)
            self._write_json("downloads.json", list(self._downloads))
        except Exception as e:
            self._warn("downloads", f"다운로드 기록 실패: {e}")

    # ------------------------------------------------------------ browser logs
    def browser_console(self, kind: str, text: str, location: str = "", frame_url: str = "") -> None:
        with self._lock:
            self._counts["console"] += 1
            n = self._counts["console"]
        if n <= MAX_CONSOLE:
            frame_part = f" frame={mask_url(frame_url)}" if frame_url else ""
            loc_part = f" {mask_text(location)}" if location else ""
            self._append("browser/console.log", f"{now_iso()} [{kind}] {mask_text(text)[:500]}{loc_part}{frame_part}".rstrip())

    def browser_page_error(self, text: str) -> None:
        self._append("browser/page_errors.log", f"{now_iso()} {mask_text(text)[:2000]}")

    def browser_request_failure(self, **fields: Any) -> None:
        with self._lock:
            self._counts["failures"] += 1
            n = self._counts["failures"]
        if n <= MAX_FAILURES:
            self._append("browser/request_failures.jsonl",
                         json.dumps({"time": now_iso(), **{k: (mask_url(v) if k.endswith("url") else v) for k, v in fields.items()}},
                                    ensure_ascii=False, default=str))

    def browser_response(self, **fields: Any) -> None:
        with self._lock:
            self._counts["responses"] += 1
            n = self._counts["responses"]
        if n <= MAX_RESPONSES:
            self._append("browser/responses.jsonl",
                         json.dumps({"time": now_iso(), **{k: (mask_url(v) if k.endswith("url") else v) for k, v in fields.items()}},
                                    ensure_ascii=False, default=str))

    # ------------------------------------------------------------ summary / zip
    def add_snapshot(self, rel: str, provider: Callable[[], Any]) -> None:
        self._snapshots[rel] = provider

    def _summary_dict(self) -> dict[str, Any]:
        cands = list(self._candidates.values())
        by_cls = {"MAIN": 0, "AD": 0, "PENDING": 0}
        for c in cands:
            by_cls[c.get("classification")] = by_cls.get(c.get("classification"), 0) + 1
        ok = sum(1 for d in self._downloads if d.get("status") == "SUCCESS")
        s = {
            "run_id": self.run_id,
            "status": self._status,
            "started_at": self.started_at,
            "finished_at": now_iso() if self._finalized else None,
            "elapsed_sec": round(time.monotonic() - self._t0, 1),
            "page_count": len(self._pages),
            "frame_count": len(self._frames),
            "player_count": len(self._players),
            "candidate_count": len(cands),
            "main_count": by_cls.get("MAIN", 0),
            "ad_count": by_cls.get("AD", 0),
            "pending_count": by_cls.get("PENDING", 0),
            "download_success": ok,
            "download_failure": len(self._downloads) - ok,
            "exception_count": self._exc_seq,
            "failure_stage": self._failure.get("failure_stage"),
            "failure_reason": self._failure.get("failure_reason"),
            "stages": self._stages,
            "log_directory": str(self.dir),
            "zip": str(self.zip_path) if self.zip_path else None,
            "meta": sanitize(self.meta),
        }
        return s

    def write_summary(self) -> None:
        if self.enabled:
            with self._lock:
                self._write_json("summary.json", self._summary_dict())

    def set_status(self, status: str, failure_stage: str | None = None, failure_reason: str | None = None) -> None:
        with self._lock:
            self._status = status
            if status == "OK":
                self._failure = {}
            if failure_stage or failure_reason:
                self._failure = {"failure_stage": failure_stage, "failure_reason": mask_text(failure_reason or "")}
        self.write_summary()

    def create_zip(self) -> Path | None:
        """실행 디렉터리를 logs/zips/<run_id>.zip 으로 묶는다. 실패해도 원본 디렉터리는 보존."""
        if not self.enabled:
            return None
        try:
            zdir = self.base_dir / "zips"
            zdir.mkdir(parents=True, exist_ok=True)
            path = zdir / f"{self.run_id}.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(self.dir.rglob("*")):
                    if f.is_file() and f.suffix != ".tmp":
                        zf.write(f, Path(self.run_id) / f.relative_to(self.dir))
            self.zip_path = path
            return path
        except Exception as e:
            self._warn("zip", f"진단 ZIP 생성 실패(원본 로그는 보존됨): {e}")
            return None

    def finalize(self, status: str, failure_stage: str | None = None, failure_reason: str | None = None,
                 make_zip: bool = True) -> None:
        """모든 종료 경로에서 호출: summary.json 최종 갱신 + 스냅샷 + ZIP."""
        if self._finalized:
            return
        try:
            self._finalized = True
            self.set_status(status, failure_stage, failure_reason)
            for rel, provider in self._snapshots.items():
                try:
                    self.write_json(rel, provider())
                except Exception as e:
                    self._warn(rel, f"스냅샷 {rel} 실패: {e}")
            self.event("run", "run_end", f"실행 종료: {status}", level="INFO", status=status)
            self.write_summary()
            if make_zip:
                self.create_zip()
                self.write_summary()
        except Exception as e:
            self._warn("finalize", f"요약 기록 실패: {e}")


# ------------------------------------------------------------------ global session
_DISABLED = Diagnostics(enabled=False)
_current: Diagnostics | None = None


def init_diagnostics(base_dir: Path | str | None = None, meta: dict[str, Any] | None = None,
                     retain: int = DEFAULT_RETAIN) -> Diagnostics:
    global _current
    _current = Diagnostics(base_dir, meta=meta, retain=retain)
    _current.event("run", "run_start", f"실행 시작 (run_id={_current.run_id})")
    return _current


def get_diagnostics() -> Diagnostics:
    return _current or _DISABLED


def log_event(stage: str, event: str, message: str = "", level: str = "INFO", **fields: Any) -> None:
    get_diagnostics().event(stage, event, message, level, **fields)


def log_exception(stage: str, exc: BaseException, **fields: Any) -> None:
    get_diagnostics().exception(stage, exc, **fields)


def write_console_line(msg: str) -> None:
    get_diagnostics().console_line(msg)


def set_context(**kw: Any) -> None:
    get_diagnostics().set_context(**kw)


def diag_context(**kw: Any):
    return get_diagnostics().context(**kw)


# ------------------------------------------------------------------ browser collectors
_MEDIA_URL_RE = re.compile(r"\.(m3u8|mpd|mp4|webm|m4s|ts)(\?|$)", re.I)


def _is_media_response(url: str, ctype: str) -> bool:
    return bool(_MEDIA_URL_RE.search(url)) or any(t in ctype for t in ("mpegurl", "dash+xml", "video/", "audio/"))


def attach_page_diagnostics(page: Any, label: str = "") -> None:
    """Playwright Page 에 console/pageerror/requestfailed/response/frame 수집기를 연결.
    핸들러 내부 예외는 모두 삼켜서 탐색 흐름을 방해하지 않는다."""
    diag = get_diagnostics()
    if not diag.enabled:
        return

    def safe(fn: Callable[..., None]) -> Callable[..., None]:
        def wrapper(*a: Any) -> None:
            try:
                fn(*a)
            except Exception as e:
                diag._warn("browser_handler", f"브라우저 이벤트 핸들러 오류(무시): {e}")
        return wrapper

    def on_console(msg: Any) -> None:
        loc = msg.location or {}
        u = loc.get("url", "")
        line = loc.get("lineNumber")
        col = loc.get("columnNumber")
        loc_str = f"{u}:{line}:{col}" if (line is not None and u) else (u or "")
        try:
            frame_url = getattr(getattr(msg, "page", None), "url", "")
        except Exception:
            frame_url = ""
        diag.browser_console(f"{label}:{msg.type}", msg.text, loc_str[:160], frame_url=frame_url)

    def on_pageerror(err: Any) -> None:
        diag.browser_page_error(f"[{label}] {err}")

    def on_requestfailed(req: Any) -> None:
        try:
            frame_url = req.frame.url
        except Exception:
            frame_url = ""
        diag.browser_request_failure(label=label, url=req.url, method=req.method, resource_type=req.resource_type,
                                     error=str(req.failure or "")[:200], frame_url=frame_url)

    def on_response(resp: Any) -> None:
        ctype = (resp.headers.get("content-type") or "").lower()
        frame_url = ""
        try:
            frame_url = resp.frame.url
        except Exception:
            pass

        # 1. HTTP 4xx, 5xx 오류 응답 기록
        if resp.status >= 400:
            req = resp.request
            diag.browser_request_failure(
                label=label,
                url=resp.url,
                method=req.method if req else "GET",
                resource_type=req.resource_type if req else "",
                status=resp.status,
                content_type=ctype[:80],
                error=f"HTTP {resp.status}",
                frame_url=frame_url,
            )

        # 2. 미디어 응답 기록
        if not _is_media_response(resp.url, ctype):
            return

        diag.browser_response(label=label, url=resp.url, status=resp.status, content_type=ctype[:80],
                              content_length=resp.headers.get("content-length"),
                              content_range=resp.headers.get("content-range"), frame_url=frame_url)

    def on_frame(kind: str) -> Callable[[Any], None]:
        def h(fr: Any) -> None:
            diag.event("browser", f"frame_{kind}", level="DEBUG", frame_url=getattr(fr, "url", ""), label=label)
        return h

    for name, fn in (("console", on_console), ("pageerror", on_pageerror), ("requestfailed", on_requestfailed),
                     ("response", on_response), ("frameattached", on_frame("attached")),
                     ("framedetached", on_frame("detached")), ("framenavigated", on_frame("navigated"))):
        try:
            page.on(name, safe(fn))
        except Exception as e:
            diag._warn("attach", f"{name} 수집기 연결 실패: {e}")
