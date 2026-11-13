"""정적·동적 광고 블랙리스트 관리.

정적 패턴은 URL 호스트에서만 검사합니다. 예외적으로
'storage.googleapis.com/mediastorage' 같은 패턴은 지정된 경로도 검사합니다.

동적 규칙은 광고 증거를 페이지별로 누적합니다.
- 약한 증거: 서로 다른 페이지 3곳에서 관측되면 활성화
- 강한 증거: 서로 다른 페이지 2곳에서 관측되면 활성화
- 본편으로 확인된 범위는 동적 광고 규칙에서 보호합니다.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import threading
from typing import Any
from urllib.parse import urlparse


_DEFAULT_FILE = pathlib.Path(__file__).with_name("ad_blacklist.json")
_SCHEMA_VERSION = 2
_WEAK_PAGE_THRESHOLD = 3
_STRONG_PAGE_THRESHOLD = 2

_STATIC_AD_PATTERNS = {
    "growcdn", "doubleclick", "cloudpiko", "ethnic", "flixcdn",
    "whitetrafsa", "thekav.co", "magsrv", "exoclick", "jwpltx",
    "diagramjawline", "illinformed", "toptoon", "aliexpress",
    "google-analytics.com", "googletagmanager.com", "analytics.google.com",
    "decafeligiblyhad.com", "saawsedge", "bxcdn.net", "bkcdn.net",
    "morphify.net", "dtscout.com", "crwdcntrl.net",
    "trafficjunky", "syndication", "adsco.re", "popads", "popcash",
    "adsterra", "hilltopads", "propellerads",
    # 이 항목만 호스트와 경로를 함께 검사합니다.
    "storage.googleapis.com/mediastorage",
}

_STATIC_HARD_PATTERNS = {
    "growcdn", "doubleclick", "cloudpiko", "flixcdn",
    "exoclick", "jwpltx", "diagramjawline", "illinformed",
    "toptoon", "aliexpress",
    "google-analytics.com", "googletagmanager.com", "analytics.google.com",
}

_PROTECTED_HOSTS = {
    "youtube.com", "youtu.be", "vimeo.com", "googlevideo.com",
    "sssrr.org", "trycloudflare.com", "cloudflare.com",
    "streamtape.com", "doodstream.com", "mixdrop.co",
}


def _parse_url(value: str):
    """URL 또는 호스트 문자열을 파싱한다."""
    value = (value or "").strip()
    if "://" not in value:
        value = "//" + value
    return urlparse(value)


def _host_path(value: str) -> tuple[str, str]:
    parsed = _parse_url(value)
    return (parsed.hostname or "").lower(), (parsed.path or "").lower()


def _is_protected_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == protected or host.endswith("." + protected)
               for protected in _PROTECTED_HOSTS)


def _scope_for_url(url: str) -> tuple[str, str]:
    """동적 규칙 범위 키와 범위 종류를 반환한다.

    파일 하나만 있는 루트 경로는 호스트 범위로 취급합니다.
    디렉터리가 있는 URL은 첫 경로 디렉터리까지만 범위로 잡습니다.
    """
    host, path = _host_path(url)
    if not host:
        return "", ""

    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 or (path.endswith("/") and parts):
        return f"{host}/{parts[0]}", "path"
    return host, "host"


def _scope_covers(scope_key: str, url: str) -> bool:
    """scope_key가 URL을 포함하는지 검사한다."""
    host, path = _host_path(url)
    if not host:
        return False

    if "/" not in scope_key:
        return host == scope_key

    scope_host, scope_dir = scope_key.split("/", 1)
    if host != scope_host:
        return False

    parts = [part for part in path.split("/") if part]
    return bool(parts) and parts[0] == scope_dir


def _static_match(pattern: str, host: str, path: str) -> bool:
    """정적 패턴은 호스트에서 검사하되, host/path 패턴은 경로도 검사한다."""
    pattern = pattern.lower()
    if "/" in pattern:
        pattern_host, pattern_path = pattern.split("/", 1)
        return pattern_host in host and pattern_path in path
    return pattern in host


class AdBlacklistStack:
    def __init__(self, storage_path: pathlib.Path | None = None):
        self._lock = threading.RLock()
        self._path = pathlib.Path(storage_path) if storage_path else _DEFAULT_FILE
        self._static = set(_STATIC_AD_PATTERNS)
        self._dynamic: dict[str, dict[str, Any]] = {}
        self._confirmed_main: dict[str, dict[str, Any]] = {}
        self._load()

    def _backup_legacy(self) -> None:
        """지원하지 않는 구형 저장 파일을 .legacy.bak으로 옮긴다."""
        backup = self._path.with_suffix(".legacy.bak")
        try:
            if backup.exists():
                backup.unlink()
            os.replace(self._path, backup)
        except OSError:
            # 백업 실패 시 원본은 그대로 두고 빈 상태로 시작한다.
            pass

    def _load(self) -> None:
        if not self._path.exists():
            return

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            self._backup_legacy()
            return

        if (
            isinstance(data, dict)
            and data.get("version") == _SCHEMA_VERSION
            and isinstance(data.get("rules"), dict)
            and isinstance(data.get("confirmed_main"), dict)
        ):
            self._dynamic = {
                str(k): v for k, v in data["rules"].items()
                if isinstance(v, dict)
            }
            self._confirmed_main = {
                str(k): v for k, v in data["confirmed_main"].items()
                if isinstance(v, dict)
            }
            return

        # 예전 dict/list 형식은 현행 범위·페이지 정보가 없어 안전하게 재사용할 수 없다.
        self._backup_legacy()

    def _save(self) -> None:
        """상태를 원자적으로 저장한다. 저장 실패가 다운로드 작업을 중단시키지는 않는다."""
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": _SCHEMA_VERSION,
            "rules": self._dynamic,
            "confirmed_main": self._confirmed_main,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _confirmed_scope_locked(self, url: str) -> str | None:
        for scope_key in self._confirmed_main:
            if _scope_covers(scope_key, url):
                return "path" if "/" in scope_key else "host"
        return None

    def main_confirmed_scope(self, url: str) -> str | None:
        """본편으로 확인되어 보호 중인 URL의 범위('host'/'path')를 반환한다."""
        with self._lock:
            return self._confirmed_scope_locked(url)

    def match(self, url: str) -> dict[str, Any] | None:
        """URL이 광고 규칙에 일치하면 규칙 정보를 반환한다."""
        if not url:
            return None

        host, path = _host_path(url)
        if not host:
            return None

        # 정적 규칙은 호스트만 검사한다. 경로 기반 예외는 패턴에 '/'가 있을 때만 검사한다.
        for pattern in sorted(self._static):
            if _static_match(pattern, host, path):
                source = (
                    "static_hard"
                    if pattern in _STATIC_HARD_PATTERNS
                    else "static_soft"
                )
                return {"source": source, "pattern": pattern}

        with self._lock:
            # 본편 확인 범위에 해당하면 동적 광고 규칙은 적용하지 않는다.
            if self._confirmed_scope_locked(url):
                return None

            for scope_key, entry in self._dynamic.items():
                if not _scope_covers(scope_key, url):
                    continue

                pages = entry.get("pages") or []
                strong = bool(entry.get("strong"))
                threshold = _STRONG_PAGE_THRESHOLD if strong else _WEAK_PAGE_THRESHOLD
                if entry.get("active") or len(set(pages)) >= threshold:
                    return {
                        "source": "dynamic",
                        "pattern": scope_key,
                        "reason": entry.get("reason", ""),
                        "count": entry.get("count", len(pages)),
                        "strong": strong,
                    }

        return None

    def is_ad(self, url: str) -> bool:
        """URL이 정적/활성 동적 블랙리스트에 해당하는지 반환한다."""
        return self.match(url) is not None

    def observe_ad(
        self,
        url: str,
        reason: str,
        page_url: str = "",
        strong: bool = False,
    ) -> bool:
        """광고 증거를 누적한다. 기준 페이지 수에 도달하기 전까지 규칙은 활성화하지 않는다."""
        if not url:
            return False

        scope_key, _scope_type = _scope_for_url(url)
        if not scope_key:
            return False

        host = scope_key.split("/", 1)[0]
        if _is_protected_host(host):
            return False

        # 정적 광고 매칭은 이미 관리되므로 동적 규칙으로 중복 등록하지 않는다.
        if self.match(url) and self.match(url).get("source", "").startswith("static_"):
            return False

        page_key = page_url.strip() or "<unknown-page>"

        with self._lock:
            if self._confirmed_scope_locked(url):
                return False

            entry = self._dynamic.setdefault(scope_key, {
                "reason": reason,
                "strong": bool(strong),
                "count": 0,
                "pages": [],
                "active": False,
            })

            entry["count"] = int(entry.get("count", 0)) + 1
            entry["strong"] = bool(entry.get("strong")) or bool(strong)
            if reason and not entry.get("reason"):
                entry["reason"] = reason

            pages = set(entry.get("pages") or [])
            pages.add(page_key)
            entry["pages"] = sorted(pages)

            threshold = (
                _STRONG_PAGE_THRESHOLD
                if entry["strong"]
                else _WEAK_PAGE_THRESHOLD
            )
            entry["active"] = len(pages) >= threshold
            self._save()

        return True

    def record_main(self, url: str, page_url: str = "") -> bool:
        """본편으로 확인된 URL의 범위를 보호하고 해당 범위의 동적 광고 규칙을 제거한다."""
        if not url:
            return False

        scope_key, scope_type = _scope_for_url(url)
        if not scope_key:
            return False

        host = scope_key.split("/", 1)[0]
        if _is_protected_host(host):
            return False

        with self._lock:
            self._confirmed_main[scope_key] = {
                "scope": scope_type,
                "page_url": page_url,
            }

            # 확인된 범위에 포함되는 동적 규칙은 제거한다.
            to_remove = [
                rule for rule in self._dynamic
                if _scope_covers(scope_key, f"https://{rule}/")
                or _scope_covers(rule, url)
            ]
            for rule in to_remove:
                self._dynamic.pop(rule, None)

            self._save()
        return True

    def push(self, url_or_pattern: str, reason: str = "") -> bool:
        """하위 호환용 즉시 등록 API. 등록한 규칙은 강한 동적 규칙으로 활성화한다."""
        if not url_or_pattern:
            return False

        target = url_or_pattern.strip()
        parsed = _parse_url(target)
        host = (parsed.hostname or "").lower()

        if host:
            if _is_protected_host(host):
                return False
            if host in {"storage.googleapis.com", "s3.amazonaws.com"}:
                path_parts = [part for part in parsed.path.split("/") if part]
                scope_key = f"{host}/{path_parts[0]}" if path_parts else host
            else:
                scope_key, _ = _scope_for_url(target)
        else:
            scope_key = target.lower().strip("/")

        if not scope_key or len(scope_key) < 3:
            return False

        with self._lock:
            self._dynamic[scope_key] = {
                "reason": reason,
                "strong": True,
                "count": 1,
                "pages": [],
                "active": True,
            }
            self._save()
        return True

    def get_all_patterns(self) -> list[str]:
        """정적·동적 규칙 키를 정렬해 반환한다."""
        with self._lock:
            return sorted(self._static | set(self._dynamic))

    def snapshot(self) -> dict[str, Any]:
        """진단용으로 동적 규칙과 본편 보호 상태의 복사본을 반환한다."""
        with self._lock:
            return {
                "version": _SCHEMA_VERSION,
                "rules": copy.deepcopy(self._dynamic),
                "confirmed_main": copy.deepcopy(self._confirmed_main),
            }


# 전역 싱글톤 인스턴스
bl = ad_blacklist.AdBlacklistStack(cls.tmp / "bl.json")


def is_ad_url(url: str) -> bool:
    return blacklist.is_ad(url)


def add_ad_pattern(url_or_pattern: str, reason: str = "") -> bool:
    return blacklist.push(url_or_pattern, reason)