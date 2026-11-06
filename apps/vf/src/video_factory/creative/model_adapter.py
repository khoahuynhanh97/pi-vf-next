"""Host-owned structured LLM adapter for Product Video creative generation."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Mapping

from .ports import MediaReference, StructuredModelResult
from .generator import StructuredGenerationError


class CreativeModelConfigurationError(RuntimeError):
    """A creative provider request failed before it could be sent."""

    request_sent = False
    retryable = False
    code = "NINE_ROUTER_CONFIGURATION_ERROR"


def _explicitly_not_sent(exc: Exception) -> bool:
    if getattr(exc, "request_sent", None) is False:
        return True
    message = str(exc).casefold()
    return any(marker in message for marker in (
        "request was not sent", "no request was sent", "no api key was found",
        "no llm provider configured", "api key is unavailable",
    ))


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 3:
        closing = next((index for index, line in enumerate(lines[1:], start=1)
                        if line.strip() == "```"), None)
        if closing is not None:
            return "\n".join(lines[1:closing]).strip()
    return text


def _usage_total(response: Any) -> int:
    usage = getattr(response, "usage", None)
    if isinstance(usage, dict):
        return int(usage.get("total_tokens") or 0)
    return int(getattr(usage, "total_tokens", 0) or 0)


class HermesStructuredCreativeModel:
    """Route creative JSON calls through Hermes' central provider resolver.

    This is intentionally an edge adapter rather than a new model tool. It
    inherits configured credentials, routing, retries and provider fallbacks
    from ``agent.auxiliary_client`` while keeping the creative core testable.
    """

    def __init__(
        self,
        *,
        caller: Callable[..., Any] | None = None,
        task: str = "product_video_creative",
        provider: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        max_tokens: int = 8192,
        main_runtime: dict[str, Any] | None = None,
    ) -> None:
        self._caller = caller
        self.task = task
        self.provider = provider
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.main_runtime = main_runtime

    def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: Mapping[str, object],
        temperature: float,
    ) -> StructuredModelResult:
        return self._generate_messages(
            messages=[{'role': 'system', 'content': system_prompt},
                      {'role': 'user', 'content': user_prompt}],
            schema_name=schema_name, json_schema=json_schema, temperature=temperature,
        )

    def generate_media_json(self, *, system_prompt, user_prompt, schema_name,
                            json_schema, temperature, media: tuple[MediaReference, ...]):
        if not media:
            raise ValueError('multimodal call requires actual media')
        content = [{'type': 'text', 'text': user_prompt}]
        for ref in media:
            content.extend(ref.content())
        return self._generate_messages(
            messages=[{'role': 'system', 'content': system_prompt},
                      {'role': 'user', 'content': content}],
            schema_name=schema_name, json_schema=json_schema, temperature=temperature,
        )

    def _generate_messages(self, *, messages, schema_name, json_schema, temperature):
        caller = self._caller
        if caller is None:
            try:
                from agent.auxiliary_client import call_llm
                caller = call_llm
            except ImportError:
                import sys
                from pathlib import Path
                for p in (
                    Path(r"D:\work\hermes-agent"),
                    Path(r"Z:\work\hermes-agent"),
                    Path(__file__).resolve().parents[3] / "hermes-agent",
                ):
                    if p.is_dir() and str(p) not in sys.path:
                        sys.path.insert(0, str(p))
                from agent.auxiliary_client import call_llm
                caller = call_llm

        route_info: dict[str, str] = {}
        try:
            response = caller(
                task=self.task,
                provider=self.provider,
                model=self.model,
                main_runtime=self.main_runtime,
                messages=messages,
                temperature=temperature,
                max_tokens=self.max_tokens,
                timeout=self.timeout,
                extra_body={
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "schema": dict(json_schema),
                            "strict": False,
                        },
                    }
                },
                route_info=route_info,
            )
        except Exception as exc:
            if _explicitly_not_sent(exc):
                raise CreativeModelConfigurationError(str(exc)) from exc
            prior_receipt = getattr(exc, "route_receipt", None)
            receipt = dict(prior_receipt) if isinstance(prior_receipt, Mapping) else {}
            receipt.update({key: value for key, value in route_info.items() if value})
            receipt.setdefault("provider", self.provider or "auto")
            receipt.setdefault("model", self.model or "default")
            receipt.setdefault("cost_status", "unknown")
            response_received = bool(
                getattr(exc, "response_received", False) or getattr(exc, "response", None) is not None
            )
            receipt.setdefault("response_received", response_received)
            error = StructuredGenerationError(
                f"creative provider outcome requires reconciliation: {type(exc).__name__}: {exc}",
                route_receipt=receipt,
                request_sent=True,
            )
            error.reconcile_required = bool(
                getattr(exc, "reconcile_required", False) or not response_received
            )
            raise error from exc

        from agent.auxiliary_client import extract_content_or_reasoning

        raw_text = extract_content_or_reasoning(response)
        parse_error = ""
        parsed: dict[str, Any] = {}
        try:
            candidate = json.loads(_strip_json_fence(raw_text))
            if not isinstance(candidate, dict):
                raise ValueError("structured response must be a JSON object")
            parsed = candidate
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            parse_error = str(exc)

        response_model = str(getattr(response, "model", "") or "").strip()
        resolved_provider = (
            route_info.get("provider") or self.provider or "auto"
        )
        resolved_model = response_model or route_info.get("model") or self.model or "default"
        return StructuredModelResult(
            parsed=parsed,
            raw_text=raw_text,
            provider=resolved_provider,
            model=resolved_model,
            total_tokens=_usage_total(response),
            parse_error=parse_error,
        )


class NineRouterHermesStructuredCreativeModel(HermesStructuredCreativeModel):
    """Hermes structured-model boundary backed by a one-shot 9Router route.

    This deliberately bypasses ``agent.auxiliary_client`` because that client
    owns retry and provider fallback policies unsuitable for a paid QC/creative
    route whose submission must be reconciled exactly once.
    """

    def __init__(self, client: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._nine_router_client = client

    def _generate_messages(self, *, messages, schema_name, json_schema, temperature):
        response, receipt = self._nine_router_client.complete(
            messages=messages,
            temperature=temperature,
            max_tokens=self.max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": dict(json_schema), "strict": False},
            },
        )
        try:
            raw_text = str(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raw_text = ""
            parse_error = f"completion envelope is invalid: {exc}"
        else:
            parse_error = ""
        parsed: dict[str, Any] = {}
        if not parse_error:
            try:
                candidate = json.loads(_strip_json_fence(raw_text))
                if not isinstance(candidate, dict):
                    raise ValueError("structured response must be a JSON object")
                parsed = candidate
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                parse_error = str(exc)
        return StructuredModelResult(
            parsed=parsed,
            raw_text=raw_text,
            provider="9router",
            model=receipt["upstream_model"],
            total_tokens=(
                int(receipt["total_tokens"])
                if isinstance(receipt.get("total_tokens"), int) and not isinstance(receipt.get("total_tokens"), bool)
                else None
            ),
            parse_error=parse_error,
            route_receipt=receipt,
            # A received response may be repaired once by the creative validator.
            # Transport ambiguity raises before a StructuredModelResult exists and is never retried here.
            retry_allowed=True,
        )
