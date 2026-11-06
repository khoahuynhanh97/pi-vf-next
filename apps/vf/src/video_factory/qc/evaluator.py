"""Multimodal vision evaluator for Image and Video QC."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Mapping

try:
    from agent.vertex_adapter import get_vertex_config
except ImportError:
    import sys
    ws_parent = Path(__file__).resolve().parents[4]
    for extra in (
        Path(r"D:\work\hermes-agent"),
        Path(r"Z:\work\hermes-agent"),
        ws_parent / "hermes-agent",
        Path(r"D:\work\hermes-upstream-rearchitecture"),
        Path(r"Z:\work\hermes-upstream-rearchitecture"),
        ws_parent / "hermes-upstream-rearchitecture",
    ):
        if extra.is_dir() and str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    try:
        from agent.vertex_adapter import get_vertex_config
    except ImportError:
        def get_vertex_config() -> tuple[None, None]:
            return None, None

from video_factory.qc.models import QCStage

logger = logging.getLogger(__name__)


_IMAGE_EXCLUSIONARY_RISK_FLAGS = frozenset({
    "seller_overlay_obscures_silhouette", "advertising_obscures_silhouette",
    "watermark_obscures_silhouette", "ad_frame_obscures_silhouette",
    "wrong_sku", "wrong_product",
    "wrong_brand", "wrong_model", "wrong_variant", "identity_mismatch",
    "corrupt_image", "undecodable_image", "severe_blur_unrecognizable",
})
_IMAGE_OBSCURATION_RISK_FLAGS = frozenset({
    "seller_overlay_obscures_silhouette", "advertising_obscures_silhouette",
    "watermark_obscures_silhouette", "ad_frame_obscures_silhouette",
})
_IMAGE_IDENTITY_RISK_FLAGS = frozenset({
    "wrong_sku", "wrong_product", "wrong_brand", "wrong_model",
    "wrong_variant", "identity_mismatch",
})


def has_exclusionary_image_risk(risk_flags: list[str] | tuple[str, ...]) -> bool:
    """Only source contamination and SKU identity flags exclude an image."""
    return any(
        re.sub(r"[^a-z0-9]+", "_", str(flag).strip().lower()).strip("_")
        in _IMAGE_EXCLUSIONARY_RISK_FLAGS
        for flag in risk_flags
    )


def is_eligible_image_result(raw: Mapping[str, Any]) -> bool:
    """Admit only explicit clean PASS image evidence; scores are advisory."""
    risk_flags = raw.get("risk_flags")
    return (
        raw.get("eligible") is True
        and raw.get("verdict") == "pass"
        and raw.get("hard_failure") is False
        and raw.get("source_branding_detected") is False
        and raw.get("identity_mismatch") is False
        and isinstance(risk_flags, (list, tuple))
        and all(isinstance(flag, str) for flag in risk_flags)
        and not has_exclusionary_image_risk(risk_flags)
    )


def is_eligible_image_evidence(item: Mapping[str, Any]) -> bool:
    """Require both the persisted item decision and its explicit visual evidence."""
    raw = item.get("findings")
    return (
        item.get("eligible") is True and item.get("verdict") == "pass"
        and item.get("hard_failure") is False
        and isinstance(raw, Mapping) and is_eligible_image_result(raw)
    )


def has_bound_image_identity(item: Mapping[str, Any], selected: list[dict[str, Any]]) -> bool:
    """Compatibility helper: every source image is assessed independently."""
    return is_eligible_image_evidence(item)


class QCResponseParseError(ValueError):
    """A fail-closed parse error with provider diagnostics safe to persist."""

    def __init__(self, raw_response: str, finish_reason: str | None, parse_error: str) -> None:
        self.raw_response = raw_response
        self.finish_reason = finish_reason
        self.parse_error = parse_error
        super().__init__(f"Could not parse JSON from model response: {parse_error}")


class QCTechnicalError(RuntimeError):
    """Raised when evaluator encounters a transient technical issue (429, timeout, network drop)."""

    def __init__(self, message: str, *, status_code: int | None = None, is_retryable: bool = True) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.is_retryable = is_retryable


def is_technical_error(exc: Exception) -> bool:
    """Classify whether an evaluator error represents a technical (transient) failure vs semantic failure."""
    if isinstance(exc, QCTechnicalError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429 or exc.code in (500, 502, 503, 504):
            return True
        if exc.code == 403:
            err_body = str(getattr(exc, "reason", ""))
            try:
                err_body += exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return "SERVICE_DISABLED" in err_body
        return False
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    msg = str(exc)
    if any(k in msg for k in ("HTTP 429", "RESOURCE_EXHAUSTED", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "SERVICE_DISABLED")):
        return True
    if any(k in msg.lower() for k in ("timeout", "timed out", "connection reset", "remotedisconnected", "network")):
        return True
    return False


def validate_qc_response(raw: dict[str, Any], stage: QCStage) -> dict[str, Any]:
    """Validate and normalize the one response contract shared by image/video QC."""
    if not isinstance(raw, dict):
        raise ValueError("QC response must be a JSON object")

    components = {
        QCStage.IMAGE: {
            "identity_score": (0, 60),
            "clarity_score": (0, 25),
            "usability_score": (0, 15),
        },
        QCStage.VIDEO: {
            "identity_consistency": (0, 50),
            "scene_adherence": (0, 30),
            "visual_cleanliness": (0, 20),
        },
        QCStage.KEYFRAME: {
            "identity_consistency": (0, 50),
            "scene_adherence": (0, 30),
            "visual_cleanliness": (0, 20),
        },
    }[stage]
    required = (*components, "total_score", "verdict", "hard_failure", "reason")
    if stage == QCStage.IMAGE:
        required += ("source_branding_detected", "identity_mismatch")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"QC response missing required fields: {', '.join(missing)}")

    normalized = dict(raw)
    for key, (lower, upper) in components.items():
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(f"QC response field '{key}' must be an integer in [{lower}, {upper}]")

    total = raw["total_score"]
    if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= 100:
        raise ValueError("QC response field 'total_score' must be an integer in [0, 100]")
    if sum(raw[key] for key in components) != total:
        raise ValueError("QC response total_score does not equal component sum")

    verdict = raw["verdict"]
    if verdict not in ("pass", "fail", "unknown"):
        raise ValueError("QC response verdict must be pass, fail, or unknown")
    if not isinstance(raw["hard_failure"], bool):
        raise ValueError("QC response hard_failure must be a boolean")
    if not isinstance(raw["reason"], str):
        raise ValueError("QC response reason must be a string")

    for key in ("source_branding_detected", "identity_mismatch"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"QC response field '{key}' must be a boolean")
            normalized[key] = raw[key]
        else:
            normalized[key] = False

    if stage != QCStage.IMAGE and (normalized["source_branding_detected"] or normalized["identity_mismatch"]):
        normalized["hard_failure"] = True
        normalized["verdict"] = "fail"

    if stage == QCStage.IMAGE:
        eligible = raw.get("eligible")
        if not isinstance(eligible, bool):
            raise ValueError("Image QC response eligible must be a boolean")
        profile_fields = (
            "view_angle",
            "shot_type",
            "visible_details",
            "scene_suitability",
            "risk_flags",
            "text_coverage_percent",
        )
        missing_profile = [key for key in profile_fields if key not in raw]
        if missing_profile:
            raise ValueError(
                f"Image QC response missing required fields: {', '.join(missing_profile)}"
            )
        for key in ("view_angle", "shot_type"):
            value = raw[key]
            if not isinstance(value, str):
                raise ValueError(f"Image QC response field '{key}' must be a string")
            normalized[key] = value.strip()
        for key in ("visible_details", "scene_suitability", "risk_flags"):
            value = raw[key]
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"Image QC response field '{key}' must be a string list")
            normalized[key] = [item.strip() for item in value if item.strip()]
        # Generic shop/watermark flags are descriptive only. Exclusion requires
        # one of the rubric's explicit visible failure categories.
        exclusionary_flags = [
            flag for flag in normalized["risk_flags"]
            if re.sub(r"[^a-z0-9]+", "_", flag.strip().lower()).strip("_")
            in _IMAGE_EXCLUSIONARY_RISK_FLAGS
        ]
        normalized["source_branding_detected"] = any(
            flag in _IMAGE_OBSCURATION_RISK_FLAGS for flag in exclusionary_flags
        )
        normalized["identity_mismatch"] = (normalized["identity_mismatch"] is True) or any(
            flag in _IMAGE_IDENTITY_RISK_FLAGS for flag in exclusionary_flags
        )
        concrete_failure = has_exclusionary_image_risk(exclusionary_flags) or (normalized["identity_mismatch"] is True)
        nonblocking_branding_only = (
            bool(raw.get("source_branding_detected"))
            and not raw["hard_failure"]
            and not concrete_failure
        )
        if (verdict == "pass" and (not eligible or raw["hard_failure"] or concrete_failure)) or (verdict != "pass" and eligible):
            raise ValueError("Image QC response has contradictory verdict, eligibility or failure evidence")
        if concrete_failure:
            if verdict != "fail":
                raise ValueError("Image QC failure evidence requires a fail verdict")
            normalized["hard_failure"] = True
        elif verdict == "fail" and nonblocking_branding_only:
            normalized["verdict"] = "pass"
            normalized["eligible"] = True
            normalized["policy_override"] = "non_obscuring_branding"
        normalized["eligible"] = is_eligible_image_result(normalized)
        text_coverage = raw["text_coverage_percent"]
        if isinstance(text_coverage, bool) or not isinstance(text_coverage, int) or not 0 <= text_coverage <= 100:
            raise ValueError("Image QC response text_coverage_percent must be an integer in [0, 100]")
        normalized["text_coverage_percent"] = text_coverage

    if stage == QCStage.KEYFRAME:
        if "black_bars_detected" in raw:
            bb = raw["black_bars_detected"]
            if not isinstance(bb, bool):
                raise ValueError("Keyframe QC response field 'black_bars_detected' must be a boolean")
            normalized["black_bars_detected"] = bb
            if bb is True:
                normalized.setdefault("warnings", []).append("black_bars_detected:aesthetic_warning")
        else:
            normalized["black_bars_detected"] = "unknown"
            normalized.setdefault("warnings", []).append("black_bars_detected:unknown")
        gate_fields = (
            "prompt_reference_consistent",
            "script_adherence",
            "important_details_preserved",
            "composition_usable",
            "animation_suitable",
        )
        missing_gates = [key for key in (*gate_fields, "recommended_action") if key not in raw]
        if missing_gates:
            raise ValueError(
                f"Keyframe QC response missing required fields: {', '.join(missing_gates)}"
            )
        for key in gate_fields:
            value = raw[key]
            if not isinstance(value, bool):
                raise ValueError(f"Keyframe QC response field '{key}' must be a boolean")
            normalized[key] = value
        action = raw.get("recommended_action")
        if action is None:
            action = "none"
        elif not isinstance(action, str):
            action = str(action)
        normalized["recommended_action"] = action.strip()
        blocking_gates = (
            "prompt_reference_consistent",
            "script_adherence",
            "important_details_preserved",
            "composition_usable",
        )
        if not all(normalized[key] for key in blocking_gates):
            normalized["hard_failure"] = True
            normalized["verdict"] = "fail"
    return normalized


def encode_image_to_base64(image_path: str | Path) -> tuple[str, str]:
    """Read image file and return (mime_type, base64_str)."""
    p = Path(image_path)
    if not p.is_file():
        raise FileNotFoundError(f"Image file not found: {image_path}")
    mime, _ = mimetypes.guess_type(str(p))
    if not mime:
        mime = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode("utf-8")
    return mime, b64


def _urlopen_with_backoff(
    req: urllib.request.Request,
    timeout: float,
    *,
    max_retries: int = 2,
    backoff: float = 5.0,
) -> dict[str, Any]:
    """Execute HTTP request with bounded backoff on 429 Resource Exhausted."""
    import time
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                sleep_secs = backoff * (attempt + 1)
                logger.warning(
                    "Vertex AI 429 rate limit, backing off %.1fs (attempt %d/%d)...",
                    sleep_secs,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(sleep_secs)
                continue
            raise


class MultimodalEvaluator:
    """Configured multimodal evaluator using Google Cloud Vertex AI Gemini."""

    def __init__(
        self,
        model: str = "google/gemini-2.5-flash",
        evaluator_name: str = "vertex_gemini_vision",
        evaluator_version: str = "1.0",
        timeout: float = 90.0,
        fake_mode: bool = False,
    ) -> None:
        self.model = model
        self.evaluator_name = evaluator_name
        self.evaluator_version = evaluator_version
        self.timeout = timeout
        self.fake_mode = fake_mode

    def configured_preflight(self) -> str | None:
        """Return a safe diagnostic before reserving budget or claiming a paid QC attempt."""
        if self.fake_mode:
            return "Configured Image QC cannot use the fake evaluator"
        try:
            token, base_url = get_vertex_config()
        except Exception as exc:
            return f"Vertex QC configuration is unavailable: {type(exc).__name__}"
        if not token or not base_url:
            return "Vertex QC credentials or base URL are unavailable"
        return None

    def evaluate_image(
        self,
        image_path: str | Path,
        prompt: str,
        system_instruction: str | None = None,
    ) -> dict[str, Any]:
        """Send image and prompt to multimodal model and return structured JSON."""
        if self.fake_mode:
            return {
                "verdict": "pass",
                "total_score": 90,
                "identity_score": 55,
                "clarity_score": 23,
                "usability_score": 12,
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "eligible": True,
                "reason": "Offline test evaluation passed",
                "view_angle": "front three-quarter",
                "shot_type": "clean product reference",
                "visible_details": [],
                "scene_suitability": ["hero", "context"],
                "risk_flags": [],
                "text_coverage_percent": 0,
            }

        mime, b64 = encode_image_to_base64(image_path)
        token, base_url = get_vertex_config()
        if not token or not base_url:
            raise RuntimeError("Vertex AI credentials or base_url unavailable for MultimodalEvaluator")

        endpoint = f"{base_url}/chat/completions"
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})

        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        })

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 8192,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )

        try:
            raw_resp = _urlopen_with_backoff(req, timeout=self.timeout)
            return self._parse_completion(raw_resp)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            logger.error("MultimodalEvaluator HTTP error %d: %s", exc.code, err_body)
            if exc.code == 429 or exc.code in (500, 502, 503, 504):
                raise QCTechnicalError(f"MultimodalEvaluator HTTP {exc.code}: {err_body}", status_code=exc.code, is_retryable=False) from exc
            if exc.code == 403 and "SERVICE_DISABLED" in err_body:
                raise QCTechnicalError(f"MultimodalEvaluator HTTP 403: {err_body}", status_code=403, is_retryable=False) from exc
            raise RuntimeError(f"MultimodalEvaluator HTTP {exc.code}: {err_body}") from exc
        except urllib.error.URLError as exc:
            raise QCTechnicalError(f"MultimodalEvaluator network error: {exc.reason}", is_retryable=False) from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise QCTechnicalError(f"MultimodalEvaluator connection/timeout error: {exc}", is_retryable=False) from exc
        except Exception as exc:
            logger.error("MultimodalEvaluator call failed: %s", exc)
            raise

    def evaluate_multiple_images(
        self,
        image_paths: list[str | Path],
        prompt: str,
        system_instruction: str | None = None,
    ) -> dict[str, Any]:
        """Send multiple images (e.g. reference vs video frames) and prompt to model."""
        if self.fake_mode:
            from video_factory.qc.rubric import IMAGE_QC_SYSTEM_PROMPT
            if system_instruction == IMAGE_QC_SYSTEM_PROMPT:
                return self.evaluate_image(image_paths[-1], prompt, system_instruction)
            return {
                "verdict": "pass",
                "total_score": 88,
                "identity_consistency": 45,
                "scene_adherence": 26,
                "visual_cleanliness": 17,
                "hard_failure": False,
                "source_branding_detected": False,
                "identity_mismatch": False,
                "black_bars_detected": False,
                "reason": "Offline multi-image test evaluation passed",
                "prompt_reference_consistent": True,
                "script_adherence": True,
                "important_details_preserved": True,
                "composition_usable": True,
                "animation_suitable": True,
                "recommended_action": "none",
            }

        token, base_url = get_vertex_config()
        if not token or not base_url:
            raise RuntimeError("Vertex AI credentials or base_url unavailable for MultimodalEvaluator")

        endpoint = f"{base_url}/chat/completions"
        content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for p in image_paths:
            mime, b64 = encode_image_to_base64(p)
            content_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": content_parts})

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 8192,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )

        try:
            raw_resp = _urlopen_with_backoff(req, timeout=self.timeout)
            return self._parse_completion(raw_resp)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            logger.error("MultimodalEvaluator HTTP error %d: %s", exc.code, err_body)
            if exc.code == 429 or exc.code in (500, 502, 503, 504):
                raise QCTechnicalError(f"MultimodalEvaluator HTTP {exc.code}: {err_body}", status_code=exc.code, is_retryable=False) from exc
            if exc.code == 403 and "SERVICE_DISABLED" in err_body:
                raise QCTechnicalError(f"MultimodalEvaluator HTTP 403: {err_body}", status_code=403, is_retryable=False) from exc
            raise RuntimeError(f"MultimodalEvaluator HTTP {exc.code}: {err_body}") from exc
        except urllib.error.URLError as exc:
            raise QCTechnicalError(f"MultimodalEvaluator network error: {exc.reason}", is_retryable=False) from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise QCTechnicalError(f"MultimodalEvaluator connection/timeout error: {exc}", is_retryable=False) from exc
        except Exception as exc:
            logger.error("MultimodalEvaluator multi-image call failed: %s", exc)
            raise

    @classmethod
    def _parse_completion(cls, raw_response: dict[str, Any]) -> dict[str, Any]:
        """Extract the first completion while retaining finish metadata on failure."""
        try:
            choice = raw_response["choices"][0]
            finish_reason = choice.get("finish_reason")
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raw = json.dumps(raw_response, ensure_ascii=False, sort_keys=True)
            raise QCResponseParseError(raw, None, f"completion envelope is invalid: {exc}") from exc

        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        if not isinstance(content, str):
            raise QCResponseParseError(str(content), finish_reason, "completion content is not text")
        return cls._parse_json_response(content, finish_reason=finish_reason)

    @staticmethod
    def _parse_json_response(text: str, *, finish_reason: str | None = None) -> dict[str, Any]:
        """Extract and parse JSON from model output."""
        cleaned = text.strip()
        # strip markdown code fences if present
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        try:
            return json.loads(cleaned)
        except (TypeError, ValueError, json.JSONDecodeError) as first_error:
            # try to find first { and last }
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except (TypeError, ValueError, json.JSONDecodeError) as nested_error:
                    raise QCResponseParseError(text, finish_reason, str(nested_error)) from nested_error
            raise QCResponseParseError(text, finish_reason, str(first_error)) from first_error
