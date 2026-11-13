from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from crawler import build_page_url, crawl_page, is_date_in_range, parse_post_date
from discover import DiscoverNavigationError, discover_videos, probe_duration, select_best
from download import (
    DEFAULT_STALL_TIMEOUT,
    DEFAULT_TOTAL_TIMEOUT,
    disable_quickedit,
    download,
    sanitize_filename,
)
from diagnostics import (
    DEFAULT_RETAIN, diag_context, get_diagnostics, init_diagnostics, log_event, log_exception, mask_text,
    sanitize,
)
from history import HistoryManager, normalize_url
from progress import ProgressBoard, log, set_board
from verify import verify
from ad_blacklist import blacklist

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "out"
LOG_DIR = ROOT / "logs"
# history 는 다운로드 폴더(out/) 밖에 둔다: out/ 을 정리·이동해도 기록이 사라지지 않도록.
HISTORY_DIR = ROOT / "history_data"

CANCEL = threading.Event()            # Ctrl+C 시 모든 워커에 중단 신호
BACKOFF = (5, 15, 30)                 # 재시도 대기(초). 여기에 0~3초 jitter 가산
MAX_CONCURRENT_BROWSERS = 3           # Chromium 동시 실행 수 (메모리/차단 방지)
MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".m4a", ".ts", ".mov"}
DONE_STATES = {"ok", "skipped", "skipped_short"}
MAX_CANDIDATES_TRIED = 3              # 한 영상에서 후보 URL을 최대 몇 개까지 시도할지


# ------------------------------------------------------------------ library (파일명 매칭)
def _stem_key(stem: str) -> str:
    return unicodedata.normalize("NFC", stem).strip().lower()


class Library:
    """이미 받아 둔 파일(다른 폴더로 옮긴 것 포함)을 파일명으로 찾는 보조 인덱스.

    history 가 유실/이동되었거나 글의 URL이 바뀐 경우의 안전망이다.
    같은 제목의 다른 영상도 같은 것으로 간주되므로 옵션(--match-name/--library)으로만 켠다.
    """

    def __init__(self, dirs: list[Path]):
        self._stems: dict[str, Path] = {}
        for d in dirs:
            if not d.is_dir():
                continue
            for f in d.rglob("*"):
                if f.is_file() and f.suffix.lower() in MEDIA_EXTS:
                    self._stems.setdefault(_stem_key(f.stem), f)

    def __len__(self) -> int:
        return len(self._stems)

    def find(self, name: str) -> Path | None:
        return self._stems.get(_stem_key(sanitize_filename(name)))


@dataclass
class Options:
    workers: int = 5
    attempts: int = 3                 # 총 시도 횟수 (최초 1회 + 재시도 2회)
    total_timeout: int = DEFAULT_TOTAL_TIMEOUT
    stall_timeout: int = DEFAULT_STALL_TIMEOUT
    require_file: bool = False        # False(기본): history 기록만 신뢰 → 파일을 옮겨도 재다운로드 안 함
    skip_under: float = 60.0          # 이 길이(초) 이하의 영상은 다운로드하지 않음 (0 = 끄기)
    history_dir: Path = HISTORY_DIR
    cooldown_hours: float = 0.0       # 0 = 끄기. 같은 글이 cooldown_after 회 이상 실패했으면 이 시간 동안 재탐색 보류
    cooldown_after: int = 2
    library: Library | None = field(default=None, repr=False)


# ------------------------------------------------------------------ names
_names_lock = threading.Lock()
_names_in_use: set[str] = set()


def _reserve_name(name: str, url: str, out_dir: Path, force: bool) -> str:
    """동시 실행/동일 제목 충돌 방지. 충돌하면 URL 해시를 덧붙여 고유 파일명 사용."""
    base = sanitize_filename(name)
    with _names_lock:
        cand = base
        other_file_exists = any(
            f.is_file() and f.stem == cand for f in out_dir.iterdir()
        ) if out_dir.is_dir() else False
        if cand.lower() in _names_in_use or (other_file_exists and not force):
            h = hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:6]
            cand = f"{base[:100]} [{h}]"
        _names_in_use.add(cand.lower())
        return cand


def _release_name(name: str) -> None:
    with _names_lock:
        _names_in_use.discard(name.lower())


# ------------------------------------------------------------------ item pipeline
def _load_targets(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("targets") or []


def _meta_num(meta: Any, key: str) -> float | None:
    try:
        if isinstance(meta, dict) and meta.get(key) is not None:
            return float(meta[key])
    except (TypeError, ValueError):
        pass
    return None


def _discover_groups(t, url, proxy, headless, attempt, browser_sem):
    # 재시도할수록 플레이어 로딩 대기시간을 늘림
    wait_ms = int(t.get("wait_ms", 6000) * (1 + 0.5 * (attempt - 1)))
    with (browser_sem or nullcontext()):
        return discover_videos(
            url,
            wait_ms=wait_ms,
            click_selector=t.get("click_selector"),
            proxy=proxy,
            headless=headless,
        )


def _mark_short(res, history, item_url, item_name, post_date, dur, opts) -> dict:
    reason = f"short video ({dur:.0f}s <= {opts.skip_under:.0f}s)"
    res.update(status="skipped_short", duration=dur, reason=reason)
    log(f"  [SHORT SKIP] {item_name}: {dur:.0f}초")
    if history:
        history.record_skipped(item_url, item_name, reason, post_date=post_date, duration=dur)
    return res


def _process_item(
    *, group, n, url, name, out_dir, proxy, opts, history, post_date, force, board, slot,
) -> dict:
    """페이지 안의 동영상 1개를 처리 (이미 완료/길이 스킵/다운로드/검증/기록)."""
    idx = group["index"]
    # 영상이 1개뿐인 글은 기존 history 와 호환되도록 글 URL 그대로, 여러 개면 #vNN 으로 구분
    item_url = url if n == 1 else f"{url}#v{idx:02d}"
    item_name = name if n == 1 else f"{name} ({idx:02d})"
    label = name if n == 1 else f"{name} {idx}/{n}"
    res: dict[str, Any] = {"index": idx, "name": item_name, "url": item_url}

    if history and not force and history.is_downloaded(item_url):
        res.update(status="skipped", reason="already_downloaded")
        return res

    if opts.library and not force and n > 1:
        hit = opts.library.find(item_name)
        if hit:
            log(f"  [SKIP] 기존 파일과 이름 일치: {item_name}")
            if history:
                history.record_success(item_url, item_name, str(hit), post_date=post_date,
                                       extra={"matched_by": "filename"})
            res.update(status="skipped", reason="exists_in_library", output=str(hit))
            return res

    cands = group.get("candidates") or []
    ordered = list(cands)   # discover_videos 가 판별·그룹화·정렬을 마친 후보 (광고로 판별된 후보는 제외됨)
    diag = get_diagnostics()
    if not ordered:
        ad_excluded = group.get("ad_excluded") or 0
        if ad_excluded:
            res.update(status="no_main_found", candidates=0, ad_excluded=ad_excluded,
                       reason=f"본편으로 볼 후보 없음: 발견된 {ad_excluded}개 후보가 모두 광고로 판별됨")
        else:
            res.update(status="no_media_found", reason="no_media_found", candidates=0)
        diag.fail("classification" if ad_excluded else "media_discovery", res["reason"])
        if group.get("debug"):
            res["debug"] = group["debug"]     # 이동 결과/거절된 요청/프레임 목록 (원인 진단용)
            log(f"  [진단] {item_name}: 본편 후보를 찾지 못함({res['status']}) → 실행 로그(candidates.json)와 report 의 debug 확인 "
                f"(헤드리스 차단 의심 시 --headful 로 재시도)")
        return res
    res["candidates"] = len(cands)
    if group.get("group_id"):
        res["group_id"] = group["group_id"]
    if group.get("low_confidence"):
        res["low_confidence"] = True
        log(f"  [주의] {item_name}: 본편 근거가 약한 후보(PENDING)를 사용합니다")

    on_progress = None
    if board is not None:
        def on_progress(pct, speed, eta, size, _l=label, _s=slot):
            board.update(_s, _l, pct, size)
        board.update(slot, label, "시작", 0)

    # 후보를 우선순위대로 시도: 앞 후보가 403 등으로 실패하면 다음 후보로 (광고/만료 URL 대비)
    short_seen: list[tuple[float, str]] = []     # (길이, 판별 결과)
    failed_any = False
    n_attempted = n_dead = 0
    for n_cand, cand in enumerate(ordered[:MAX_CANDIDATES_TRIED], 1):
        if CANCEL.is_set():
            res["status"] = "cancelled"
            return res
        cls = cand.get("classification") or "PENDING"
        res.update(selected_content_type=cand.get("content_type"), source=cand["url"])

        # 1분 이하 영상은 받지 않음(사용자 옵션) - 이 후보만 건너뛰고 다음 후보 확인.
        # 길이만으로 광고 확정/블랙리스트 등록은 하지 않는다: 본편 근거가 없는 짧은 영상만 '약한 증거'로 남긴다.
        if opts.skip_under > 0:
            dur = probe_duration(cand, proxy=proxy)
            if dur is None:
                dur = cand.get("dom_duration")   # 플레이어(DOM)가 알려준 길이
            if dur is not None and dur <= opts.skip_under:
                short_seen.append((dur, cls))
                if cls != "MAIN":
                    blacklist.observe_ad(cand.get("url", ""), reason="short_unclassified", page_url=url)
                log_event("download", "candidate_skipped_short", f"짧은 영상 건너뜀 ({dur:.0f}s, {cls})",
                          candidate_id=cand.get("candidate_id"), duration=dur, classification=cls)
                get_diagnostics().download(candidate_id=cand.get("candidate_id"), group_id=group.get("group_id"),
                                           url=cand.get("url"), status="SKIPPED_SHORT", duration=dur, classification=cls)
                continue

        r = _download_candidate(cand, item_url=item_url, item_name=item_name, out_dir=out_dir,
                                proxy=proxy, opts=opts, force=force, on_progress=on_progress,
                                group_id=group.get("group_id"))
        st = r.pop("status")
        n_attempted += 1
        is_dead = r.pop("dead", False)
        if is_dead:
            n_dead += 1
            blacklist.observe_ad(cand.get("url", ""), reason="dead_url", page_url=url)
        res.update(r)
        if st == "ok":
            res["status"] = "ok"
            if not group.get("low_confidence"):
                blacklist.record_main(cand.get("url", ""), page_url=url)   # 근거 약한 후보는 '본편 확인'으로 치지 않는다
            if history:
                size = _meta_num(res.get("meta"), "size")
                history.record_success(
                    url=item_url, name=item_name, output_file=res["output"],
                    post_date=post_date, duration=res.get("duration"),
                    size=int(size) if size is not None else None,
                    extra={"low_confidence": True, "classification": cls} if group.get("low_confidence") else None,
                )
            return res
        if st == "cancelled":
            res["status"] = "cancelled"
            return res
        if st == "short":
            short_seen.append((r.get("duration") or 0.0, cls))
            if cls != "MAIN":
                blacklist.observe_ad(cand.get("url", ""), reason="short_unclassified", page_url=url)
            continue
        failed_any = True
        if r.get("fatal"):
            res.update(status="failed", fatal=True)
            return res
        if n_cand < min(len(ordered), MAX_CANDIDATES_TRIED):
            log(f"  [후보 실패] {item_name}: {str(r.get('reason'))[:110]} → 다음 후보 시도")

    # 모든 후보를 소진
    if short_seen and not failed_any:
        if any(c == "MAIN" for _, c in short_seen):
            return _mark_short(res, history, item_url, item_name, post_date, max(d for d, _ in short_seen), opts)
        # 본편 근거가 없는 짧은 영상뿐: 광고만 잡힌 것일 수 있으므로 '완료'로 기록하지 않아 다음 실행에서 다시 탐색한다.
        longest = max(d for d, _ in short_seen)
        res.update(status="no_main_found", duration=longest,
                   reason=f"본편 미검출: 짧은 미분류 영상만 발견({longest:.0f}s <= {opts.skip_under:.0f}s) - 광고 가능성")
        diag.fail("classification", res["reason"])
        log(f"  [본편 미검출] {item_name}: 짧은 영상({longest:.0f}초)만 발견되어 완료 처리하지 않음")
        return res
    res["status"] = "failed"
    res.setdefault("reason", "all candidates failed")
    if n_attempted and n_dead == n_attempted:
        res["reason"] = "본편 스트림 미검출(폐기된 URL만 발견) - " + str(res["reason"])
    diag.fail("download", str(res["reason"]))
    return res


def _short_reason(dl_log: str) -> str:
    """로그 앞부분(Extracting URL...)이 아니라 실제 ERROR 줄과 진단 정보를 사유로 사용."""
    lines = [ln.strip() for ln in dl_log.splitlines() if ln.strip()]
    if not lines:
        return "download_failed"
    head = lines[0] if lines[0].startswith(("[DEAD", "[DIAG", "[STALLED", "[TIMEOUT", "[SIZE", "[SECURITY")) else ""
    errs = [ln for ln in lines if ln.startswith("ERROR")]
    body = errs[-1] if errs else lines[-1]
    # 히스토리/보고서에 저장되는 사유이므로 yt-dlp 오류 줄에 들어 있는 서명 URL 을 먼저 마스킹한 뒤 자른다
    return mask_text(f"{head} {body}" if head and head != body else body)[:300]


def _download_candidate(cand, *, item_url, item_name, out_dir, proxy, opts, force, on_progress,
                        group_id=None) -> dict:
    """후보 1개 다운로드 + 검증. status: ok | short | failed | cancelled.
    시도 결과(헤더 프로필별/엔진별)와 검증 정보는 downloads.json 에 기록한다."""
    r: dict[str, Any] = {}
    diag = get_diagnostics()
    safe_name = _reserve_name(item_name, item_url, out_dir, force)
    attempts: list[dict[str, Any]] = []
    rec: dict[str, Any] = {"candidate_id": cand.get("candidate_id"), "group_id": group_id, "url": cand.get("url"),
                           "classification": cand.get("classification"), "item_name": item_name}
    t0 = time.monotonic()
    try:
        with diag.stage("download"):
            ok, dl_log = download(
                cand,
                out_dir=str(out_dir),
                name=safe_name,
                proxy=proxy,
                total_timeout=opts.total_timeout,
                stall_timeout=opts.stall_timeout,
                show_progress=on_progress is None,
                on_progress=on_progress,
                cancel=CANCEL,
                attempt_log=attempts,
            )
        rec.update(attempts=attempts, elapsed_sec=round(time.monotonic() - t0, 1), download_log_tail=dl_log[-800:])
        r["download_log_tail"] = dl_log
        if dl_log.startswith("[CANCELLED"):
            r["status"] = "cancelled"
            diag.download(**rec, status="CANCELLED")
            return r
        if not ok:
            r.update(status="failed", reason=_short_reason(dl_log))
            if dl_log.startswith("[DEAD"):
                r["dead"] = True       # 서버가 제공 불가라고 답함 (폐기된 URL)
            if dl_log.startswith("[SECURITY]"):
                r["fatal"] = True      # 재시도해도 소용없음
            diag.download(**rec, status="FAILED", reason=r["reason"])
            log_event("download", "download_failed", f"다운로드 실패: {r['reason'][:150]}", level="WARNING",
                      candidate_id=cand.get("candidate_id"))
            return r

        media_files = [
            f for f in out_dir.iterdir()
            if f.is_file() and f.stem == safe_name and f.suffix.lower() in MEDIA_EXTS
        ]
        if not media_files:
            r.update(status="failed", reason="output_missing")
            diag.download(**rec, status="DOWNLOAD_INVALID", reason="output_missing")
            return r

        v_ok, meta = verify(media_files[0])
        r.update(output=str(media_files[0]), meta=meta)
        rec["verify"] = meta
        if not v_ok:
            try:
                media_files[0].unlink()      # 손상 파일 제거 후 재시도
            except OSError:
                pass
            r.pop("output", None)
            r.update(status="failed", reason=f"DOWNLOAD_INVALID: {meta}")
            diag.download(**rec, status="DOWNLOAD_INVALID", reason=str(meta))
            log_event("download", "download_invalid", f"검증 실패(DOWNLOAD_INVALID): {meta}", level="WARNING",
                      candidate_id=cand.get("candidate_id"))
            return r

        d = _meta_num(meta, "duration")
        r["duration"] = d
        # 사전 확인으로 길이를 알 수 없었던 경우: 받은 뒤 길이 재확인
        if opts.skip_under > 0 and d is not None and 0 < d <= opts.skip_under:
            try:
                media_files[0].unlink()
            except OSError:
                pass
            r.pop("output", None)
            r.update(status="short", duration=d)
            diag.download(**rec, status="SKIPPED_SHORT", duration=d)
            return r

        r["status"] = "ok"
        diag.download(**rec, status="SUCCESS", output=media_files[0].name, duration=d)
        log_event("download", "download_ok", f"다운로드·검증 성공: {media_files[0].name}",
                  candidate_id=cand.get("candidate_id"), duration=d)
        return r
    except Exception as e:
        log_exception("download", e, candidate_id=cand.get("candidate_id"))
        diag.download(**rec, status="ERROR", reason=f"{type(e).__name__}: {e}")
        raise
    finally:
        _release_name(safe_name)


def _backoff(attempt: int, name: str, reason: str, limit: int) -> bool:
    """재시도 대기. 중단 요청이 오면 True."""
    delay = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)] + random.uniform(0, 3)
    log(f"  [RETRY {attempt}/{limit - 1}] {name}: {str(reason)[:80]} → {delay:.0f}초 후 재시도")
    return CANCEL.wait(delay)


def run_target(
    t: dict,
    out_dir: Path,
    history: HistoryManager | None = None,
    proxy: str | None = None,
    headless: bool = True,
    force: bool = False,
    opts: Options | None = None,
    board: ProgressBoard | None = None,
    slot: int = 0,
    browser_sem: threading.Semaphore | None = None,
) -> dict:
    """게시글 1개 처리. 안에 동영상이 여러 개면 순차(sequential)로 하나씩 처리한다."""
    opts = opts or Options()
    url = t["page_url"]
    name = t.get("name") or sanitize_filename(url)
    post_date = t.get("date")
    proxy = proxy or t.get("proxy")
    entry: dict[str, Any] = {"name": name, "url": url, "license": t.get("license")}

    # --- 글 단위 중복 검사(날짜 무관, URL 기준) + 진행중 선점
    if history:
        if not force and history.is_downloaded(url):
            log(f"  [SKIP] 이미 다운로드 완료: {name}")
            entry.update(status="skipped", reason="already_downloaded")
            return entry
        if not force and opts.cooldown_hours > 0 and history.in_cooldown(url, opts.cooldown_hours, opts.cooldown_after):
            log(f"  [SKIP] 반복 실패로 {opts.cooldown_hours:g}시간 보류 중: {name} (--force 로 무시)")
            log_event("target", "cooldown_skip", "반복 실패 보류로 건너뜀", page_url=url,
                      cooldown_hours=opts.cooldown_hours, cooldown_after=opts.cooldown_after)
            entry.update(status="skipped", reason="cooldown")
            return entry
            log(f"  [SKIP] 다른 작업에서 진행 중: {name}")
            entry.update(status="skipped", reason="in_progress")
            return entry

    try:
        # --- (옵션) 파일명으로 기존 파일 확인: history 유실/URL 변경 대비 안전망
        if opts.library and not force:
            hit = opts.library.find(name)
            if hit:
                log(f"  [SKIP] 기존 파일과 이름 일치: {name}")
                if history:
                    history.record_success(url, name, str(hit), post_date=post_date,
                                           extra={"matched_by": "filename"})
                entry.update(status="skipped", reason="exists_in_library", output=str(hit))
                return entry

        results: dict[int, dict] = {}
        finished: set[int] = set()
        n_total = 0
        last_error = "no_media_found"

        for attempt in range(1, opts.attempts + 1):
            if CANCEL.is_set():
                break
            entry["attempts"] = attempt
            if board is not None:
                board.update(slot, name, "탐색" if attempt == 1 else f"재시도{attempt}", 0)

            groups = None
            nav_fatal = False
            try:
                with get_diagnostics().stage("media_discovery"):
                    groups = _discover_groups(t, url, proxy, headless, attempt, browser_sem)
            except DiscoverNavigationError as e:
                last_error = mask_text(f"navigation: {e}")
                nav_fatal = getattr(e, "status", None) in (404, 410, 451)   # 재시도 무의미
                log_event("navigation", "navigation_error", f"탐색 중 페이지 이동 실패(시도 {attempt}): {e}",
                          level="WARNING", page_url=url, status=getattr(e, "status", None), fatal=nav_fatal)
                get_diagnostics().fail("navigation", last_error)
            except Exception as e:
                last_error = mask_text(str(e))
                log_exception("media_discovery", e, page_url=url, attempt=attempt)
                get_diagnostics().fail("media_discovery", f"{type(e).__name__}: {last_error}")

            if not groups:   # None(탐색 오류) 또는 [](동영상 없음)
                if groups is not None:
                    last_error = "no_media_found"
                    log_event("media_discovery", "no_media_found", f"미디어를 찾지 못함(시도 {attempt}/{opts.attempts})",
                              level="WARNING", page_url=url)
                limit = min(2, opts.attempts) if groups is not None else opts.attempts
                if nav_fatal or attempt >= limit or _backoff(attempt, name, last_error, limit):
                    break
                continue

            n_total = len(groups)
            if n_total > 1:
                log(f"  → 영상 {n_total}개 순차 처리: {name}")
            for g in groups:                      # ← 순차 처리
                if CANCEL.is_set():
                    break
                idx = g["index"]
                if idx in finished:               # 이전 시도에서 이미 끝난 항목은 건너뜀
                    continue
                with diag_context(item=idx):
                    res = _process_item(
                        group=g, n=n_total, url=url, name=name, out_dir=out_dir, proxy=proxy,
                        opts=opts, history=history, post_date=post_date, force=force,
                        board=board, slot=slot,
                    )
                results[idx] = res
                if res["status"] in DONE_STATES or res.get("fatal"):
                    finished.add(idx)
                elif res["status"] == "cancelled":
                    break

            pending = [g for g in groups if g["index"] not in finished]
            if not pending or CANCEL.is_set() or attempt >= opts.attempts:
                break
            why = next((results[g["index"]].get("reason") for g in pending if g["index"] in results), "failed")
            if _backoff(attempt, name, why or "failed", opts.attempts):
                break

        # ------------------------------------------------ 결과 집계
        items = [results[i] for i in sorted(results)]
        entry["videos"] = n_total

        if not items:
            status = "cancelled" if CANCEL.is_set() else (
                "no_media_found" if last_error == "no_media_found" else "failed")
            entry.update(status=status, error=last_error)
            if history and status != "cancelled":
                history.record_failed(url, name, last_error, post_date=post_date)
                log(f"  [FAIL] {name}: {last_error[:120]}")
            return entry

        if n_total == 1 and len(items) == 1:
            entry.update({k: v for k, v in items[0].items()
                          if k not in ("index", "name", "url", "status", "fatal")})
        else:
            entry["items"] = items

        good = [r for r in items if r["status"] in DONE_STATES]
        ok_items = [r for r in items if r["status"] == "ok"]
        bad = [r for r in items if r["status"] not in DONE_STATES]
        incomplete = len(items) < n_total

        if CANCEL.is_set() and (bad or incomplete):
            status = "cancelled"
        elif bad or incomplete:
            status = "partial" if good else (bad[0]["status"] if bad else "failed")
            if history:
                for r in bad:
                    if r["status"] != "cancelled":
                        history.record_failed(r["url"], r["name"],
                                              r.get("reason") or r["status"], post_date=post_date)
            for r in bad:
                log(f"  [FAIL] {r['name']}: {str(r.get('reason') or r['status'])[:120]}")
        else:
            if ok_items:
                status = "ok"
            elif all(r["status"] == "skipped_short" for r in items):
                status = "skipped_short"
            else:
                status = "skipped"
            # 여러 영상 글: 전부 처리되면 글 단위 기록을 남겨 다음 실행에서 탐색 자체를 생략
            if n_total > 1 and history:
                if any(r["status"] in ("ok", "skipped") for r in items):
                    first_out = next((r.get("output") for r in ok_items if r.get("output")), "")
                    history.record_success(url, name, first_out or "", post_date=post_date,
                                           extra={"videos": n_total})
                else:
                    history.record_skipped(url, name, "all videos are short", post_date=post_date)
        entry["status"] = status
        return entry
    finally:
        if history:
            history.release(url)


# ------------------------------------------------------------------ batch
def _run_batch(
    targets: list[dict],
    history: HistoryManager,
    proxy: str | None,
    headless: bool,
    force: bool,
    opts: Options,
) -> list[dict]:
    if not targets:
        return []
    results: list[dict | None] = [None] * len(targets)
    sem = threading.Semaphore(max(1, min(opts.workers, MAX_CONCURRENT_BROWSERS)))
    board = ProgressBoard(len(targets))
    set_board(board)
    board.start()

    def work(i: int, t: dict) -> tuple[int, dict]:
        # 동시 접속 폭주(ERR_CONNECTION_RESET 유발) 방지: 첫 배치는 시차를 두고 시작
        if i < opts.workers and CANCEL.wait(i * 1.5):
            return i, {"name": t.get("name"), "url": t.get("page_url"), "status": "cancelled"}
        with diag_context(target=t.get("name"), page_url=t.get("page_url")):
            log_event("target", "target_start", f"대상 처리 시작: {t.get('name')}", level="DEBUG")
            try:
                res = run_target(
                    t, OUT_DIR, history=history, proxy=proxy, headless=headless, force=force,
                    opts=opts, board=board, slot=i, browser_sem=sem,
                )
            except Exception as e:
                log_exception("target", e)
                res = {"name": t.get("name"), "url": t.get("page_url"), "status": "error",
                       "error": f"{type(e).__name__}: {e}"}
            log_event("target", "target_end", f"대상 처리 종료: {res.get('status')}", level="INFO",
                      status=res.get("status"), reason=res.get("reason") or res.get("error"))
        board.finish(i, res.get("status", "error"))
        CANCEL.wait(random.uniform(1, 3))  # 사이트 부하 완화
        return i, res

    ex = ThreadPoolExecutor(max_workers=max(1, opts.workers))
    futs = [ex.submit(work, i, t) for i, t in enumerate(targets)]
    try:
        for _ in as_completed(futs):
            pass
    except KeyboardInterrupt:
        CANCEL.set()
        log("\n[중단] Ctrl+C 감지 - 진행 중인 작업을 정리합니다...")
        log_event("run", "user_interrupt", "사용자 중단(Ctrl+C)", level="WARNING")
        for f in futs:
            f.cancel()
    finally:
        ex.shutdown(wait=True)
        board.stop()
        set_board(None)

    for f in futs:
        if f.cancelled():
            continue
        try:
            i, res = f.result()
            results[i] = res
        except Exception as e:
            log_exception("batch", e)
    return [r for r in results if r]


def _print_summary(report: list[dict]) -> None:
    counts: dict[str, int] = {}
    for r in report:
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
    log("결과 요약: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def _write_report(report: list[dict], path: Path) -> None:
    """보고서는 쿠키/요청 헤더를 제외하고 URL 의 인증 값을 마스킹해서 저장한다. 같은 내용을 실행 로그에도 복사."""
    clean = sanitize(report)
    with path.open("w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    get_diagnostics().write_json(path.name, clean)


# ------------------------------------------------------------------ history setup
def _migrate_history_location(dst: Path) -> None:
    """예전 위치(out/history)의 기록을 out/ 밖의 history_data/ 로 병합 이전."""
    old = OUT_DIR / "history"
    if not old.is_dir() or old.resolve() == dst.resolve():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for f in old.glob("*.json"):
        try:
            src = json.loads(f.read_text(encoding="utf-8")) or {}
            tgt_path = dst / f.name
            tgt = json.loads(tgt_path.read_text(encoding="utf-8")) if tgt_path.is_file() else {}
            for k, v in src.items():
                tgt.setdefault(k, v)
            tmp = tgt_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(tgt, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(tgt_path)
        except Exception as e:
            log(f"[History Migration Warning] {f.name}: {e} → 원본을 그대로 둡니다.")
            log_exception("history", e)
            return
    backup = OUT_DIR / f"history.migrated-{datetime.now():%Y%m%d%H%M%S}"
    old.rename(backup)
    log(f"[History] out/history → {dst} 로 이전 완료 (원본 백업: {backup.name})")


def _open_history(opts: Options) -> HistoryManager:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_history_location(opts.history_dir)
    h = HistoryManager(opts.history_dir, require_file=opts.require_file,
                       legacy_file=OUT_DIR / "history.json")
    st = h.get_stats()
    log(f"[History] {opts.history_dir} - 기록 {st['entries_count']}건 / 파일 {st['dates_count']}개 "
        f"(파일 존재 확인: {'ON' if opts.require_file else 'OFF'})")
    if st["entries_count"] == 0:
        n_media = sum(1 for f in OUT_DIR.glob("*") if f.is_file() and f.suffix.lower() in MEDIA_EXTS)
        if n_media:
            log(f"[History 경고] 기록이 0건인데 {OUT_DIR} 에 미디어 파일이 {n_media}개 있습니다.\n"
                f"  → 이전 history 폴더를 {opts.history_dir} 로 복사하거나, "
                f"--match-name / --library 로 기존 파일명을 기준으로 건너뛰세요.")
    return h


def run_targets(
    targets_path: Path | None = None,
    only_name: str | None = None,
    proxy: str | None = None,
    headless: bool = True,
    force: bool = False,
    opts: Options | None = None,
) -> list[dict]:
    opts = opts or Options()
    targets_path = targets_path or ROOT / "targets.yaml"
    history = _open_history(opts)

    targets = _load_targets(targets_path)
    if only_name:
        targets = [t for t in targets if t["name"] == only_name]
        if not targets:
            raise SystemExit(f"targets.yaml에 '{only_name}' 대상이 없습니다.")

    report = _run_batch(targets, history, proxy, headless, force, opts)

    report_path = OUT_DIR / "report.json"
    _write_report(report, report_path)
    _print_summary(report)
    log(f"Report: {report_path}")
    return report


def run_crawl_and_download(
    base_url: str,
    start_page: int = 1,
    end_page: int = 1,
    since_date: str | None = None,
    until_date: str | None = None,
    include_undated: bool = True,
    proxy: str | None = None,
    headless: bool = True,
    force: bool = False,
    max_posts: int | None = None,
    wait_ms: int = 8000,
    click_selector: str | None = None,
    opts: Options | None = None,
) -> list[dict]:
    opts = opts or Options()
    history = _open_history(opts)

    since_dt = parse_post_date(since_date) if since_date else None
    until_dt = parse_post_date(until_date) if until_date else None

    log("=== [배치 크롤링 및 다운로드 시작] ===")
    log(f"기준 URL: {base_url}")
    log(f"페이지 범위: {start_page} ~ {end_page} / 동시 다운로드: {opts.workers} / 시도: {opts.attempts}"
        f" / {opts.skip_under:.0f}초 이하 영상 스킵")
    if since_dt:
        log(f"날짜 필터: >= {since_dt.isoformat()}")
    if until_dt:
        log(f"날짜 필터: <= {until_dt.isoformat()}")
    if proxy:
        log("프록시 사용")

    total_candidates: list[dict[str, Any]] = []
    seen_keys: set[str] = set()   # 페이지 밀림/사이드바 위젯으로 인한 중복 후보 제거

    for page_num in range(start_page, end_page + 1):
        target_page_url = build_page_url(base_url, page_num)
        log(f"\n[Crawl Page {page_num}/{end_page}] {target_page_url}")
        with get_diagnostics().stage("crawl"):
            posts = crawl_page(target_page_url, proxy=proxy, headless=headless)
        log(f"  → 발견된 게시글 수: {len(posts)}")
        if not posts:
            log("  → 게시글을 발견하지 못했거나 페이지 접근에 실패했습니다.")
            get_diagnostics().fail("crawl", f"게시글 없음 또는 접근 실패: {mask_text(target_page_url)}")

        for p in posts:
            key = normalize_url(p["url"])
            if key in seen_keys:
                continue
            seen_keys.add(key)

            if not is_date_in_range(p.get("date_obj"), since=since_dt, until=until_dt,
                                    include_undated=include_undated):
                log(f"  [DATE SKIP] 날짜 조건 불일치 ({p.get('date')}): {p['title']}")
                continue

            # 날짜와 무관하게 URL 기준으로 중복 판정
            if not force and history.is_downloaded(p["url"]):
                log(f"  [DUP SKIP] 이미 다운로드 완료됨: {p['title']} ({p.get('date')})")
                continue

            total_candidates.append(p)
            if max_posts and len(total_candidates) >= max_posts:
                log(f"\n최대 다운로드 대상 개수({max_posts}개)에 도달했습니다.")
                break

        if max_posts and len(total_candidates) >= max_posts:
            break
        time.sleep(2)

    log(f"\n=== 다운로드 대상 선별 완료 (총 {len(total_candidates)}개) ===")

    default_click = (
        "iframe >> video, .plyr__control--overlaid, "
        "button[aria-label*='Play'], .vjs-big-play-button"
    )
    targets = [
        {
            "name": sanitize_filename(post["title"]),
            "page_url": post["url"],
            "date": post.get("date"),
            "wait_ms": wait_ms,
            "click_selector": click_selector or default_click,
        }
        for post in total_candidates
    ]

    report = _run_batch(targets, history, proxy, headless, force, opts)

    report_path = OUT_DIR / "report_crawl.json"
    _write_report(report, report_path)
    _print_summary(report)
    log(f"Crawl Report: {report_path}")
    return report


# ------------------------------------------------------------------ run status
def _summarize_report(report: list[dict] | None) -> tuple[str, str | None, str | None]:
    """보고서에서 실행 전체 상태(OK/PARTIAL/FAILED/CANCELLED/ERROR)와 첫 실패 단계·사유를 도출."""
    if CANCEL.is_set():
        return "CANCELLED", "run", "사용자 중단"
    if not report:
        return "OK", None, None
    stage_of = {"no_media_found": "media_discovery", "no_main_found": "classification", "error": "exception",
                "failed": "download", "partial": "download"}
    good = {"ok", "skipped", "skipped_short"}
    bad = [r for r in report if r.get("status") not in good]
    if not bad:
        return "OK", None, None
    first = bad[0]
    reason = first.get("reason") or first.get("error")
    if not reason:
        sub = next((it for it in first.get("items", []) if it.get("status") not in good), None)
        reason = (sub or {}).get("reason") or first.get("status")
    stage = stage_of.get(first.get("status"), "download")
    if str(reason).startswith("navigation"):
        stage = "navigation"
    has_ok = any(r.get("status") in good for r in report)
    status = "ERROR" if all(r.get("status") == "error" for r in report) else ("PARTIAL" if has_ok else "FAILED")
    return status, stage, mask_text(str(reason))[:300]


# ------------------------------------------------------------------ CLI
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="비디오 자동 탐색 및 일괄 다운로드 파이프라인")

    parser.add_argument("--targets", type=Path, default=ROOT / "targets.yaml",
                        help="targets.yaml 설정 파일 경로 (기본값: targets.yaml)")
    parser.add_argument("--only", metavar="NAME", help="특정 target name만 실행")
    parser.add_argument("--url", metavar="POST_URL", help="글 1개만 바로 처리하고 결과(debug 포함)를 출력 (테스트/재시도용)")
    parser.add_argument("--discover-only", action="store_true",
                        help="미디어 다운로드 없이 스트림 URL만 탐색/확인")

    parser.add_argument("--crawl", metavar="URL",
                        help="일괄 다운로드할 카탈로그 기본 URL (예: https://s28.testonline.co)")
    parser.add_argument("--start-page", type=int, default=1, help="순회 시작 페이지 (기본값: 1)")
    parser.add_argument("--end-page", type=int, default=1, help="순회 종료 페이지 (기본값: 1)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="지정 날짜 이후(포함) 게시글만")
    parser.add_argument("--until", metavar="YYYY-MM-DD", help="지정 날짜 이전(포함) 게시글만")
    parser.add_argument("--max-posts", type=int, help="다운로드할 최대 게시글 개수")

    parser.add_argument("--proxy", metavar="PROXY_URL", help="HTTP/SOCKS5 프록시 URL")
    parser.add_argument("--headful", action="store_true", help="브라우저 창을 띄워 실행")
    parser.add_argument("--force", action="store_true", help="중복 검사를 무시하고 강제 재다운로드")

    parser.add_argument("--workers", type=int, default=5, help="동시에 처리할 게시글 수 (기본값: 5)")
    parser.add_argument("--attempts", type=int, default=3, help="항목당 총 시도 횟수 (기본값: 3)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TOTAL_TIMEOUT,
                        help="파일당 최대 소요 시간(초)")
    parser.add_argument("--stall-timeout", type=int, default=DEFAULT_STALL_TIMEOUT,
                        help="진행이 없을 때 중단하는 시간(초)")
    parser.add_argument("--skip-under", type=float, default=60.0, metavar="SEC",
                        help="이 길이(초) 이하의 영상은 건너뜀 (기본값: 60, 0이면 끄기)")

    parser.add_argument("--history-dir", type=Path, default=HISTORY_DIR,
                        help=f"history 저장 폴더 (기본값: {HISTORY_DIR.name}/, out/ 밖)")
    parser.add_argument("--verify-files", action="store_true",
                        help="history에 기록된 파일이 실제로 존재할 때만 '다운로드됨'으로 간주 (기본: history만 신뢰)")
    parser.add_argument("--match-name", action="store_true",
                        help="파일명이 같은 기존 파일(out/)이 있으면 건너뜀 (history 유실/URL 변경 대비)")
    parser.add_argument("--library", type=Path, action="append", metavar="DIR",
                        help="옮겨 둔 파일이 있는 폴더(여러 번 지정 가능). 파일명 일치 시 건너뜀")

    parser.add_argument("--cooldown-hours", type=float, default=0.0, metavar="H",
                        help="같은 글이 반복 실패(--cooldown-after 회 이상)하면 H시간 동안 재탐색을 보류 (기본값: 0=끄기)")
    parser.add_argument("--cooldown-after", type=int, default=2, metavar="N",
                        help="--cooldown-hours 적용 기준이 되는 누적 실패 횟수 (기본값: 2)")
    parser.add_argument("--log-retain", type=int, default=DEFAULT_RETAIN, metavar="N",
                        help=f"logs/runs 에 보관할 최근 실행 수 (기본값: {DEFAULT_RETAIN}, 0이면 정리 안 함)")
    return parser


def _dispatch(args: argparse.Namespace, opts: Options, headless: bool) -> list[dict] | None:
    if args.url:
        from urllib.parse import unquote, urlparse
        slug = unquote(urlparse(args.url).path.strip("/").split("/")[-1]) or "video"
        t = {
            "name": sanitize_filename(slug), "page_url": args.url, "wait_ms": 8000,
            "click_selector": "iframe >> video, .plyr__control--overlaid, button[aria-label*='Play'], .vjs-big-play-button",
        }
        history = _open_history(opts)
        report = _run_batch([t], history, args.proxy, headless, args.force, opts)
        print(json.dumps(sanitize(report), ensure_ascii=False, indent=2))
        return report

    if args.crawl:
        return run_crawl_and_download(
            base_url=args.crawl,
            start_page=args.start_page,
            end_page=args.end_page,
            since_date=args.since,
            until_date=args.until,
            proxy=args.proxy,
            headless=headless,
            force=args.force,
            max_posts=args.max_posts,
            opts=opts,
        )

    if args.discover_only:
        for t in _load_targets(args.targets):
            if args.only and t["name"] != args.only:
                continue
            log(f"\n=== Discovering: {t['name']} ===")
            with diag_context(target=t["name"], page_url=t["page_url"]):
                try:
                    groups = discover_videos(
                        t["page_url"],
                        wait_ms=t.get("wait_ms", 6000),
                        click_selector=t.get("click_selector"),
                        proxy=args.proxy or t.get("proxy"),
                        headless=headless,
                    )
                except DiscoverNavigationError as e:
                    log(f"  [Navigation Error] {e}")
                    log_event("navigation", "navigation_error", str(e), level="WARNING", status=getattr(e, "status", None))
                    continue
            log(f"  영상 {len(groups)}개")
            for g in groups:
                best = select_best(g["candidates"], proxy=args.proxy or t.get("proxy"))
                dur = probe_duration(best, proxy=args.proxy or t.get("proxy")) if best else None
                dur_s = f"{dur:.0f}s" if dur is not None else "길이 미확인"
                extra = " (근거 약함)" if g.get("low_confidence") else ""
                log(f"  #{g['index']:02d} {dur_s}{extra}\t{mask_text(best['url']) if best else None}")
        return None

    return run_targets(
        targets_path=args.targets,
        only_name=args.only,
        proxy=args.proxy,
        headless=headless,
        force=args.force,
        opts=opts,
    )


def main() -> None:
    disable_quickedit()  # Windows 콘솔 클릭 시 멈춤 방지
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = _build_parser().parse_args()
    headless = not args.headful

    # 진단 세션은 기본 활성화: 성공/실패/예외/중단 어떤 경로로 끝나도 summary.json 이 남는다.
    diag = init_diagnostics(LOG_DIR, retain=args.log_retain, meta={
        "mode": "url" if args.url else "crawl" if args.crawl else "discover_only" if args.discover_only else "targets",
        "workers": args.workers, "attempts": args.attempts, "skip_under": args.skip_under,
        "headless": headless, "proxy": bool(args.proxy), "force": args.force,
    })
    diag.add_snapshot("blacklist.json", blacklist.snapshot)
    log(f"[진단] 실행 ID {diag.run_id} → 로그: {diag.dir}")

    status, f_stage, f_reason = "OK", None, None
    report: list[dict] | None = None
    try:
        library = None
        if args.match_name or args.library:
            library = Library([OUT_DIR, *(args.library or [])])
            log(f"[Library] 파일명 매칭용 {len(library)}개 파일 인덱싱")

        opts = Options(
            workers=max(1, args.workers),
            attempts=max(1, args.attempts),
            total_timeout=args.timeout,
            stall_timeout=args.stall_timeout,
            require_file=args.verify_files,
            skip_under=max(0.0, args.skip_under),
            cooldown_hours=max(0.0, args.cooldown_hours),
            cooldown_after=max(1, args.cooldown_after),
            history_dir=args.history_dir,
            library=library,
        )
        report = _dispatch(args, opts, headless)
        status, f_stage, f_reason = _summarize_report(report)
    except KeyboardInterrupt:
        CANCEL.set()
        status, f_stage, f_reason = "CANCELLED", "run", "사용자 중단(Ctrl+C)"
        log_event("run", "user_interrupt", "사용자 중단(Ctrl+C)", level="WARNING")
        log("\n[중단] 사용자 요청으로 종료합니다.")
    except SystemExit as e:
        status, f_stage, f_reason = ("OK", None, None) if e.code in (None, 0) else ("FAILED", "startup", str(e.code))
        raise
    except Exception as e:
        status, f_stage, f_reason = "ERROR", "exception", f"{type(e).__name__}: {e}"
        log_exception("run", e)
        raise
    finally:
        diag.finalize(status, f_stage, f_reason)
        print(f"[진단] 실행 상태: {status} / 로그 폴더: {diag.dir}", flush=True)
        if diag.zip_path:
            print(f"[진단] 전달용 ZIP: {diag.zip_path}", flush=True)


if __name__ == "__main__":
    main()