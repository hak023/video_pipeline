from __future__ import annotations

import json
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


def normalize_url(url: str) -> str:
    """게시글 고유 키 생성 (호스트 제외).

    사이트가 도메인을 바꿔도(s27→s28, .co→.tv, '새주소' 이동) 같은 글로 인식하도록
    호스트/스킴/쿼리/끝 슬래시/퍼센트 인코딩 차이/유니코드 NFC·NFD 를 모두 흡수한다.
    한 페이지에 동영상이 여러 개인 경우 '#v01' 형태의 항목 구분자는 유지한다.
    (여러 사이트를 한 history 로 섞어 쓰면 경로가 같은 글이 충돌할 수 있음)
    """
    try:
        p = urlparse(url.strip())
        path = unicodedata.normalize("NFC", unquote(p.path)).rstrip("/").lower()
        if not path and p.query:  # ?p=123 형태 permalink
            path = "?" + p.query.lower()
        frag = f"#{p.fragment}" if re.fullmatch(r"v\d{1,3}", p.fragment or "") else ""
        return f"{path or '/'}{frag}"
    except Exception:
        return url.strip().rstrip("/").lower()


class HistoryManager:
    """날짜별 파티션 파일(out/history/YYYY-MM-DD.json)에 기록하되,
    중복 판정은 '날짜와 무관한 URL 인덱스'로 수행한다.

    - 게시글 날짜가 바뀌거나(undated ↔ dated) 파싱이 달라져도 중복을 놓치지 않는다.
    - 스레드 안전 (RLock) + 진행 중(in-flight) 작업 claim 지원.
    """

    def __init__(self, base_dir: str | Path, require_file: bool = False,
                 legacy_file: str | Path | None = None):
        self.require_file = require_file
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, Any]] = {}      # date_key -> {record_key: record}
        self._loc: dict[str, dict[str, str]] = {}        # url_key -> {date_key: record_key}
        self._inflight: set[str] = set()

        base = Path(base_dir)
        if base.suffix == ".json":
            self.history_dir = base.parent / "history"
            self.legacy_file = base
        else:
            self.history_dir = base
            self.legacy_file = Path(legacy_file) if legacy_file else base.parent / "history.json"
        self.history_dir.mkdir(parents=True, exist_ok=True)

        self._migrate_legacy()
        self._load_all()

    # ------------------------------------------------------------ storage
    def _partition_path(self, date_key: str) -> Path:
        safe_key = "".join(c for c in date_key if c.isalnum() or c in "-_") or "undated"
        return self.history_dir / f"{safe_key}.json"

    def _load_partition(self, date_key: str) -> dict[str, Any]:
        if date_key in self._cache:
            return self._cache[date_key]
        path = self._partition_path(date_key)
        records: dict[str, Any] = {}
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as f:
                    records = json.load(f)
                if not isinstance(records, dict):
                    raise ValueError("not a dict")
            except Exception as e:
                # 손상 파일을 덮어써서 기록이 사라지는 것을 방지
                bad = path.with_suffix(".corrupt")
                try:
                    path.replace(bad)
                except OSError:
                    pass
                print(f"[History Warning] {path.name} 손상 → {bad.name} 로 이동 ({e})")
                records = {}
        self._cache[date_key] = records
        return records

    def _save_partition(self, date_key: str, records: dict[str, Any],
                        removed: set[str] | None = None) -> None:
        path = self._partition_path(date_key)
        # 다른 프로세스가 그 사이 추가한 기록을 덮어써서 잃지 않도록 병합
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as f:
                    disk = json.load(f)
                if isinstance(disk, dict):
                    for k, v in disk.items():
                        if k not in records and k not in (removed or ()):
                            records[k] = v
                            u = v.get("url") if isinstance(v, dict) else None
                            self._loc.setdefault(normalize_url(u or k), {})[date_key] = k
            except Exception:
                pass
        self._cache[date_key] = records
        if not records:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        tmp.replace(path)

    def _load_all(self) -> None:
        """모든 파티션을 읽어 URL→파티션 인덱스를 구성 (날짜 무관 중복 판정용)."""
        with self._lock:
            for p in self.history_dir.glob("*.json"):
                if p.stat().st_size <= 2:
                    continue
                date_key = p.stem
                for rk, rec in self._load_partition(date_key).items():
                    url = rec.get("url") if isinstance(rec, dict) else None
                    key = normalize_url(url or rk)  # 구버전 키도 새 규칙으로 재계산
                    self._loc.setdefault(key, {})[date_key] = rk

    def _migrate_legacy(self) -> None:
        if not self.legacy_file.is_file() or self.legacy_file.stat().st_size <= 2:
            return
        try:
            with self.legacy_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key, item in data.items():
                    post_date = item.get("post_date") or "undated"
                    recs = self._load_partition(post_date)
                    recs[key] = item
                    self._save_partition(post_date, recs)
                self.legacy_file.replace(self.legacy_file.with_suffix(".json.bak"))
                print(f"[History] 기존 {self.legacy_file.name} 을 날짜별 저장소로 마이그레이션했습니다.")
        except Exception as e:
            print(f"[History Migration Warning] {e}")

    def _upsert(self, key: str, date_key: str, record: dict[str, Any]) -> None:
        """같은 게시글의 다른 파티션 기록은 제거(이동)하고 현재 파티션에 기록."""
        locs = self._loc.setdefault(key, {})
        for d, rk in list(locs.items()):
            if d != date_key:
                recs = self._load_partition(d)
                recs.pop(rk, None)
                self._save_partition(d, recs, removed={rk})
                del locs[d]
        recs = self._load_partition(date_key)
        rk = locs.get(date_key, key)
        recs[rk] = record
        locs[date_key] = rk
        self._save_partition(date_key, recs)

    # ------------------------------------------------------------ queries
    def _verify_record(self, record: dict[str, Any] | None) -> bool:
        if not record:
            return False
        if record.get("status") == "skipped_short":
            return True  # 길이 조건으로 건너뛴 항목도 '처리 완료'로 간주
        if record.get("status") != "ok":
            return False
        if not self.require_file:
            return True
        out_file = record.get("output_file")
        if not out_file:
            return True
        # 프로젝트 폴더가 이동된 경우를 대비해 out/ 아래 같은 파일명도 확인
        for p in (Path(out_file), self.history_dir.parent / Path(out_file).name):
            try:
                if p.is_file() and p.stat().st_size > 0:
                    return True
            except OSError:
                pass
        return False

    def is_downloaded(self, url: str, post_date: str | None = None) -> bool:
        """URL 기준으로 판정한다. post_date 는 하위 호환용이며 판정에 사용하지 않는다."""
        key = normalize_url(url)
        with self._lock:
            for d, rk in self._loc.get(key, {}).items():
                if self._verify_record(self._load_partition(d).get(rk)):
                    return True
        return False

    # ------------------------------------------------------------ in-flight
    def claim(self, url: str) -> bool:
        """병렬 처리 시 같은 게시글을 두 워커가 동시에 받지 않도록 선점."""
        key = normalize_url(url)
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)
            return True

    def release(self, url: str) -> None:
        with self._lock:
            self._inflight.discard(normalize_url(url))

    # ------------------------------------------------------------ writes
    def record_success(
        self,
        url: str,
        name: str,
        output_file: str,
        post_date: str | None = None,
        duration: float | None = None,
        size: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        key = normalize_url(url)
        date_key = post_date.strip() if post_date else "undated"
        with self._lock:
            self._upsert(key, date_key, {
                "name": name,
                "url": url,
                "post_date": post_date,
                "status": "ok",
                "output_file": output_file,
                "duration": duration,
                "size": size,
                "downloaded_at": datetime.now().isoformat(),
                **(extra or {}),
            })

    def record_skipped(
        self,
        url: str,
        name: str,
        reason: str,
        post_date: str | None = None,
        duration: float | None = None,
    ) -> None:
        """길이 조건 등으로 의도적으로 건너뛴 항목 (재방문 시 다시 탐색하지 않음)."""
        key = normalize_url(url)
        date_key = post_date.strip() if post_date else "undated"
        with self._lock:
            for d, rk in self._loc.get(key, {}).items():
                if (self._load_partition(d).get(rk) or {}).get("status") == "ok":
                    return
            self._upsert(key, date_key, {
                "name": name,
                "url": url,
                "post_date": post_date,
                "status": "skipped_short",
                "reason": reason,
                "duration": duration,
                "skipped_at": datetime.now().isoformat(),
            })

    def record_failed(
        self,
        url: str,
        name: str,
        reason: str,
        post_date: str | None = None,
    ) -> None:
        key = normalize_url(url)
        date_key = post_date.strip() if post_date else "undated"
        with self._lock:
            attempts = 1
            for d, rk in self._loc.get(key, {}).items():
                rec = self._load_partition(d).get(rk) or {}
                if rec.get("status") in ("ok", "skipped_short"):
                    return  # 기존 성공/스킵 기록을 실패로 덮어쓰지 않음 (--force 재시도 실패 등)
                attempts = max(attempts, int(rec.get("attempts", 1)) + 1)
            self._upsert(key, date_key, {
                "name": name,
                "url": url,
                "post_date": post_date,
                "status": "failed",
                "reason": reason,
                "attempts": attempts,
                "last_attempt": datetime.now().isoformat(),
            })

    def in_cooldown(self, url: str, hours: float, min_attempts: int = 2) -> bool:
        """같은 글이 min_attempts 회 이상 실패했고 마지막 시도가 hours 시간 이내면 True (재탐색 보류용).
        성공/건너뜀 기록이 하나라도 있으면 False. 이 판정은 글 URL 의 기록만 본다."""
        if hours <= 0:
            return False
        key = normalize_url(url)
        with self._lock:
            for d, rk in self._loc.get(key, {}).items():
                rec = self._load_partition(d).get(rk) or {}
                if rec.get("status") != "failed" or int(rec.get("attempts", 1)) < min_attempts:
                    continue
                try:
                    last = datetime.fromisoformat(rec.get("last_attempt", ""))
                except ValueError:
                    continue
                if (datetime.now() - last).total_seconds() < hours * 3600:
                    return True
        return False

    def get_stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "dates_count": sum(1 for r in self._cache.values() if r),
                "entries_count": sum(len(r) for r in self._cache.values()),
            }
