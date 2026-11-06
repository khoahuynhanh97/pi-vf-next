import enum
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple


class JobStatus(enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    RETRYING = "retrying"


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    func: Callable[..., Any]
    args: Tuple[Any, ...] = field(default_factory=tuple)
    kwargs: Dict[str, Any] = field(default_factory=dict)
    timeout: int = 5  # seconds
    max_retries: int = 0


@dataclass
class JobResult:
    job_id: str
    status: JobStatus
    result: Any = None
    attempts: int = 0
    error: Exception | None = None


class JobDispatcher:
    """Simple async job dispatcher with callback handling and retry logic.

    Jobs run in background threads. On completion, ``handle_success`` or
    ``handle_failure`` is invoked. ``retry`` can be called manually or from
    ``handle_failure`` when retries are remaining.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._specs: Dict[str, JobSpec] = {}
        self._results: Dict[str, JobResult] = {}
        self.events: List[Dict[str, Any]] = []  # simple event log for tests

    def dispatch(self, spec: JobSpec) -> JobResult:
        with self._lock:
            self._specs[spec.job_id] = spec
            self._results[spec.job_id] = JobResult(
                job_id=spec.job_id, status=JobStatus.PENDING, attempts=0
            )
        thread = threading.Thread(target=self._run_job, args=(spec,))
        thread.daemon = True
        thread.start()
        return self._results[spec.job_id]

    def _run_job(self, spec: JobSpec) -> None:
        attempt = 0
        while attempt <= spec.max_retries:
            attempt += 1
            try:
                # Execute with timeout using a helper thread.
                result = self._run_with_timeout(spec.func, spec.timeout, *spec.args, **spec.kwargs)
            except Exception as exc:
                self.handle_failure(spec.job_id, exc, attempt, spec)
                # Python cannot safely terminate a running thread. Retrying a
                # timed-out attempt would overlap the still-running function
                # and can duplicate paid or irreversible side effects.
                if isinstance(exc, TimeoutError):
                    break
                # If retries left, continue loop.
                if attempt > spec.max_retries:
                    break
                else:
                    self.events.append({"type": "job_retry", "job_id": spec.job_id, "attempt": attempt})
                    continue
            else:
                self.handle_success(spec.job_id, result, attempt)
                break

    def _run_with_timeout(self, func: Callable[..., Any], timeout: int, *args: Any, **kwargs: Any) -> Any:
        """Run ``func`` in a thread and enforce ``timeout`` seconds.
        Raises ``TimeoutError`` on timeout.
        """
        result_container: List[Any] = []
        exc_container: List[BaseException] = []

        def target() -> None:
            try:
                result_container.append(func(*args, **kwargs))
            except BaseException as e:  # noqa: BLE001
                exc_container.append(e)

        t = threading.Thread(target=target)
        t.start()
        t.join(timeout)
        if t.is_alive():
            # Job still running – cannot safely kill thread, mark as timeout.
            raise TimeoutError("job timed out")
        if exc_container:
            raise exc_container[0]
        return result_container[0] if result_container else None

    def handle_success(self, job_id: str, result: Any, attempts: int) -> None:
        with self._lock:
            self._results[job_id] = JobResult(
                job_id=job_id,
                status=JobStatus.SUCCESS,
                result=result,
                attempts=attempts,
            )
        self.events.append({"type": "job_success", "job_id": job_id, "result": result, "attempts": attempts})

    def handle_failure(self, job_id: str, error: Exception, attempts: int, spec: JobSpec) -> None:
        with self._lock:
            self._results[job_id] = JobResult(
                job_id=job_id,
                status=JobStatus.FAILURE,
                error=error,
                attempts=attempts,
            )
        self.events.append({"type": "job_failure", "job_id": job_id, "error": str(error), "attempts": attempts})

    def retry(self, job_id: str) -> None:
        """Manually retry a failed job if retries remain.
        Raises ``KeyError`` if unknown job.
        """
        with self._lock:
            spec = self._specs[job_id]
            result = self._results[job_id]
            if result.attempts > spec.max_retries:
                raise RuntimeError("max retries exhausted")
        # Dispatch a fresh thread for the retry.
        thread = threading.Thread(target=self._run_job, args=(spec,))
        thread.daemon = True
        thread.start()

    def get_result(self, job_id: str) -> JobResult | None:
        with self._lock:
            return self._results.get(job_id)
