from __future__ import annotations

from pathlib import Path
from typing import Any

from media_tools import probe_duration_size, probe_tools_available


def verify(path: str | Path, min_seconds: float = 5) -> tuple[bool, dict[str, Any] | str]:
    """다운로드 결과 검증.

    - 파일 없음/0바이트/메타 추출 실패/너무 짧음 → (False, 사유 문자열 또는 meta)  → 호출측이 DOWNLOAD_INVALID 로 처리
    - ffprobe/ffmpeg 가 아예 없어 검증 자체가 불가능하면 정상 파일을 지우지 않도록 (True, {"unverified": ...}) 를 돌려준다.
    """
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return False, "파일 없음 또는 0바이트"

    if not probe_tools_available():
        return True, {"duration": None, "size": str(path.stat().st_size), "unverified": "ffprobe/ffmpeg 없음"}

    probed = probe_duration_size(path)
    if probed is None:
        return False, "메타 추출 실패 (손상된 파일일 수 있음)"

    duration, size = probed
    fmt = {"duration": str(duration), "size": str(size)}
    ok = duration >= min_seconds and size > 0
    return ok, fmt
