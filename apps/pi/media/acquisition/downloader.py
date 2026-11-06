"""Streamed image payload download with size guard and hard deadline."""

from __future__ import annotations

from ..models import ImageCandidate


class ImageDownloadError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


class ImageDownloader:
    def __init__(self, *, max_bytes: int = 15 * 1024 * 1024, timeout_seconds: float = 20.0, fetcher=None):
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.fetcher = fetcher or self._http_fetch

    def _http_fetch(self, url: str) -> tuple[int, bytes]:
        import httpx

        # httpx timeout covers each socket op, not the whole stream; a server
        # that dribbles bytes slowly can hold the download open past the budget.
        with httpx.stream("GET", url, follow_redirects=True, timeout=self.timeout_seconds) as response:
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > self.max_bytes:
                    raise ImageDownloadError("IMAGE_TOO_LARGE")
                chunks.append(chunk)
            return response.status_code, b"".join(chunks)

    def download(self, candidate: ImageCandidate) -> tuple[int, bytes]:
        # A fetch can stall on connect/redirect/read even when each httpx op is
        # under its own timeout. Bound the WHOLE fetch with a hard wall-clock
        # deadline on a daemon worker; on timeout the daemon thread is dropped
        # (it dies with the process, holding no more than one socket) and the
        # caller gets IMAGE_TIMEOUT. ponytail: timed-out threads leak one socket
        # each; replace with cooperative cancellation once httpx supports it.
        import threading

        out: dict[str, object] = {}

        def _run() -> None:
            try:
                out["result"] = self.fetcher(candidate.image_url)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                out["error"] = exc

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(self.timeout_seconds)
        if worker.is_alive():
            raise ImageDownloadError("IMAGE_TIMEOUT", f"fetch exceeded {self.timeout_seconds}s")
        if "error" in out:
            exc = out["error"]
            if isinstance(exc, ImageDownloadError):
                raise exc
            raise ImageDownloadError("IMAGE_HTTP_STATUS", f"{type(exc).__name__}: {exc}") from exc
        status, payload = out["result"]  # type: ignore[misc]
        if status < 200 or status >= 300:
            raise ImageDownloadError("IMAGE_HTTP_STATUS", str(status))
        if len(payload) > self.max_bytes:
            raise ImageDownloadError("IMAGE_TOO_LARGE")
        return status, payload
