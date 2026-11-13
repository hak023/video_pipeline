"""미디어 후보 판별 엔진: MAIN / AD / PENDING.

네트워크·브라우저에 의존하지 않는 순수 함수다. 후보 dict 의 필드와 블랙리스트 '근거'를 받아
점수와 근거 목록(rule ID 포함)을 돌려준다.

판정
  AD      : ad_score >= AD_THRESHOLD   이고 ad_score  > main_score
  MAIN    : main_score >= MAIN_THRESHOLD 이고 main_score > ad_score
  PENDING : 그 외 (근거 부족/충돌). 오탐 방지를 위해 광고로 확정할 근거가 부족하면 버리지 않는다.

도메인·길이·용량은 단독으로 AD 를 확정하지 못하도록 점수를 배분했다.
  - 단독 AD 확정이 가능한 근거: 정적 HARD 광고 네트워크(10, 뒤집히지 않음), 정적 SOFT/동적 규칙(3), 광고 전용 경로 토큰(3)
  - 길이/용량(AD_SMALL_FILE, AD_SHORT_*)은 보조 근거이며, 이것만으로는 점수와 무관하게 AD 가 될 수 없다(PENDING).
  - 본편 근거(플레이어 playlist 2 + 마스터 1 + 길이 3 ...)가 충분하면 SOFT/동적 근거를 뒤집을 수 있다.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

AD_THRESHOLD = 3
MAIN_THRESHOLD = 3

LONG_SECONDS = 90          # 이 길이 이상이면 본편 근거
MEDIUM_SECONDS = 60
SHORT_AD_SECONDS = 30      # 이 길이 이하면 광고 보조 근거
SHORT_SECONDS = 60
SMALL_FILE_BYTES = 1 * 1024 * 1024
AUX_AD_RULES = frozenset({"AD_SMALL_FILE", "AD_SHORT_30", "AD_SHORT_60", "AD_DYNAMIC_WEAK"})

# 광고 전용 경로를 나타내는 토큰 (경로 세그먼트를 '/', '.', '_', '-' 로 쪼갠 단어와 정확히 일치할 때만)
AD_PATH_TOKENS = frozenset({
    "ad", "ads", "adserver", "adserving", "advert", "adverts", "advertisement", "advertising",
    "vast", "vpaid", "preroll", "midroll", "postroll", "sponsor", "sponsored",
})
_SPLIT_RE = re.compile(r"[/._\-]+")


def _evidence(rule: str, points: int, detail: str = "") -> dict[str, Any]:
    return {"rule": rule, "points": points, "detail": detail}


def ad_path_token(url: str) -> str | None:
    try:
        path = urlparse(url).path.lower()
    except ValueError:
        return None
    for tok in _SPLIT_RE.split(path):
        if tok in AD_PATH_TOKENS:
            return tok
    return None


def _duration(c: dict[str, Any]) -> float | None:
    for k in ("duration", "dom_duration"):
        v = c.get(k)
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def classify_candidate(
    cand: dict[str, Any],
    *,
    blacklist: Any = None,
    player_hosts: set[str] | None = None,
) -> dict[str, Any]:
    """후보 1개를 판별한다. cand 는 변경하지 않는다.

    cand 가 사용하는 필드: url, content_type, content_length, duration, dom_duration, m3u8_kind,
    from_playlist, frame_url.   blacklist: match(url)/main_confirmed_scope(url) 를 제공하는 객체.
    """
    url = cand.get("url", "") or ""
    ad: list[dict[str, Any]] = []
    main: list[dict[str, Any]] = []
    bl_match: dict[str, str] | None = None

    if blacklist is not None:
        bl_match = blacklist.match(url)
        if bl_match:
            src = bl_match["source"]
            if src == "dynamic" and not bl_match.get("strong"):
                # 약한 증거(짧은 길이 등)만으로 활성화된 동적 규칙은 보조 근거일 뿐 AD 를 확정하지 못한다
                ad.append(_evidence("AD_DYNAMIC_WEAK", 2, bl_match.get("pattern", "")))
            else:
                pts = {"static_hard": 10, "static_soft": 3, "dynamic": 3}.get(src, 3)   # HARD 는 본편 근거 합산으로도 뒤집히지 않음
                ad.append(_evidence(f"AD_{src.upper()}", pts, bl_match.get("pattern", "")))
        scope = blacklist.main_confirmed_scope(url)
        if scope:
            main.append(_evidence("MAIN_HOST_CONFIRMED", 2 if scope == "path" else 1, scope))

    tok = ad_path_token(url)
    if tok:
        ad.append(_evidence("AD_PATH_TOKEN", 3, tok))

    length = cand.get("content_length")
    is_mp4 = urlparse(url).path.lower().endswith(".mp4") or "video/mp4" in (cand.get("content_type") or "")
    if is_mp4 and isinstance(length, int) and 0 < length < SMALL_FILE_BYTES:
        ad.append(_evidence("AD_SMALL_FILE", 2, f"{length}B"))

    dur = _duration(cand)
    if dur is not None:
        if dur <= SHORT_AD_SECONDS:
            ad.append(_evidence("AD_SHORT_30", 2, f"{dur:.0f}s"))
        elif dur <= SHORT_SECONDS:
            ad.append(_evidence("AD_SHORT_60", 1, f"{dur:.0f}s"))
        elif dur >= LONG_SECONDS:
            main.append(_evidence("MAIN_LONG", 3, f"{dur:.0f}s"))
        else:
            main.append(_evidence("MAIN_MEDIUM", 1, f"{dur:.0f}s"))

    if cand.get("from_playlist"):
        main.append(_evidence("MAIN_PLAYLIST", 2, f"index={cand.get('playlist_index')}"))
    if cand.get("m3u8_kind") == "master":
        main.append(_evidence("MAIN_MASTER", 1))
    if player_hosts:
        fh = _frame_host(cand.get("frame_url") or cand.get("embed_url") or "")
        if fh and fh in player_hosts:
            main.append(_evidence("MAIN_PLAYER_FRAME", 1, fh))

    ad_score = sum(e["points"] for e in ad)
    main_score = sum(e["points"] for e in main)
    # 길이/용량 같은 보조 근거만으로는 AD 를 확정하지 않는다 (도메인·경로·규칙 근거가 최소 1개 필요)
    has_primary_ad = any(e["rule"] not in AUX_AD_RULES for e in ad)
    if has_primary_ad and ad_score >= AD_THRESHOLD and ad_score > main_score:
        label = "AD"
    elif main_score >= MAIN_THRESHOLD and main_score > ad_score:
        label = "MAIN"
    else:
        label = "PENDING"
    return {
        "classification": label, "main_score": main_score, "ad_score": ad_score,
        "main_evidence": main, "ad_evidence": ad,
        "rules": [e["rule"] for e in main + ad], "blacklist_match": bl_match,
    }


def _frame_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def strong_ad_rules(result: dict[str, Any]) -> bool:
    """동적 블랙리스트에 '강한 증거'로 반영할 수 있는 광고 판정인지 (정적/동적 규칙 이외의 독립 근거가 있는 AD)."""
    if result.get("classification") != "AD":
        return False
    return any(e["rule"] == "AD_PATH_TOKEN" for e in result.get("ad_evidence", []))
