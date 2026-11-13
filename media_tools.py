from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any


def resolve_chrome_channel() -> str | None:
    """Return 'chrome' if system Google Chrome is installed, else None."""
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / r"Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if c.is_file():
            return "chrome"
    return None


def _sandbox_args() -> list[str]:
    """Chrome 샌드박스는 광고(악성 스크립트) 페이지로부터 PC 를 보호하는 마지막 방어선이라 기본으로 켠다.
    Windows 일반 사용자 실행에서는 필요 없고, Linux root/컨테이너처럼 켤 수 없을 때만 해제한다.
    (환경변수 VP_NO_SANDBOX=1 로 강제 해제 가능)"""
    if os.environ.get("VP_NO_SANDBOX") == "1":
        return ["--no-sandbox"]
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        return ["--no-sandbox"]
    return []


def get_browser_launch_kwargs(
    headless: bool = True,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Build launch kwargs for Playwright, preferring system Chrome for SecretDNS/VPN compatibility."""
    kwargs: dict[str, Any] = {
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            *_sandbox_args(),
        ],
    }
    channel = resolve_chrome_channel()
    if channel:
        kwargs["channel"] = channel

    if proxy:
        kwargs["proxy"] = {"server": proxy}

    return kwargs


def get_context_kwargs(user_agent: str) -> dict[str, Any]:
    """브라우저 컨텍스트 공통 옵션. 광고가 유발하는 자동 파일 다운로드(.exe/.apk 등)는 받지 않는다."""
    return {"user_agent": user_agent, "locale": "ko-KR", "accept_downloads": False}


def resolve_ffmpeg() -> Path | None:
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    for d in sorted(base.glob("ffmpeg-*"), reverse=True):
        bundled = d / "ffmpeg-win64.exe"
        if bundled.is_file():
            return bundled
    return None


def resolve_ffprobe() -> Path | None:
    found = shutil.which("ffprobe")
    if found:
        return Path(found)
    return None


def probe_tools_available() -> bool:
    return resolve_ffprobe() is not None or resolve_ffmpeg() is not None


def probe_duration_size(path: Path) -> tuple[float, int] | None:
    """Return (duration_seconds, size_bytes) using ffprobe or ffmpeg. 실패/손상/도구 오류는 None."""
    import json
    import subprocess

    size = path.stat().st_size
    ffprobe = resolve_ffprobe()
    if ffprobe:
        try:
            r = subprocess.run(
                [str(ffprobe), "-v", "error", "-show_entries", "format=duration,size", "-of", "json", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                stdin=subprocess.DEVNULL,
            )
            if r.returncode == 0:
                fmt = json.loads(r.stdout).get("format") or {}
                dur = fmt.get("duration")
                if dur not in (None, "N/A", ""):
                    return float(dur), int(fmt.get("size") or size)
        except (subprocess.TimeoutExpired, ValueError, OSError, KeyError):
            pass

    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        return None

    try:
        r = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
    if not m:
        return None
    h, mi, s = m.groups()
    duration = int(h) * 3600 + int(mi) * 60 + float(s)
    return duration, size
