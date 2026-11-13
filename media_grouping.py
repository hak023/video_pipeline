"""복수 본편·화질별 스트림 그룹화와 다운로드 시도 순서 결정 (네트워크 비의존 순수 함수).

같은 영상으로 묶는 규칙 (하나라도 만족하면 같은 그룹)
  HLS_CHILD     : master m3u8 과 같은 디렉터리 아래의 하위 playlist
  SAME_BASENAME : 같은 디렉터리 + 화질 토큰(1080p, _hd 등)을 제거한 파일명이 같음
  SAME_PLAYLIST : 같은 플레이어의 같은 playlist 항목(playlist_index)에서 나온 소스들
  SAME_DURATION : 같은 호스트이고 길이가 둘 다 알려져 있으며 1.5초 이내로 같음
서로 다른 본편은 서로 다른 그룹이 된다. 광고 후보는 호출 전에 제외하고, 제외 기록은 호출측이 로그로 남긴다.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

DURATION_TOLERANCE = 1.5

_RES_RE = re.compile(r"(?<![0-9])(2160|1440|1080|720|576|480|360|240|144)(?:p|P)?(?![0-9])")
_QUALITY_WORD_RE = re.compile(r"(?i)(?<![a-z])(hd|sd|fhd|uhd|low|medium|high|hq|lq)(?![a-z])")


def _path(u: str) -> str:
    try:
        return urlparse(u).path
    except ValueError:
        return ""


def _host(u: str) -> str:
    try:
        return (urlparse(u).hostname or "").lower()
    except ValueError:
        return ""


def _basename(u: str) -> str:
    return _path(u).rsplit("/", 1)[-1].lower()


def is_hls(c: dict[str, Any]) -> bool:
    return _path(c["url"]).lower().endswith(".m3u8") or "mpegurl" in (c.get("content_type") or "")


def is_dash(c: dict[str, Any]) -> bool:
    return _path(c["url"]).lower().endswith(".mpd")


def resolution_hint(url: str) -> int:
    m = _RES_RE.findall(_path(url))
    return max((int(x) for x in m), default=0)


def variant_key(url: str) -> tuple[str, str]:
    """(디렉터리, 화질 토큰을 제거한 파일명). 같은 영상의 화질 변형은 같은 키가 된다."""
    path = _path(url).lower()
    d, _, name = path.rpartition("/")
    stem = name.rsplit(".", 1)[0]
    stem = _RES_RE.sub("", stem)
    stem = _QUALITY_WORD_RE.sub("", stem)
    stem = re.sub(r"[-_.\s]+", "-", stem).strip("-")
    return _host(url) + d, stem


def _dir_prefix(url: str) -> str:
    return url.split("?")[0].rsplit("/", 1)[0] + "/"


def _is_master(c: dict[str, Any]) -> bool:
    if not is_hls(c):
        return False
    if c.get("m3u8_kind") == "master":
        return True
    return c.get("m3u8_kind") in (None, "unknown") and _path(c["url"]).rsplit("/", 1)[-1].lower().startswith("master")


def rank_key(c: dict[str, Any]) -> tuple:
    """다운로드 시도 순서: master m3u8 > media m3u8 > 기타 m3u8 > mpd > mp4(화질/용량 큰 순)."""
    if is_hls(c):
        kind = 0 if _is_master(c) else (1 if c.get("m3u8_kind") == "media" else 2)
        return (kind, 0, 0)
    if is_dash(c):
        return (3, 0, 0)
    return (4, -resolution_hint(c["url"]), -(c.get("content_length") or 0))


def order_attempts(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """그룹 구성원 중 실제로 다운로드를 시도할 후보를 우선순위대로 반환.
    master 가 있으면 그 아래 하위 playlist 는 master 로 함께 받으므로 제외한다
    (제외하지 않으면 master 실패 시 '영상만' 또는 '소리만' 받게 됨)."""
    masters = [c for c in members if _is_master(c)]
    prefixes = [_dir_prefix(c["url"]) for c in masters]
    out = [c for c in members
           if c in masters or not (is_hls(c) and any(c["url"].startswith(p) for p in prefixes))]
    return sorted(out, key=rank_key)


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.p[max(ra, rb)] = min(ra, rb)
        return True


def _dur(c: dict[str, Any]) -> float | None:
    for k in ("duration", "dom_duration"):
        v = c.get(k)
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def group_candidates(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """비(非)광고 후보를 영상 단위 그룹으로 묶는다.

    반환: [{"group_id", "members", "attempts", "candidate_ids", "classification", "reasons",
            "main_score", "ad_score", "representative"}] — 재생/playlist 순서대로.
    """
    n = len(cands)
    if n == 0:
        return []
    uf = _UF(n)
    reasons: dict[int, set[str]] = {i: set() for i in range(n)}

    def link(i: int, j: int, why: str) -> None:
        # playlist 항목 번호가 서로 다른 후보는 서로 다른 영상이므로 어떤 규칙으로도 합치지 않는다
        pi, pj = cands[i].get("playlist_index"), cands[j].get("playlist_index")
        if pi is not None and pj is not None and pi != pj:
            return
        uf.union(i, j)
        reasons[i].add(why)
        reasons[j].add(why)

    masters = [(i, _dir_prefix(c["url"])) for i, c in enumerate(cands) if _is_master(c)]
    keys = [variant_key(c["url"]) for c in cands]
    for i in range(n):
        for j in range(i + 1, n):
            ci, cj = cands[i], cands[j]
            if keys[i] == keys[j] and keys[i][1]:
                link(i, j, "SAME_BASENAME")
            # 리다이렉트/미러로 호스트가 달라져도 파일명과 전체 용량이 같으면 같은 파일이다
            li, lj = ci.get("content_length"), cj.get("content_length")
            if (isinstance(li, int) and li > 0 and li == lj and _basename(ci["url"]) == _basename(cj["url"])
                    and _basename(ci["url"])):
                link(i, j, "SAME_FILE")
            pi, pj = ci.get("playlist_index"), cj.get("playlist_index")
            if (pi is not None and pi == pj and ci.get("player_id") == cj.get("player_id")
                    and ci.get("embed_url") == cj.get("embed_url")):
                link(i, j, "SAME_PLAYLIST")
            di, dj = _dur(ci), _dur(cj)
            if (di and dj and abs(di - dj) <= DURATION_TOLERANCE and _host(ci["url"]) == _host(cj["url"])
                    and pi is None and pj is None):
                link(i, j, "SAME_DURATION")
    for mi, prefix in masters:
        for j, c in enumerate(cands):
            # 하위 playlist 만 master 의 구성원이다 (다른 master 는 다른 영상일 수 있으므로 합치지 않는다)
            if j != mi and is_hls(c) and not _is_master(c) and c["url"].startswith(prefix):
                link(mi, j, "HLS_CHILD")

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(uf.find(i), []).append(i)

    def order_pos(idx: list[int]) -> tuple:
        pls = [cands[i].get("playlist_index") for i in idx if cands[i].get("playlist_index") is not None]
        plays = [cands[i].get("play_order") for i in idx if cands[i].get("play_order") is not None]
        return (min(pls) if pls else 10**6, min(plays) if plays else 10**6, min(idx))

    groups: list[dict[str, Any]] = []
    for gi, idx in enumerate(sorted(buckets.values(), key=order_pos), 1):
        members = [cands[i] for i in idx]
        attempts = order_attempts(members)
        why = sorted(set().union(*(reasons[i] for i in idx)))
        labels = [m.get("classification") for m in members]
        groups.append({
            "group_id": f"group_{gi:02d}",
            "members": members,
            "attempts": attempts,
            "candidate_ids": [m.get("candidate_id") for m in members],
            "classification": "MAIN" if "MAIN" in labels else "PENDING",
            "main_score": max((m.get("main_score") or 0) for m in members),
            "ad_score": min((m.get("ad_score") or 0) for m in members),
            "reasons": why,
            "representative": attempts[0] if attempts else members[0],
        })
    return groups
