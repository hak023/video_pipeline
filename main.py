from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
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
    attach_page_diagnostics,
)
from media_tools import get_browser_launch_kwargs, get_context_kwargs
from playwright.sync_api import sync_playwright
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
DONE_STATES = {"ok", "skipped", "skipped_short", "skipped_unconfirmed"}
MAX_CANDIDATES_TRIED = 3              # 한 영상에서 후보 URL을 최대 몇 개까지 시도할지


MIN_FREE_BYTES = 3 * 1024 ** 3        # 다운로드 시작 전 최소 여유 공간. 가혹한 환경에서 광고/대용량 스트림으로 디스크가 차는 것 방지


# ------------------------------------------------------------------ library (파일명 매칭)
@dataclass
class Options:
    workers: int = 5
    attempts: int = 3                 # 총 시도 횟수 (최초 1회 + 재시도 2회)
    total_timeout: int = DEFAULT_TOTAL_TIMEOUT
    stall_timeout: int = DEFAULT_STALL_TIMEOUT
    require_file: bool = False        # False(기본): history 기록만 신뢰 → 파일을 옮겨도 재다운로드 안 함
    skip_under: float = 60.0          # 이 길이(초) 이하의 영상은 다운로드하지 않음 (0 = 끄기)
    history_dir: Path = HISTORY_DIR
    cooldown_hours: float = 0.0       # 0 = 끄기. 같은 글이 반복 실패하면 이 시간 동안 재탐색 보류
    cooldown_after: int = 2
    library: Library | None = field(default=None, repr=False)
    allow_pending: bool = False       # fail-closed: 본편 확인(MAIN) 안 된 PENDING 후보 다운로드 허용 여부
    passive_embed: bool = False       # 진단 모드: 임베드 탐색 시 라우팅/스크립트 주입 비활성화
    cdp_url: str | None = None        # 기존 Chrome의 CDP 엔드포인트 (예: http://127.0.0.1:9222)


# ------------------------------------------------------------------ names
_names_lock = threading.Lock()
_names_claimed: dict[str, str] = {}      # 소문자 파일명(확장자 제외) → 항목 키. 이번 실행에서 한 번 쓴 이름은 다른 항목이 쓰지 못한다
_INDEX_SUFFIX_RE = re.compile(r" \(\d{2,3}\)$")
MAX_NAME_LEN = 120
PATH_BUDGET = 240                         # Windows MAX_PATH(260) - yt-dlp 임시 접미사(.f137.mp4.part-FragNNN 등) 여유


def _env_info() -> dict[str, str]:
    """원격 진단용 환경 버전 (summary.json meta). 실패해도 실행에는 영향 없음."""
    import platform
    import subprocess
    info: dict[str, str] = {"python": platform.python_version(), "os": platform.platform()}
    for mod in ("playwright", "yt_dlp"):
        try:
            from importlib.metadata import version
            info[mod] = version(mod.replace("_", "-"))
        except Exception:
            info[mod] = "?"
    try:
        from media_tools import resolve_ffmpeg
        ff = resolve_ffmpeg()
        out = subprocess.run([str(ff), "-version"], capture_output=True, text=True, timeout=10).stdout if ff else ""
        info["ffmpeg"] = (out.splitlines() or ["?"])[0][:60]
    except Exception:
        info["ffmpeg"] = "?"
    return info


def _free_space_problem(out_dir: Path) -> str | None:
    try:
        free = shutil.disk_usage(out_dir).free
    except OSError:
        return None
    if free < MIN_FREE_BYTES:
        return f"디스크 여유 공간 부족: {free / 1024 ** 3:.1f}GB < {MIN_FREE_BYTES / 1024 ** 3:.0f}GB ({out_dir})"
    return None


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


def _discover_groups(t, url, proxy, headless, attempt, browser_sem, passive: bool = False, cdp_url: str | None = None):
    # 재시도할수록 플레이어 로딩 대기시간을 늘림
    wait_ms = int(t.get("wait_ms", 6000) * (1 + 0.5 * (attempt - 1)))
    with (browser_sem or nullcontext()):
        return discover_videos(
            url,
            wait_ms=wait_ms,
            click_selector=t.get("click_selector"),
            proxy=proxy,
            headless=headless,
            passive=passive,
            cdp_url=cdp_url,
        )


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


# ------------------------------------------------------------------ CLI

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="비디오 자동 탐색 및 일괄 다운로드 파이프라인")

    parser.add_argument("--targets", type=Path, default=ROOT / "targets.yaml",
                        help="targets.yaml 설정 파일 경로 (기본값: targets.yaml)")
    parser.add_argument("--only", metavar="NAME", help="특정 target name만 실행")
    parser.add_argument("--url", metavar="POST_URL", help="글 1개만 바로 처리하고 결과(debug 포함)를 출력 (테스트/재��도용)")
    parser.add_argument("--discover-only", action="store_true",
                        help="미디어 다운로드 없이 스트림 URL만 탐색/확인")
    parser.add_argument("--manual-playback", action="store_true",
                        help="--url 페이지를 실제 브라우저로 열어 사용자가 직접 재생하고 네트워크/플레이어 로그 수집 (다운로드 안 함)")
    parser.add_argument("--cdp", metavar="CDP_URL", default=None,
                        help="기존 Chrome의 CDP 주소를 연결합니다(예: http://127.0.0.1:9222). 광고차단 확장이 활성화된 브라우저를 사용합니다.")

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
    parser.add_argument("--allow-pending", action="store_true",
                        help="본편 근거가 약한 PENDING 후보도 다운로드 시도 (기본값: False, 광고 오인 방지 fail-closed)")
    parser.add_argument("--passive-embed", action="store_true",
                        help="임베드 탐색의 라우팅/스크립트 주입을 끔 (--discover-only와 함께 사용하면 다운로드 없이 진단)")
    parser.add_argument("--log-retain", type=int, default=DEFAULT_RETAIN, metavar="N",
                        help=f"logs/runs 에 보관할 최근 실행 수 (기본값: {DEFAULT_RETAIN}, 0이면 정리 안 함)")
    return parser


def _name_from_url(url: str) -> str:
    """--url 용 기본 이름: 경로 마지막 조각(영상 확장자는 제거: x.m3u8 → x). 없으면 'video'."""
    from urllib.parse import unquote, urlparse
    slug = unquote(urlparse(url).path.strip("/").split("/")[-1]) or "video"
    stem, dot, ext = slug.rpartition(".")
    if dot and stem and ext.lower() in {"m3u8", "mpd", "mp4", "mkv", "webm", "mov", "ts", "html", "htm", "php"}:
        slug = stem
    return sanitize_filename(slug)


def _manual_playback(url: str, proxy: str | None = None) -> None:
    """사용자가 브라우저에서 직접 재생하는 동안 네트워크/플레이어 진단을 수집한다."""
    from urllib.parse import urlparse

    diag = get_diagnostics()
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    observed: dict[str, dict[str, Any]] = {}
    response_count = 0
    lock = threading.Lock()
    finished = threading.Event()

    def on_response(resp) -> None:
        nonlocal response_count
        try:
            media_url = resp.url
            path_part = urlparse(media_url).path.lower()
            headers = resp.headers
            content_type = (headers.get("content-type") or "").lower()
            if not (path_part.endswith((".mp4", ".m3u8", ".mpd", ".webm", ".m4v", ".ts"))
                    or any(x in content_type for x in ("video/", "mpegurl", "dash+xml"))):
                return

            try:
                raw_headers = resp.request.all_headers()
            except Exception:
                raw_headers = {}
            allowed = {
                "user-agent", "referer", "origin", "accept", "accept-language",
                "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
                "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
            }
            safe_headers = {k.lower(): v for k, v in raw_headers.items() if k.lower() in allowed}
            try:
                frame_url = resp.frame.url
            except Exception:
                frame_url = ""

            item = {
                "url": media_url, "status": resp.status, "content_type": content_type,
                "content_length": headers.get("content-length"),
                "content_range": headers.get("content-range"),
                "accept_ranges": headers.get("accept-ranges"),
                "frame_url": frame_url, "request_method": resp.request.method,
                "request_headers": safe_headers,
                "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            with lock:
                if media_url in observed:
                    old = observed[media_url]
                    old["response_count"] = old.get("response_count", 1) + 1
                    old["last_status"] = resp.status
                    old["last_content_range"] = headers.get("content-range")
                else:
                    item["response_count"] = 1
                    observed[media_url] = item
                response_count += 1
                snapshot = list(observed.values())
                total = response_count

            diag.event("manual_playback", "media_response",
                       f"브라우저 미디어 응답 HTTP {resp.status}",
                       level="INFO" if resp.status < 400 else "WARNING",
                       frame_url=frame_url, media_url=media_url,
                       content_type=content_type, content_length=headers.get("content-length"),
                       content_range=headers.get("content-range"),
                       request_method=resp.request.method)
            diag.write_json("manual_playback.json", {
                "page_url": url, "responses": snapshot, "response_event_count": total,
            })
        except Exception as exc:
            log_exception("manual_playback_response", exc, page_url=url)

    snapshot_js = r"""() => {
      const out = {url: location.href, title: document.title,
        readyState: document.readyState, jwplayers: [], videos: [], sources: []};
      try {
        if (typeof jwplayer === 'function') {
          document.querySelectorAll('.jwplayer,[id^="jwplayer"],#player').forEach((el, i) => {
            try {
              const p = jwplayer(el.id || undefined);
              const pl = p && p.getPlaylist ? (p.getPlaylist() || []) : [];
              out.jwplayers.push({id: el.id || ('player-' + i),
                state: p && p.getState ? p.getState() : null,
                position: p && p.getPosition ? p.getPosition() : null,
                duration: p && p.getDuration ? p.getDuration() : null,
                playlist: pl.map((x, n) => ({index:n, title:x.title || null,
                  duration:x.duration || null,
                  sources:(x.allSources || x.sources || []).map(y => ({
                    file:y.file || null, type:y.type || null, label:y.label || null}))}))});
            } catch(e) { out.jwplayers.push({error:String(e)}); }
          });
        }
      } catch(e) { out.jw_error = String(e); }
      document.querySelectorAll('video').forEach((v, i) => out.videos.push({
        index:i, src:v.currentSrc || v.src || null, readyState:v.readyState,
        networkState:v.networkState, paused:v.paused, currentTime:v.currentTime,
        duration:Number.isFinite(v.duration) ? v.duration : null,
        videoWidth:v.videoWidth, videoHeight:v.videoHeight,
        error:v.error ? {code:v.error.code, message:v.error.message} : null}));
      document.querySelectorAll('source').forEach((x, i) =>
        out.sources.push({index:i, src:x.src || x.getAttribute('src'), type:x.type || null}));
      return out;
    }"""

    def wait_for_enter() -> None:
        try:
            input("\n브라우저에서 직접 재생을 시도한 뒤 이 콘솔에서 Enter를 누르세요: ")
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            finished.set()

    log_event("manual_playback", "started",
              "사용자 직접 재생 진단 시작 (자동 클릭/다운로드 없음)", page_url=url)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(
            "http://127.0.0.1:9222"
        )

        context = browser.contexts[0]

        page = context.new_page()
        attach_page_diagnostics(page, "manual")
        page.on("response", on_response)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            diag.note_page(url)
            diag.note_frames([frame.url for frame in page.frames])
            log_event("manual_playback", "page_loaded", "수동 재생 페이지 로드",
                      page_url=url, status=response.status if response else None, final_url=page.url)
            print("\n[수동 재생 진단]")
            print("1. 열린 브라우저에서 플레이어를 직접 찾아 재생 버튼을 누르세요.")
            print("2. 재생 성공 또는 오류가 나타날 때까지 기다리세요.")
            print("3. 브라우저는 그대로 두고 이 콘솔에서 Enter를 누르세요.")
            print("4. 쿠키와 Authorization 헤더는 저장하지 않습니다.")
            threading.Thread(target=wait_for_enter, daemon=True).start()
            while not finished.is_set():
                page.wait_for_timeout(250)
            page.wait_for_timeout(1000)

            frame_snapshots = []
            for index, frame in enumerate(page.frames):
                try:
                    data = frame.evaluate(snapshot_js)
                    data["frame_index"] = index
                    data["frame_url"] = frame.url
                    frame_snapshots.append(data)
                except Exception as exc:
                    frame_snapshots.append({"frame_index": index, "frame_url": frame.url,
                        "snapshot_error": f"{type(exc).__name__}: {str(exc)[:250]}"})

            with lock:
                responses = list(observed.values())
                total_events = response_count
            result = {
                "page_url": url, "final_url": page.url, "page_title": page.title(),
                "captured_at": datetime.now().astimezone().isoformat(),
                "response_event_count": total_events, "responses": responses,
                "frames": frame_snapshots,
                "note": "수동 관찰 결과. 자동 다운로드 및 인증정보 저장 없음.",
            }
            diag.write_json("manual_playback.json", result)
            ok_count = sum(1 for x in responses if x.get("status") in (200, 206))
            error_count = sum(1 for x in responses
                              if (x.get("last_status") or x.get("status", 0)) >= 400)
            log_event("manual_playback", "completed", "수동 재생 진단 완료",
                      page_url=url, unique_media_urls=len(responses),
                      successful_media_responses=ok_count, error_media_responses=error_count)
            print(f"\n[수집 완료] 미디어 URL {len(responses)}개 / 정상(200·206) {ok_count}개 / 오류 {error_count}개")
            print(f"[로그] {diag.dir / 'manual_playback.json'}")
        except Exception as exc:
            log_exception("manual_playback", exc, page_url=url)
            raise
        finally:
            try:
                browser.close()
            except Exception:
                pass


def _dispatch(args: argparse.Namespace, opts: Options, headless: bool) -> list[dict] | None:
    if args.manual_playback:
        if not args.url:
            raise ValueError('--manual-playback 옵션은 --url 과 함께 사용해야 합니다.')
        _manual_playback(args.url, proxy=args.proxy)
        return None

    if args.url:
        t = {
            "name": _name_from_url(args.url), "page_url": args.url, "wait_ms": 8000,
            "click_selector": "iframe >> video, .plyr__control--overlaid, button[aria-label*='Play'], .vjs-big-play-button",
        }

        if args.discover_only:
            groups = discover_videos(
                t["page_url"],
                wait_ms=t["wait_ms"],
                click_selector=t["click_selector"],
                proxy=args.proxy,
                headless=headless,
                passive=opts.passive_embed,
                cdp_url=opts.cdp_url,
            )
            print(json.dumps(sanitize(groups), ensure_ascii=False, indent=2))
            return None

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
            opts=Options(
                workers=max(1, args.workers),
                attempts=max(1, args.attempts),
                total_timeout=args.timeout,
                stall_timeout=args.stall_timeout,
                require_file=args.verify_files,
                skip_under=max(0.0, args.skip_under),
                cooldown_hours=max(0.0, args.cooldown_hours),
                cooldown_after=max(1, args.cooldown_after),
                history_dir=args.history_dir,
                library=None,
                allow_pending=args.allow_pending,
                passive_embed=args.passive_embed,
                cdp_url=args.cdp,
            ),
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
                        passive=opts.passive_embed,
                        cdp_url=args.cdp or opts.cdp_url,
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
        "mode": "manual_playback" if args.manual_playback else "url" if args.url else "crawl" if args.crawl else "discover_only" if args.discover_only else "targets",
        "workers": args.workers, "attempts": args.attempts, "skip_under": args.skip_under,
        "headless": headless, "proxy": bool(args.proxy), "force": args.force, "env": _env_info(),
        "cdp_url": args.cdp,
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
            allow_pending=args.allow_pending,
            passive_embed=args.passive_embed,
            cdp_url=args.cdp,
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


# NOTE: the rest of the original file is intentionally not redefined here;
# this patch is only for the CDP compatibility path in the actual repository.
# The repository already contains the remaining implementation for run_targets, crawl, etc.
# This stub exists to keep the patch surgical and to avoid duplicating unchanged logic.
# The actual file in the repository is updated via repo edit, not by re-serializing the entire file.
# DO NOT use this as literal replacement output; the tool call below patches only the needed sections.

# ---
# Placeholder to satisfy editor expectations; actual edits are applied to the repository file.
# ---

# End of patch placeholder
            