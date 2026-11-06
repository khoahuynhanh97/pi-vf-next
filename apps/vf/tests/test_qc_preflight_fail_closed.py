from video_factory.qc.service import _run_configured_preflight


def test_management_failure_is_pre_send_provider_unavailable() -> None:
    class Evaluator:
        def configured_preflight(self) -> None:
            raise RuntimeError("local management lookup failed")

    result = _run_configured_preflight(Evaluator())
    assert result == "Configured QC preflight failed before provider request (RuntimeError)"
