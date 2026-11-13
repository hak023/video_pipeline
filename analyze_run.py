"""실행 로그(폴더 또는 ZIP) 요약 도구 — 사용자가 보낸 진단 자료를 빠르게 분석하기 위한 Phase 5 보조 도구.

사용: python analyze_run.py logs/zips/<run_id>.zip
      python analyze_run.py logs/runs/<run_id>
      python analyze_run.py            # logs/runs 의 가장 최근 실행
로그는 이미 마스킹되어 있으며, 이 도구는 네트워크/브라우저를 사용하지 않는다.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


class RunData:
    def __init__(self, path: Path):
        self.files: dict[str, str] = {}
        if path.is_file() and path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                for n in zf.namelist():
                    if n.endswith("/"):
                        continue
                    rel = n.split("/", 1)[1] if "/" in n else n
                    self.files[rel.replace("\\", "/")] = zf.read(n).decode("utf-8", "replace")
        elif path.is_dir():
            for f in path.rglob("*"):
                if f.is_file():
                    self.files[f.relative_to(path).as_posix()] = f.read_text(encoding="utf-8", errors="replace")
        else:
            raise FileNotFoundError(path)

    def json(self, name: str, default: Any = None) -> Any:
        try:
            return json.loads(self.files[name])
        except (KeyError, ValueError):
            return default

    def lines(self, name: str) -> list[str]:
        return [ln for ln in self.files.get(name, "").splitlines() if ln.strip()]

    def jsonl(self, name: str) -> list[dict]:
        out = []
        for ln in self.lines(name):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out


def diagnose(d: RunData) -> list[str]:
    """로그에서 읽어낼 수 있는 원인 후보. 확정이 아니라 다음에 볼 곳을 알려주는 힌트다."""
    hints: list[str] = []
    cands = d.json("candidates.json", []) or []
    players = d.json("players.json", []) or []
    downloads = d.json("downloads.json", []) or []
    summary = d.json("summary.json", {}) or {}
    if summary.get("status") == "RUNNING":
        hints.append("summary 가 RUNNING 상태: 프로세스가 비정상 종료(강제 종료/전원)되었을 수 있음. run.log 마지막 줄 확인")
    if not cands:
        if any(p.get("jw_present") and not p.get("jw_ready") for p in players):
            hints.append("후보 0개 + JWPlayer 초기화 미확인: 플레이어 로딩 실패/차단 가능. --headful 재시도, wait_ms 증가, "
                         "browser/page_errors.log 와 request_failures.jsonl 확인")
        elif players:
            hints.append("후보 0개: 플레이어는 열렸으나 미디어 요청이 없음. 재생 유도 실패 가능(players.json 의 play_result 확인)")
        else:
            hints.append("후보 0개 + 플레이어 없음: 임베드(iframe)를 찾지 못함. events.jsonl 의 no_embed 와 페이지 구조 확인")
    ads = [c for c in cands if c.get("classification") == "AD"]
    if cands and len(ads) == len(cands):
        soft = [c for c in ads if any(r in ("AD_STATIC_SOFT", "AD_DYNAMIC") for r in c.get("rules", []))]
        if soft:
            hints.append(f"전부 AD 로 판별, 그중 {len(soft)}개는 SOFT/동적 규칙 근거: 본편이 광고로 오탐됐을 가능성. "
                         "candidates.json 의 blacklist_match 확인 후 ad_blacklist.json 의 해당 항목 삭제 검토")
        else:
            hints.append("전부 AD 로 판별: 본편 후보가 수집되지 않았을 가능성(재생 유도/프레임 제한). excluded_reason 확인")
    others = [c for c in cands if str(c.get("excluded_reason", "")).startswith("other_frame")]
    if others:
        hints.append(f"{len(others)}개 후보가 플레이어 외 프레임에서 발생해 제외됨(other_frame): 플레이어 호스트 판정이 어긋났을 수 있음")
    if any(c.get("classification") == "PENDING" for c in cands):
        hints.append("PENDING 후보 존재: 근거 부족. 이 후보로 받은 파일은 history/report 에 low_confidence 로 표시됨 → 실제 본편인지 확인")
    for dl in downloads:
        if dl.get("status") in ("FAILED", "DOWNLOAD_INVALID"):
            reason = str(dl.get("reason", ""))
            if "403" in reason or "401" in reason:
                hints.append(f"{dl.get('candidate_id')}: HTTP 401/403 — attempts 의 profile_headers 별 결과 확인(Referer/Origin/쿠키)")
            elif dl.get("status") == "DOWNLOAD_INVALID":
                hints.append(f"{dl.get('candidate_id')}: 받은 파일 검증 실패 — {reason[:80]}")
    n_fail = len(d.lines("browser/request_failures.jsonl"))
    if n_fail >= 10:
        hints.append(f"브라우저 요청 실패 {n_fail}건: 네트워크/프록시/차단 확인 (browser/request_failures.jsonl)")
    return hints


def render(d: RunData) -> str:
    s = d.json("summary.json", {}) or {}
    out: list[str] = []
    w = out.append
    w(f"실행 {s.get('run_id')}  상태={s.get('status')}  소요={s.get('elapsed_sec')}s")
    if s.get("failure_stage") or s.get("failure_reason"):
        w(f"  실패 단계: {s.get('failure_stage')}  사유: {s.get('failure_reason')}")
    w("  집계: " + ", ".join(f"{k}={s.get(k)}" for k in (
        "page_count", "frame_count", "player_count", "candidate_count", "main_count", "ad_count",
        "pending_count", "download_success", "download_failure", "exception_count")))
    meta = s.get("meta") or {}
    if meta:
        w("  실행 옵션: " + ", ".join(f"{k}={v}" for k, v in meta.items()))

    report = d.json("report.json", []) or d.json("report_crawl.json", []) or []
    if report:
        w("\n[대상별 결과]")
        for r in report:
            w(f"  {r.get('status'):<14} {r.get('name')}  {r.get('reason') or r.get('error') or ''}"[:200])

    cands = d.json("candidates.json", []) or []
    if cands:
        w("\n[후보 판별]")
        for c in cands:
            w(f"  {c.get('candidate_id')} {c.get('classification'):<8} main={c.get('main_score')} ad={c.get('ad_score')} "
              f"group={c.get('group_id') or '-'} rules={','.join(c.get('rules', [])) or '-'}")
            w(f"      {c.get('url')}" + (f"  [제외: {c['excluded_reason']}]" if c.get("excluded_reason") else ""))

    players = d.json("players.json", []) or []
    if players:
        w("\n[플레이어]")
        for p in players:
            w(f"  {p.get('player_id')} jw_present={p.get('jw_present')} jw_ready={p.get('jw_ready')} play={p.get('play_result')} captured={p.get('captured')} "
              f"rejected={p.get('rejected')} popups={p.get('popups_closed')} wait={p.get('init_wait_sec')}s err={p.get('error', '-')}")

    dls = d.json("downloads.json", []) or []
    if dls:
        w("\n[다운로드]")
        for x in dls:
            engines = "; ".join(f"#{a.get('profile_no')}:{a.get('engine')}={'ok' if a.get('ok') else 'fail'}" for a in x.get("attempts", []))
            w(f"  {x.get('candidate_id')} {x.get('status'):<16} {x.get('group_id') or '-'} {engines}  {str(x.get('reason', ''))[:80]}")

    exc = sorted(k for k in d.files if k.startswith("exceptions/"))
    if exc:
        w("\n[예외]")
        for k in exc:
            head = [ln for ln in d.files[k].splitlines() if ln.startswith(("stage:", "type:"))]
            w(f"  {k}: {' / '.join(head)}")
    perr = d.lines("browser/page_errors.log")
    if perr:
        w(f"\n[페이지 JS 오류 {len(perr)}건] 처음 3건:")
        for ln in perr[:3]:
            w("  " + ln[:160])

    hints = diagnose(d)
    w("\n[진단 힌트]" if hints else "\n[진단 힌트] 특이사항 없음")
    for h in hints:
        w("  - " + h)
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if argv:
        path = Path(argv[0])
    else:
        runs = sorted((ROOT / "logs" / "runs").glob("*")) if (ROOT / "logs" / "runs").is_dir() else []
        if not runs:
            print("logs/runs 에 실행 기록이 없습니다.")
            return 1
        path = runs[-1]
    try:
        print(render(RunData(path)))
    except FileNotFoundError:
        print(f"찾을 수 없음: {path}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main(sys.argv[1:]))
