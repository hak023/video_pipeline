from __future__ import annotations

import shutil
import sys
import threading
import unicodedata

from diagnostics import write_console_line

_board: "ProgressBoard | None" = None


def set_board(b: "ProgressBoard | None") -> None:
    global _board
    _board = b


def log(msg: str = "") -> None:
    """진행 표시줄과 섞이지 않게 로그를 출력 (스레드 안전). 같은 내용이 실행 로그(run.log)에도 남는다."""
    b = _board
    if b is not None:
        b.log(msg)
    else:
        print(msg, flush=True)
    write_console_line(msg)


def _fit(text: str, width: int, pad: bool = False) -> str:
    """한글(전각) 폭을 고려해 표시 너비에 맞게 자른다."""
    out, w = [], 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width:
            break
        out.append(ch)
        w += cw
    s = "".join(out)
    return s + " " * (width - w) if pad else s


class ProgressBoard:
    """여러 다운로드의 상태를 한 줄에 요약 표시.

    [12/40] OK 10 FAIL 1 SKIP 1 | 진행 5 | 제목A 42.3% ; 제목B 탐색 ; ...
    """

    def __init__(self, total: int):
        self.total = total
        self.ok = self.failed = self.skipped = 0
        self._slots: dict[int, tuple[str, str]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._shown = False
        self._tty = sys.stdout.isatty()

    # -- lifecycle
    def start(self) -> None:
        if not self._tty:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            self._clear()
            print(self._summary(), flush=True)

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            self._render()

    # -- state updates (워커 스레드에서 호출)
    def update(self, slot: int, name: str, pct: str, size: int = 0) -> None:
        with self._lock:
            self._slots[slot] = (name, pct)

    def finish(self, slot: int, status: str) -> None:
        with self._lock:
            self._slots.pop(slot, None)
            if status == "ok":
                self.ok += 1
            elif status in ("skipped", "skipped_short"):
                self.skipped += 1
            else:
                self.failed += 1

    def log(self, msg: str) -> None:
        with self._lock:
            self._clear()
            print(msg, flush=True)

    # -- rendering
    def _width(self) -> int:
        return max(shutil.get_terminal_size((100, 20)).columns - 1, 40)

    def _summary(self) -> str:
        done = self.ok + self.failed + self.skipped
        return (f"[{done}/{self.total}] OK {self.ok} FAIL {self.failed} "
                f"SKIP {self.skipped} | 진행 {len(self._slots)}")

    def _clear(self) -> None:
        if self._shown and self._tty:
            sys.stdout.write("\r" + " " * self._width() + "\r")
            self._shown = False

    def _render(self) -> None:
        with self._lock:
            parts = [f"{_fit(n, 12)} {p}" for _, (n, p) in sorted(self._slots.items())]
            line = self._summary() + (" | " + " ; ".join(parts) if parts else "")
            width = self._width()
            sys.stdout.write("\r" + _fit(line, width, pad=True))
            sys.stdout.flush()
            self._shown = True
