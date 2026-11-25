import sys
import threading
import time


class ProgressTracker:
    """Thread-safe live single-line terminal status bar (zero external dependencies)."""

    def __init__(self, label: str, total: int, total_chunks: int = 1):
        self.label = label
        self.total = max(0, total)
        self.total_chunks = max(1, total_chunks)
        self.completed = 0
        self.errors = 0
        self.current_chunk = 1
        self.start_time = time.time()
        self._lock = threading.Lock()
        self._last_render = 0.0

    def set_chunk(self, chunk_idx: int) -> None:
        with self._lock:
            self.current_chunk = chunk_idx

    def advance(
        self,
        count: int = 1,
        error: bool = False,
        req_count: int = 0,
        rate_remaining: str = "?",
        force: bool = False,
    ) -> None:
        with self._lock:
            self.completed += count
            if error:
                self.errors += count
            now = time.time()
            if not force and (now - self._last_render) < 0.25 and self.completed < self.total:
                return
            self._last_render = now
            self._render(req_count, rate_remaining)

    def _render(self, req_count: int, rate_remaining: str) -> None:
        elapsed = max(0.001, time.time() - self.start_time)
        pct = (self.completed / self.total * 100.0) if self.total else 100.0
        rate = self.completed / elapsed
        eta = int((self.total - self.completed) / rate) if rate > 0 and self.total > self.completed else 0
        bar_len = 20
        filled = int(bar_len * (self.completed / self.total)) if self.total else bar_len
        bar = "#" * filled + "-" * (bar_len - filled)
        line = (
            f"\r[{self.label}] [{bar}] {self.completed}/{self.total} ({pct:5.1f}%) "
            f"| chunk {self.current_chunk}/{self.total_chunks} "
            f"| err:{self.errors} | reqs:{req_count} | rl:{rate_remaining} | ETA:{eta}s   "
        )
        sys.stderr.write(line)
        sys.stderr.flush()

    def finish(self, req_count: int = 0, rate_remaining: str = "?") -> None:
        with self._lock:
            self._render(req_count, rate_remaining)
            sys.stderr.write("\n")
            sys.stderr.flush()