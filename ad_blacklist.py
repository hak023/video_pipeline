"""동적 광고/유해 URL 블랙리스트 스택 모듈.

런타임에 발견되는 광고 도메인, DEAD URL, 팝업 위젯 등을 동적으로 스택에 축적하고
JSON 파일에 영구 저장하여 이후 탐색 및 다운로드 시 즉시 배제한다.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import threading
from typing import Any
from urllib.parse import urlparse

# 파일 저장 경로 (video-pipeline/ad_blacklist.json)
_DEFAULT_FILE = pathlib.Path(__file__).with_name("ad_blacklist.json")

# 정적 기본 블랙리스트 (도메인 또는 키워드)
_STATIC_AD_PATTERNS = [
    # 기존 블랙리스트
    "growcdn", "doubleclick", "cloudpiko", "ethnic", "flixcdn",
    "whitetrafsa", "thekav.co", "magsrv", "exoclick", "jwpltx",
    "diagramjawline", "illinformed", "toptoon", "aliexpress",
    "google-analytics.com", "googletagmanager.com", "analytics.google.com",
    # 팝업/단축/위젯/30초 광고 도메인
    "decafeligiblyhad.com", "saawsedge", "bxcdn.net", "bkcdn.net",
    "morphify.net", "dtscout.com", "crwdcntrl.net",
    "trafficjunky", "syndication", "adsco.re", "popads", "popcash",
    "adsterra", "hilltopads", "propellerads",
    # 결제 중지/폐기된 GCS 더미 스트림 버킷
    "storage.googleapis.com/mediastorage",
]

# 절대 블랙리스트에 넣지 말아야 할 안전 도메인 보호 목록
_PROTECTED_HOSTS = {
    "youtube.com", "youtu.be", "vimeo.com", "googlevideo.com",
    "sssrr.org", "trycloudflare.com", "cloudflare.com",
    "streamtape.com", "doodstream.com", "mixdrop.co",
}


class AdBlacklistStack:
    def __init__(self, storage_path: pathlib.Path | None = None):
        self._lock = threading.Lock()
        self._path = storage_path or _DEFAULT_FILE
        self._static: set[str] = set(_STATIC_AD_PATTERNS)
        self._dynamic: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self._dynamic = data
                elif isinstance(data, list):
                    # 구버전 리스트 형식 호환
                    self._dynamic = {p: {"reason": "legacy", "count": 1} for p in data if isinstance(p, str)}
        except Exception:
            pass

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._dynamic, f, indent=2, ensure_ascii=False)
            if os.path.exists(tmp):
                os.replace(tmp, self._path)
        except Exception:
            pass

    def is_ad(self, url: str) -> bool:
        """URL이 정적/동적 블랙리스트에 해당하는지 판별."""
        if not url:
            return False
        u_lower = url.lower()

        # 정적 패턴 검사
        for pat in self._static:
            if pat in u_lower:
                return True

        with self._lock:
            # 동적 패턴 검사
            for pat in self._dynamic.keys():
                if pat in u_lower:
                    return True

        return False

    def match(self, url: str) -> dict[str, Any] | None:
        """URL이 블랙리스트에 일치하면 상세 결과(dict)를, 없으면 None 반환.

        반환 dict 키:
          - source: "static_hard" | "static_soft" | "dynamic"
          - pattern: 일치한 패턴 문자열
        """
        if not url:
            return None
        u_lower = url.lower()

        # 정적 패턴 검사 (키워드 기반)
        for pat in self._static:
            if pat in u_lower:
                # 하드 광고 도메인 키워드
                if pat in (
                    "growcdn", "doubleclick", "cloudpiko", "flixcdn",
                    "exoclick", "jwpltx", "diagramjawline", "illinformed",
                    "toptoon", "aliexpress",
                    "google-analytics.com", "googletagmanager.com", "analytics.google.com",
                ):
                    return {"source": "static_hard", "pattern": pat}
                return {"source": "static_soft", "pattern": pat}

        # 동적 패턴 검사
        with self._lock:
            for pat in self._dynamic.keys():
                if pat in u_lower:
                    entry = self._dynamic.get(pat, {})
                    return {
                        "source": "dynamic",
                        "pattern": pat,
                        "reason": entry.get("reason", ""),
                        "count": entry.get("count", 1),
                    }

        return None

    def push(self, url_or_pattern: str, reason: str = "") -> bool:
        """새로운 광고/DEAD 패턴 또는 도메인을 동적 블랙리스트 스택에 추가."""
        if not url_or_pattern:
            return False

        # 도메인 또는 주요 식별자 추출
        target = url_or_pattern.strip()
        parsed = urlparse(target)
        host = (parsed.hostname or "").lower()

        pattern_to_add = ""
        if host:
            # 보호 도메인은 추가 금지
            if any(host.endswith(p) for p in _PROTECTED_HOSTS):
                return False

            # GCS 등 거대 클라우드 호스트는 전체 호스트가 아닌 특정 경로/버킷 단위로 등록
            if host in ("storage.googleapis.com", "s3.amazonaws.com"):
                path_parts = [p for p in parsed.path.split("/") if p]
                if path_parts:
                    pattern_to_add = f"{host}/{path_parts[0]}"
                else:
                    pattern_to_add = target.split("?")[0].split("#")[0]
            else:
                pattern_to_add = host
        else:
            pattern_to_add = target.lower()

        if not pattern_to_add or len(pattern_to_add) < 3:
            return False

        with self._lock:
            if pattern_to_add in self._static:
                return True
            entry = self._dynamic.get(pattern_to_add)
            if entry:
                entry["count"] = entry.get("count", 1) + 1
                if reason and not entry.get("reason"):
                    entry["reason"] = reason
            else:
                self._dynamic[pattern_to_add] = {
                    "reason": reason,
                    "count": 1,
                    "added_from": target[:150],
                }
            self._save()
        return True

    def get_all_patterns(self) -> list[str]:
        """현재 등록된 모든 블랙리스트 패턴 목록 반환."""
        with self._lock:
            return sorted(list(self._static | set(self._dynamic.keys())))

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """현재 동적 블랙리스트 상태의 스냅샷(복사본)을 반환합니다."""
        with self._lock:
            return dict(self._dynamic)


# 전역 싱글톤 인스턴스
blacklist = AdBlacklistStack()


def is_ad_url(url: str) -> bool:
    return blacklist.is_ad(url)


def add_ad_pattern(url_or_pattern: str, reason: str = "") -> bool:
    return blacklist.push(url_or_pattern, reason)
