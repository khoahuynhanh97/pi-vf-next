"""Named 9Router combo routes for creative and vision QC calls.

9Router owns member ordering and rotation. Video Factory sends one request to the
configured purpose combo, without selecting a member or retrying across members.
Creative output is gated by JSON parsing and the creative validators; missing
upstream route metadata is retained as unknown for audit.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
import hashlib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from video_factory.config import load_config
from video_factory.qc.evaluator import QCTechnicalError, encode_image_to_base64


class NineRouterConfigurationError(RuntimeError):
    """A router route has not established the fail-closed contract."""


class NineRouterResponseSchemaError(QCTechnicalError):
    """A provider response arrived but could not satisfy the QC response contract."""

    def __init__(self, message: str, *, route_receipt: Mapping[str, Any]) -> None:
        super().__init__(message, is_retryable=False)
        self.request_sent = True
        self.response_received = True
        self.reconcile_required = False
        self.usage_status = "consumed"
        self.retry_allowed = True
        self.route_receipt = dict(route_receipt)


class NineRouterSubmissionUncertain(QCTechnicalError):
    """The request may have reached 9Router; reconcile before any retry."""

    def __init__(self, message: str, *, route_receipt: Mapping[str, Any]) -> None:
        super().__init__(message, is_retryable=False)
        self.request_sent = True
        self.reconcile_required = True
        self.route_receipt = dict(route_receipt)

    def __str__(self) -> str:
        return (
            f"{super().__str__()} | route_receipt="
            f"{json.dumps(self.route_receipt, sort_keys=True, ensure_ascii=True)}"
        )


class NineRouterHTTPResponseInvalid(RuntimeError):
    """An HTTP response arrived, but its outer JSON body was invalid."""

    def __init__(self, message: str, *, route_receipt: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.request_sent = True
        self.response_received = True
        self.reconcile_required = False
        self.route_receipt = dict(route_receipt)


@dataclass(frozen=True)
class NineRouterRoute:
    stage: str
    endpoint: str
    combo_id: str
    combo_name: str
    model_id: str
    execution_mode: str
    capabilities: frozenset[str]

    @classmethod
    def from_mapping(cls, stage: str, value: Mapping[str, Any]) -> "NineRouterRoute":
        if value.get("enabled") is not True:
            raise NineRouterConfigurationError(f"9Router {stage} feature flag is disabled")
        endpoint = str(value.get("endpoint") or "").rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise NineRouterConfigurationError("9Router endpoint must be an explicit loopback http URL")
        if not parsed.path.endswith("/v1"):
            raise NineRouterConfigurationError("9Router endpoint must end with /v1")
        combo_id = str(value.get("combo_id") or "").strip()
        if not combo_id or any(char.isspace() for char in combo_id):
            raise NineRouterConfigurationError("9Router requires an immutable combo_id, not a combo name")
        combo_name = str(value.get("combo_name") or "").strip()
        model_id = str(value.get("model_id") or "").strip()
        execution_mode = str(value.get("execution_mode") or "").strip()
        if not combo_name:
            raise NineRouterConfigurationError("9Router requires the live combo name associated with combo_id")
        if execution_mode not in {"combo", "single_model"}:
            raise NineRouterConfigurationError("9Router execution_mode must be combo or single_model")
        if execution_mode == "single_model" and not model_id:
            raise NineRouterConfigurationError("9Router single_model route requires an explicit model_id")
        # Capability metadata is accepted only as a requested contract and is
        # verified by the live one-request contract test, never by these config
        # strings or boolean flags.
        capabilities = frozenset(str(item).strip() for item in value.get("capabilities", []) if str(item).strip())
        return cls(stage, endpoint, combo_id, combo_name, model_id, execution_mode, capabilities)


def verify_local_router_route(route: NineRouterRoute, *, db_path: Path | None = None) -> None:
    """Validate the selected combo and its members without reading credentials.

    9Router's local database stores member model identifiers but no capability
    metadata.  Therefore each member needs an explicit result from an offline
    contract test; absent evidence is not treated as text or vision support.
    """
    path = db_path or _router_data_dir() / "db" / "data.sqlite"
    try:
        database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = database.execute("SELECT models FROM combos WHERE id = ?", (route.combo_id,)).fetchone()
        setting = database.execute("SELECT data FROM settings LIMIT 1").fetchone()
    except (sqlite3.Error, OSError) as exc:
        raise NineRouterConfigurationError("9Router local route metadata is unavailable") from exc
    finally:
        try:
            database.close()
        except UnboundLocalError:
            pass
    if row is None:
        raise NineRouterConfigurationError("9Router combo_id is not present in local router metadata")
    try:
        members = tuple(str(item) for item in json.loads(row[0]) if str(item).strip())
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NineRouterConfigurationError("9Router combo members are invalid") from exc
    if not members:
        raise NineRouterConfigurationError("9Router combo has no members")
    try:
        settings = json.loads(setting[0]) if setting and setting[0] else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        settings = {}
    strategies = settings.get("providerStrategies") if isinstance(settings, Mapping) else None
    if isinstance(strategies, Mapping) and any(
        isinstance(policy, Mapping) and policy.get("fallbackStrategy") not in (None, "disabled", "forbid")
        for policy in strategies.values()
    ):
        raise NineRouterConfigurationError("9Router local policy enables provider fallback; route is unsafe")
    # The config is a contract-test result, not a model-name guess.  It binds
    # every current combo member to tested capabilities and detects membership
    # drift before a request can be sent.
    # The attributes are attached by _route_from_config only after parsing.
    expected_members = getattr(route, "expected_members", ())
    member_capabilities = getattr(route, "member_capabilities", {})
    if tuple(expected_members) != members:
        raise NineRouterConfigurationError("9Router combo members differ from the tested contract")
    required = {"json_schema"}
    if route.stage == "vision_qc":
        required.add("vision_single_image")
    for member in members:
        supported = set(member_capabilities.get(member, ()))
        if not required.issubset(supported):
            raise NineRouterConfigurationError("9Router combo member lacks a tested required capability")


def _route_from_config(stage: str, config: Mapping[str, Any] | None = None) -> NineRouterRoute | None:
    root = config if config is not None else load_config()
    product_video = root.get("product_video") if isinstance(root, Mapping) else None
    nine_router = product_video.get("nine_router") if isinstance(product_video, Mapping) else None
    if not isinstance(nine_router, Mapping) or nine_router.get("enabled") is not True:
        return None
    routes = nine_router.get("routes")
    value = routes.get(stage) if isinstance(routes, Mapping) else None
    if not isinstance(value, Mapping) or value.get("enabled") is False:
        return None
    merged = dict(value)
    merged["enabled"] = True
    merged.setdefault("endpoint", nine_router.get("endpoint"))
    route = NineRouterRoute.from_mapping(stage, merged)
    # Preserve test-attested fields for an operator's explicit local metadata
    # preflight.  The SQLite cache is deliberately not authoritative: the
    # dashboard API can contain newer Layers than that cache.
    object.__setattr__(route, "expected_members", tuple(str(item) for item in merged.get("expected_members", [])))
    raw_capabilities = merged.get("member_capabilities")
    object.__setattr__(route, "member_capabilities", raw_capabilities if isinstance(raw_capabilities, Mapping) else {})
    return route


def _router_data_dir() -> Path:
    configured_dir = os.environ.get("DATA_DIR")
    if configured_dir:
        return Path(configured_dir)
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "9router"


def _load_local_api_key() -> str:
    """Load an active local API key into memory without exposing it to logs."""
    db_path = _router_data_dir() / "db" / "data.sqlite"
    try:
        database = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = database.execute("SELECT key FROM apiKeys WHERE isActive = 1 ORDER BY createdAt DESC LIMIT 1").fetchone()
        database.close()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row else ""


def _admin_token() -> str:
    root = _router_data_dir()
    try:
        machine_id = (root / "machine-id").read_text(encoding="utf-8").strip()
        secret = (root / "auth" / "cli-secret").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not machine_id or not secret:
        return ""
    return hashlib.sha256((machine_id + "9r-cli-auth" + secret).encode()).hexdigest()[:16]


def _admin_get(path: str, *, api_key: str = "") -> Any:
    token = _admin_token()
    if not token:
        raise NineRouterConfigurationError("9Router local management credentials are unavailable")
    request = urllib.request.Request(
        f"http://127.0.0.1:20128{path}",
        headers={"x-9r-cli-token": token, **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise NineRouterConfigurationError(
            f"9Router management {path.split('?', 1)[0]} returned HTTP {exc.code}"
        ) from exc
    except (OSError, urllib.error.URLError, ValueError) as exc:
        failure = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        failure_type = type(failure).__name__
        raise NineRouterConfigurationError(
            "9Router local management API could not verify the route "
            f"({failure_type})"
        ) from exc


def _active_provider_connection_count(provider: str) -> int:
    """Count active provider credentials using live, sanitized management data."""
    try:
        payload = _admin_get("/api/providers")
    except NineRouterConfigurationError as exc:
        raise NineRouterConfigurationError("9Router live provider metadata is unavailable") from exc
    connections = payload.get("connections") if isinstance(payload, Mapping) else None
    if not isinstance(connections, list):
        raise NineRouterConfigurationError("9Router live provider connection list is invalid")
    return sum(
        1
        for item in connections
        if isinstance(item, Mapping)
        and str(item.get("provider") or "") == provider
        and item.get("isActive") is True
    )


def _live_route_preflight(route: NineRouterRoute, *, api_key: str) -> str | None:
    """Check the current route and stage capabilities against live 9Router data."""
    if not api_key:
        return "9Router API key is unavailable; no request was sent"
    combo_envelope = _admin_get(f"/api/combos/{route.combo_id}")
    combo = combo_envelope.get("data", combo_envelope) if isinstance(combo_envelope, Mapping) else {}
    if not isinstance(combo, Mapping) or str(combo.get("id") or "") != route.combo_id:
        return "Configured 9Router combo_id does not resolve in live management API"
    if str(combo.get("name") or "") != route.combo_name:
        return "Configured 9Router combo_name does not match live combo_id"
    members = combo.get("models")
    if not isinstance(members, list) or not members:
        return "Live 9Router combo has no members"
    live_members = tuple(str(item).strip() for item in members if str(item).strip())
    configured_members = tuple(getattr(route, "expected_members", ()))
    if configured_members and configured_members != live_members:
        return "Live 9Router combo members changed since the route contract was configured"
    object.__setattr__(route, "expected_members", live_members)
    settings = _admin_get("/api/settings")
    combo_strategy = str(settings.get("comboStrategy") or "") if isinstance(settings, Mapping) else ""
    if route.execution_mode == "combo":
        strategy_map = settings.get("comboStrategies") if isinstance(settings, Mapping) else None
        strategy = strategy_map.get(route.combo_name) if isinstance(strategy_map, Mapping) else None
        strategy = str(strategy or combo_strategy)
        normalized_strategy = strategy.strip().lower().replace("_", "-")
        if normalized_strategy not in {"fallback", "round-robin"}:
            return (
                f"Live 9Router combo strategy is {strategy or 'unset'}, which is not supported "
                "for a single-request route"
            )
        api_models = _admin_get("/v1/models", api_key=api_key)
        model_rows = api_models.get("data", []) if isinstance(api_models, Mapping) else []
        rows = {
            str(row.get("id") or ""): row
            for row in model_rows
            if isinstance(row, Mapping)
        }
        for member in live_members:
            model_row = rows.get(member)
            if model_row is None:
                return f"Combo member {member} is not currently served by 9Router"
            capabilities = model_row.get("capabilities")
            if route.stage == "vision_qc" and not (
                isinstance(capabilities, Mapping) and capabilities.get("vision") is True
            ):
                return f"Combo member {member} does not have live vision capability"
        # JSON-schema support is not exposed by the local model catalog. The
        # caller verifies it with the actual structured request and receipt.
        object.__setattr__(route, "live_combo_strategy", strategy)
        return None
    if route.model_id not in members:
        return "Configured pilot model_id is not a member of the live combo"
    provider = route.model_id.split("/", 1)[0]
    provider_aliases = {
        "gc": "gemini-cli",
        "kr": "kiro",
        "qd": "qoder",
        "cf": "cloudflare-ai",
    }
    target_provider = provider_aliases.get(provider, provider)
    try:
        active_connections = _active_provider_connection_count(target_provider)
    except NineRouterConfigurationError as exc:
        return f"{exc}; cannot prove a single upstream attempt"
    if active_connections < 1:
        return "Live 9Router has no active credential for the selected model provider"
    api_models = _admin_get("/v1/models", api_key=api_key)
    model_rows = api_models.get("data", []) if isinstance(api_models, Mapping) else []
    model_row = next((row for row in model_rows if isinstance(row, Mapping) and str(row.get("id") or "") == route.model_id), None)
    if model_row is None:
        return "Configured pilot model_id is not currently served by 9Router"
    capabilities = model_row.get("capabilities")
    has_vision = False
    if isinstance(capabilities, Mapping) and capabilities.get("vision") is True:
        has_vision = True
    elif any(name in route.model_id.lower() for name in ("sonnet", "opus", "haiku", "gemini", "gpt-4", "vl", "vision")):
        has_vision = True

    if route.stage == "vision_qc" and not has_vision:
        return "Selected 9Router model has no live vision capability metadata"
    return None


def _usage_marker(api_key: str) -> dict[str, Any]:
    """Snapshot the live router usage counters before a single request."""
    del api_key  # The authenticated local API returns aggregate usage without key material.
    try:
        history = _admin_get("/api/usage/history?limit=20")
        total_requests = int(history["totalRequests"])
        total_cost = float(history["totalCost"])
        recent = history.get("recentRequests", [])
        latest_timestamp = str(recent[0].get("timestamp") or "") if recent and isinstance(recent[0], Mapping) else ""
    except (NineRouterConfigurationError, KeyError, TypeError, ValueError) as exc:
        raise NineRouterConfigurationError("9Router live usage ledger is unavailable; request was not sent") from exc
    if total_requests < 0 or total_cost < 0:
        raise NineRouterConfigurationError("9Router live usage counters are invalid; request was not sent")
    return {
        "total_requests": total_requests,
        "total_cost": total_cost,
        "latest_timestamp": latest_timestamp,
    }


def _read_usage_since(marker: Mapping[str, Any], api_key: str) -> list[dict[str, Any]]:
    """Reconcile a request only when live usage totals show exactly one new row."""
    del api_key
    history = _admin_get("/api/usage/history?limit=20")
    try:
        request_delta = int(history["totalRequests"]) - int(marker["total_requests"])
        cost_delta = float(history["totalCost"]) - float(marker["total_cost"])
        recent = history.get("recentRequests", [])
        prior_timestamp = str(marker.get("latest_timestamp") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise NineRouterConfigurationError("9Router live usage ledger could not reconcile the request") from exc
    if request_delta != 1 or cost_delta < 0 or not isinstance(recent, list):
        return []
    new_rows = [
        row for row in recent
        if isinstance(row, Mapping) and str(row.get("timestamp") or "") > prior_timestamp
    ]
    if len(new_rows) != 1:
        return []
    row = dict(new_rows[0])
    row["id"] = str(row.get("timestamp") or "")
    row["cost"] = cost_delta
    row["tokens"] = row.get("promptTokens", 0) + row.get("completionTokens", 0)
    return [row]


def _default_transport(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response_bytes = response.read()
        response_hash = hashlib.sha256(response_bytes).hexdigest()
        try:
            text = response_bytes.decode("utf-8")
            if "data: [DONE]" in text:
                text = text.split("data: [DONE]")[0].strip()
            raw = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            line = getattr(exc, "lineno", None)
            column = getattr(exc, "colno", None)
            detail = f" at line {line}, column {column}" if line is not None else ""
            raise NineRouterHTTPResponseInvalid(
                f"9Router HTTP response body is not valid JSON{detail}",
                route_receipt={
                    "http_status": int(getattr(response, "status", 200) or 200),
                    "response_received": True,
                    "response_bytes": len(response_bytes),
                    "response_sha256": response_hash,
                    "validation_error": "invalid_json",
                },
            ) from exc
    if not isinstance(raw, dict):
        raise NineRouterHTTPResponseInvalid(
            "9Router HTTP response JSON must be an object",
            route_receipt={
                "http_status": int(getattr(response, "status", 200) or 200),
                "response_received": True,
                "response_bytes": len(response_bytes),
                "response_sha256": response_hash,
                "validation_error": "json_not_object",
            },
        )
    raw["__vf_transport_trace"] = {
        "http_status": int(getattr(response, "status", 200) or 200),
        "response_received": True,
        "response_bytes": len(response_bytes),
        "response_sha256": response_hash,
    }
    return raw


_HTTP_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|authorization|cookie)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"
    ),
)


def _safe_http_error_details(exc: urllib.error.HTTPError) -> dict[str, Any]:
    """Capture a router error receipt without persisting credentials or request content."""
    try:
        response_bytes = exc.read(64 * 1024)
    except OSError:
        response_bytes = b""
    response_hash = hashlib.sha256(response_bytes).hexdigest()
    raw_text = response_bytes.decode("utf-8", errors="replace")
    safe_text = raw_text
    for pattern in _HTTP_SECRET_PATTERNS:
        safe_text = pattern.sub(lambda match: f"{match.group(1) if match.lastindex else ''}[REDACTED]", safe_text)
    safe_text = safe_text[:1200]

    response_id = ""
    error_code = ""
    try:
        payload = json.loads(raw_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = None

    def visit(value: Any) -> None:
        nonlocal response_id, error_code
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in {"request_id", "response_id", "trace_id", "correlation_id", "id"} and not response_id:
                    if isinstance(item, (str, int)):
                        response_id = str(item)[:200]
                if normalized in {"code", "error_code", "type"} and not error_code:
                    if isinstance(item, (str, int)):
                        error_code = str(item)[:200]
                visit(item)
        elif isinstance(value, list):
            for item in value[:50]:
                visit(item)

    visit(payload)
    headers = exc.headers
    router_request_id = ""
    if headers is not None:
        accepted_header_names = {"x-request-id", "request-id", "x-correlation-id", "cf-ray"}
        for header_name, value in headers.items():
            if str(header_name).lower() in accepted_header_names and value:
                router_request_id = str(value)[:200]
                break
    status = int(exc.code)
    signal = f"{status} {error_code} {safe_text}".lower()
    if status == 429 or "rate_limit" in signal or "quota" in signal:
        category = "quota_rate_limit"
    elif 500 <= status <= 599:
        category = "server_error"
    elif status in {400, 401, 403, 404, 405, 413, 415, 422}:
        category = "request_or_configuration"
    else:
        category = "router_http_error"
    return {
        "http_status": status,
        "error_category": category,
        "error_code": error_code or None,
        "router_request_id": router_request_id or None,
        "response_id": response_id or None,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "response_received": True,
        "response_bytes": len(response_bytes),
        "response_body_sha256": response_hash,
        "response_body_excerpt": safe_text,
    }


class NineRouterOpenAIClient:
    """A one-shot OpenAI-compatible request client with receipt verification."""

    def __init__(
        self,
        route: NineRouterRoute,
        *,
        api_key: str | None = None,
        timeout: float = 90.0,
        transport: Callable[[urllib.request.Request, float], dict[str, Any]] | None = None,
        preflight: Callable[[NineRouterRoute, str], str | None] | None = None,
        usage_reader: Callable[[Mapping[str, Any], str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.route = route
        self.api_key = api_key or _load_local_api_key()
        self.timeout = timeout
        self._transport = transport or _default_transport
        self._preflight = preflight or _live_route_preflight
        self._usage_reader = usage_reader or _read_usage_since

    def configured_preflight(self) -> str | None:
        return self._preflight(self.route, api_key=self.api_key)

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._transport is _default_transport:
            error = self._preflight(self.route, api_key=self.api_key)
            if error:
                raise NineRouterConfigurationError(f"{error}; request was not sent")
        if not self.api_key and self._transport is _default_transport:
            raise NineRouterConfigurationError("9Router API key is unavailable; request was not sent")
        body: dict[str, Any] = {
            "model": self.route.model_id if self.route.execution_mode == "single_model" else self.route.combo_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format is not None:
            body["response_format"] = dict(response_format)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        local_attempt_id = f"vf9r_{uuid.uuid4().hex}"
        request = urllib.request.Request(
            f"{self.route.endpoint}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={**headers, "X-Video-Factory-Attempt-ID": local_attempt_id},
        )
        usage_marker = _usage_marker(self.api_key) if self._transport is _default_transport else -1
        try:
            response = self._transport(request, self.timeout)
        except NineRouterHTTPResponseInvalid as exc:
            route_receipt = {
                "route": "9router",
                "combo_id": self.route.combo_id,
                "requested_model": str(body["model"]),
                "attempt_id": local_attempt_id,
                "upstream_provider": "unknown",
                "upstream_model": "unknown",
                "fallback_used": "unknown",
                "cost_status": "unknown",
                **dict(exc.route_receipt),
            }
            raise exc.__class__(str(exc), route_receipt=route_receipt) from exc
        except urllib.error.HTTPError as exc:
            route_receipt = {
                "route": "9router",
                "combo_id": self.route.combo_id,
                "combo_name": self.route.combo_name,
                "requested_model": str(body["model"]),
                "attempt_id": local_attempt_id,
                "upstream_provider": "unknown",
                "upstream_model": "unknown",
                "fallback_used": "unknown",
                "cost_status": "uncertain",
                "reconcile_required": True,
                "usage_before": dict(usage_marker) if isinstance(usage_marker, Mapping) else None,
                **_safe_http_error_details(exc),
            }
            raise NineRouterSubmissionUncertain(
                f"9Router returned HTTP {exc.code} ({route_receipt['error_category']}); upstream outcome remains uncertain",
                route_receipt=route_receipt,
            ) from exc
        except Exception as exc:
            route_receipt: dict[str, Any] = {
                "route": "9router",
                "combo_id": self.route.combo_id,
                "requested_model": str(body["model"]),
                "attempt_id": local_attempt_id,
                "upstream_provider": "unknown",
                "upstream_model": "unknown",
                "fallback_used": "unknown",
                "cost_status": "uncertain",
                "response_received": False,
                "usage_before": dict(usage_marker) if isinstance(usage_marker, Mapping) else None,
            }
            if isinstance(usage_marker, Mapping):
                try:
                    rows = self._usage_reader(usage_marker, self.api_key)
                except Exception:
                    rows = []
                if len(rows) == 1:
                    row = rows[0]
                    route_receipt.update(
                        {
                            "router_usage_id": str(row.get("id") or ""),
                            "upstream_provider": str(row.get("provider") or "unknown"),
                            "upstream_model": str(row.get("model") or "unknown"),
                            "cost_status": "recorded" if row.get("cost") is not None else "uncertain",
                            "cost_usd": row.get("cost"),
                            "prompt_tokens": row.get("promptTokens"),
                            "completion_tokens": row.get("completionTokens"),
                            "total_tokens": row.get("tokens"),
                            "usage_status": str(row.get("status") or "unknown"),
                        }
                    )
            raise NineRouterSubmissionUncertain(
                f"9Router request may have been sent; reconciliation is required before retry: {type(exc).__name__}",
                route_receipt=route_receipt,
            ) from exc
        try:
            router_receipt = response.get("router") if isinstance(response, Mapping) else None
            if (
                not isinstance(router_receipt, Mapping)
                and self._transport is _default_transport
                and not (self.route.stage == "creative" and self.route.execution_mode == "combo")
            ):
                rows = self._usage_reader(usage_marker, self.api_key)
                if len(rows) != 1:
                    raise NineRouterConfigurationError("Router usage ledger did not identify exactly one upstream request")
                usage_row = rows[0]
                upstream_provider = str(usage_row.get("provider") or "").strip()
                upstream_model = str(usage_row.get("model") or "").strip()
                qualified_model = f"{upstream_provider}/{upstream_model}" if upstream_provider and not upstream_model.startswith(f"{upstream_provider}/") else upstream_model
                expected_members = tuple(getattr(self.route, "expected_members", ()))
                if qualified_model in expected_members:
                    final_model = qualified_model
                elif upstream_model in expected_members:
                    final_model = upstream_model
                elif self.route.model_id in (upstream_model, qualified_model):
                    final_model = self.route.model_id
                else:
                    final_model = qualified_model or upstream_model

                router_receipt = {
                    "requested_combo_id": self.route.combo_id,
                    "fallback_used": bool(usage_row.get("fallback_used", False)),
                    "attempt_id": str(usage_row.get("id") or local_attempt_id),
                    "upstream_provider": upstream_provider,
                    "upstream_model": final_model,
                    "cost_status": "recorded" if usage_row.get("cost") is not None else "uncertain",
                    "cost_usd": usage_row.get("cost"),
                    "ledger_token_usage": {
                        "prompt_tokens": usage_row.get("promptTokens"),
                        "completion_tokens": usage_row.get("completionTokens"),
                        "total_tokens": usage_row.get("tokens"),
                    },
                }
                response = dict(response)
                response["router"] = dict(router_receipt)
                if "usage" not in response and isinstance(router_receipt.get("ledger_token_usage"), Mapping):
                    response["usage"] = dict(router_receipt["ledger_token_usage"])
            if self.route.stage == "creative" and self.route.execution_mode == "combo":
                receipt = self._validate_creative_receipt(response)
            else:
                receipt = self._validate_receipt(response)
            receipt["attempt_id"] = local_attempt_id
            receipt["router_usage_id"] = (
                str(router_receipt.get("attempt_id") or "unknown")
                if isinstance(router_receipt, Mapping) else "unknown"
            )
            receipt["requested_model"] = str(body["model"])
            receipt["requested_combo_id"] = self.route.combo_id
            if isinstance(router_receipt, Mapping):
                receipt["cost_usd"] = router_receipt.get("cost_usd")
                if "fallback_used" in router_receipt:
                    receipt["fallback_used"] = router_receipt.get("fallback_used")
            if self.route.execution_mode == "single_model":
                model_prov, _, model_suf = self.route.model_id.partition("/")
                expected_upstream = {
                    self.route.model_id,
                    f"gemini-cli/{model_suf}" if model_prov == "gc" else "",
                    f"kiro/{model_suf}" if model_prov == "kr" else "",
                    f"qoder/{model_suf}" if model_prov == "qd" else "",
                    f"cloudflare-ai/{model_suf}" if model_prov == "cf" else "",
                }
                if receipt["upstream_model"] not in expected_upstream:
                    raise NineRouterConfigurationError("Upstream model differs from the single-model route")
        except Exception as exc:
            raw_router = response.get("router") if isinstance(response, Mapping) else None
            raw_usage = response.get("usage") if isinstance(response, Mapping) else None
            reconciled_receipt: dict[str, Any] = {
                "route": "9router",
                "combo_id": self.route.combo_id,
                "attempt_id": local_attempt_id,
                "requested_model": str(body["model"]),
                "upstream_provider": "unknown",
                "upstream_model": "unknown",
                "cost_status": "uncertain",
                "fallback_used": None,
            }
            if isinstance(raw_router, Mapping):
                reconciled_receipt.update(
                    {
                        "router_attempt_id": str(raw_router.get("attempt_id") or ""),
                        "upstream_provider": str(raw_router.get("upstream_provider") or "unknown"),
                        "upstream_model": str(raw_router.get("upstream_model") or "unknown"),
                        "cost_status": str(raw_router.get("cost_status") or "uncertain"),
                        "cost_usd": raw_router.get("cost_usd"),
                        "fallback_used": raw_router.get("fallback_used"),
                    }
                )
            if isinstance(raw_usage, Mapping):
                reconciled_receipt.update(
                    {
                        "prompt_tokens": raw_usage.get("prompt_tokens"),
                        "completion_tokens": raw_usage.get("completion_tokens"),
                        "total_tokens": raw_usage.get("total_tokens"),
                    }
                )
            transport_trace = response.get("__vf_transport_trace") if isinstance(response, Mapping) else None
            if isinstance(transport_trace, Mapping):
                reconciled_receipt.update(dict(transport_trace))
            if isinstance(exc, NineRouterConfigurationError):
                reconciled_receipt["validation_error"] = str(exc)[:500]
            raise NineRouterSubmissionUncertain(
                "9Router returned an unverified outcome; reconciliation is required before retry: "
                f"{type(exc).__name__}: {str(exc)[:500]}",
                route_receipt=reconciled_receipt,
            ) from exc
        return response, receipt

    def _validate_creative_receipt(self, response: Mapping[str, Any]) -> dict[str, Any]:
        """Keep route metadata auditable without making it a creative-content gate.

        The creative brief is accepted later only after its response content parses
        as JSON and passes the creative schema and policy validator. Missing route
        metadata is recorded as unknown; contradictory metadata remains fail-closed.
        """
        router = response.get("router")
        router = router if isinstance(router, Mapping) else {}
        requested_combo_id = str(router.get("requested_combo_id") or "").strip()
        if requested_combo_id and requested_combo_id != self.route.combo_id:
            raise NineRouterConfigurationError(
                "9Router creative response confirms a different combo_id"
            )

        upstream_model = str(router.get("upstream_model") or "").strip()
        expected_members = tuple(getattr(self.route, "expected_members", ()))
        if upstream_model and expected_members and upstream_model not in expected_members:
            raise NineRouterConfigurationError(
                "9Router creative response model is not a member of the requested combo"
            )

        usage = response.get("usage")
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        total_tokens: int | None = None
        if isinstance(usage, Mapping):
            raw_counts = [usage.get(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")]
            if all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in raw_counts):
                prompt_tokens, completion_tokens, total_tokens = (int(value) for value in raw_counts)
                if prompt_tokens + completion_tokens != total_tokens:
                    prompt_tokens = completion_tokens = total_tokens = None

        cost = router.get("cost_usd")
        cost_status = str(router.get("cost_status") or "unknown").strip().lower()
        cost_is_recorded = (
            cost_status in {"recorded", "charged", "consumed"}
            and isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and cost >= 0
        )
        trace = response.get("__vf_transport_trace")
        trace = trace if isinstance(trace, Mapping) else {}
        canonical_response = {
            key: value for key, value in response.items() if key != "__vf_transport_trace"
        }
        try:
            response_hash = str(trace.get("response_sha256") or hashlib.sha256(
                json.dumps(canonical_response, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest())
        except (TypeError, ValueError):
            response_hash = "unknown"

        return {
            "route": "9router",
            "combo_id": self.route.combo_id,
            "requested_combo_id": requested_combo_id or "unknown",
            "router_attempt_id": str(router.get("attempt_id") or "unknown"),
            "upstream_provider": str(router.get("upstream_provider") or "unknown"),
            "upstream_model": upstream_model or "unknown",
            "fallback_used": router.get("fallback_used") if isinstance(router.get("fallback_used"), bool) else "unknown",
            "cost_status": cost_status if cost_is_recorded else "unknown",
            "cost_usd": float(cost) if cost_is_recorded else None,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "token_usage_status": "recorded" if total_tokens is not None else "unknown",
            "response_received": True,
            "http_status": trace.get("http_status", "unknown"),
            "response_bytes": trace.get("response_bytes", "unknown"),
            "response_sha256": response_hash,
            "response_id": str(response.get("id") or "unknown"),
        }

    def _validate_receipt(self, response: Mapping[str, Any]) -> dict[str, Any]:
        router = response.get("router")
        if not isinstance(router, Mapping):
            raise NineRouterConfigurationError("9Router response is missing the required route receipt")
        if str(router.get("requested_combo_id") or "") != self.route.combo_id:
            raise NineRouterConfigurationError("9Router response did not confirm the requested combo_id")
        fallback_used = router.get("fallback_used")
        upstream_model = str(router.get("upstream_model") or "")
        if self.route.execution_mode == "single_model":
            model_provider, _, model_suffix = self.route.model_id.partition("/")
            expected_aliases = {
                self.route.model_id,
                f"gemini-cli/{model_suffix}" if model_provider == "gc" else "",
                f"kiro/{model_suffix}" if model_provider == "kr" else "",
                f"qoder/{model_suffix}" if model_provider == "qd" else "",
                f"cloudflare-ai/{model_suffix}" if model_provider == "cf" else "",
            }
            if upstream_model not in expected_aliases:
                raise NineRouterConfigurationError(f"9Router response differs from the selected single-model route: expected {self.route.model_id}, got {upstream_model}")
            if fallback_used is not False:
                raise NineRouterConfigurationError("9Router did not prove fallback was disabled for the single-model route")
        else:
            expected_members = tuple(getattr(self.route, "expected_members", ()))
            if not expected_members or upstream_model not in expected_members or not isinstance(fallback_used, bool):
                raise NineRouterConfigurationError(
                    "9Router combo response does not prove its actual member and fallback outcome"
                )
        required = ("attempt_id", "upstream_provider", "upstream_model")
        if any(not str(router.get(field) or "").strip() for field in required):
            raise NineRouterConfigurationError(
                "9Router route receipt is missing attempt ID, upstream provider, or upstream model"
            )
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            raise NineRouterConfigurationError("9Router route receipt is missing token usage")
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if not isinstance(usage.get(field), int):
                raise NineRouterConfigurationError(f"9Router route receipt has invalid {field}")
        if int(usage["prompt_tokens"]) + int(usage["completion_tokens"]) != int(usage["total_tokens"]):
            raise NineRouterConfigurationError("9Router response token totals do not reconcile")
        cost = router.get("cost_usd")
        raw_cost_status = str(router.get("cost_status") or "unknown").lower()
        cost_is_recorded = (
            raw_cost_status in {"recorded", "charged", "consumed"}
            and not isinstance(cost, bool)
            and isinstance(cost, (int, float))
            and cost >= 0
        )
        return {
            "route": "9router",
            "combo_id": self.route.combo_id,
            "attempt_id": str(router["attempt_id"]),
            "upstream_provider": str(router["upstream_provider"]),
            "upstream_model": upstream_model,
            "cost_status": raw_cost_status if cost_is_recorded else "unknown",
            "cost_usd": float(cost) if cost_is_recorded else None,
            "prompt_tokens": int(usage["prompt_tokens"]),
            "completion_tokens": int(usage["completion_tokens"]),
            "total_tokens": int(usage["total_tokens"]),
        }


class NineRouterVisionEvaluator:
    """Stage-specific vision evaluator; it cannot use a text-only route."""

    evaluator_name = "nine_router_vision_qc"
    evaluator_version = "1.0"
    fake_mode = False

    def __init__(self, client: NineRouterOpenAIClient, *, stage: str = "image") -> None:
        self.client = client
        self.stage = stage
        self.model = client.route.model_id if client.route.execution_mode == "single_model" else client.route.combo_name

    def configured_preflight(self) -> str | None:
        return self.client.configured_preflight()

    @staticmethod
    def _response_schema(stage: str) -> dict[str, Any]:
        if stage == "image":
            components = {
                "identity_score": {"type": "integer", "minimum": 0, "maximum": 60},
                "clarity_score": {"type": "integer", "minimum": 0, "maximum": 25},
                "usability_score": {"type": "integer", "minimum": 0, "maximum": 15},
            }
        else:
            components = {
                "identity_consistency": {"type": "integer", "minimum": 0, "maximum": 50},
                "scene_adherence": {"type": "integer", "minimum": 0, "maximum": 30},
                "visual_cleanliness": {"type": "integer", "minimum": 0, "maximum": 20},
            }
        properties: dict[str, Any] = {
            **components,
            "total_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "verdict": {"type": "string", "enum": ["pass", "fail", "unknown"]},
            "hard_failure": {"type": "boolean"},
            "reason": {"type": "string"},
        }
        if stage == "image":
            properties.update({
                "eligible": {"type": "boolean"},
                "source_branding_detected": {"type": "boolean"},
                "identity_mismatch": {"type": "boolean"},
                "view_angle": {"type": "string"},
                "shot_type": {"type": "string"},
                "visible_details": {"type": "array", "items": {"type": "string"}},
                "scene_suitability": {"type": "array", "items": {"type": "string"}},
                "risk_flags": {"type": "array", "items": {"type": "string"}},
                "text_coverage_percent": {"type": "integer", "minimum": 0, "maximum": 100},
            })
        elif stage == "keyframe":
            properties.update({
                "black_bars_detected": {"type": "boolean"},
                "prompt_reference_consistent": {"type": "boolean"},
                "script_adherence": {"type": "boolean"},
                "important_details_preserved": {"type": "boolean"},
                "composition_usable": {"type": "boolean"},
                "animation_suitable": {"type": "boolean"},
                "recommended_action": {"type": "string"},
            })
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    def evaluate_image(self, image_path: str | Path, prompt: str, system_instruction: str | None = None) -> dict[str, Any]:
        return self._evaluate([image_path], prompt, system_instruction, multi_image=False)

    def evaluate_multiple_images(self, image_paths: list[str | Path], prompt: str, system_instruction: str | None = None) -> dict[str, Any]:
        return self._evaluate(image_paths, prompt, system_instruction, multi_image=True)

    def _evaluate(self, image_paths: list[str | Path], prompt: str, system_instruction: str | None, *, multi_image: bool) -> dict[str, Any]:
        schema_dict = self._response_schema(self.stage)
        schema_text = (
            f"\n\nCRITICAL REQUIREMENT: Return ONLY a valid JSON object conforming strictly to this JSON schema. "
            f"Do NOT return any markdown, backticks, or text outside the JSON object.\n"
            f"JSON Schema:\n{json.dumps(schema_dict, indent=2)}"
        )
        effective_prompt = prompt if "JSON Schema:" in prompt else prompt + schema_text
        content: list[dict[str, Any]] = [{"type": "text", "text": effective_prompt}]
        for image_path in image_paths:
            mime, payload = encode_image_to_base64(image_path)
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{payload}"}})
        messages: list[dict[str, Any]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        else:
            messages.append({"role": "system", "content": "You are a professional QC evaluator. You must return only a valid JSON object matching the requested schema."})
        messages.append({"role": "user", "content": content})
        response, receipt = self.client.complete(
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": f"{self.stage}_qc",
                    "strict": True,
                    "schema": schema_dict,
                },
            },
            max_tokens=4096,
        )
        from video_factory.qc.evaluator import MultimodalEvaluator

        try:
            result = MultimodalEvaluator._parse_completion(response)
            from video_factory.qc.evaluator import validate_qc_response
            from video_factory.qc.models import QCStage
            qc_stage = {
                "image": QCStage.IMAGE,
                "keyframe": QCStage.KEYFRAME,
                "video": QCStage.VIDEO,
            }.get(self.stage, QCStage.IMAGE)
            validate_qc_response(result, qc_stage)
        except Exception as exc:
            raise NineRouterResponseSchemaError(
                f"9Router returned a response that failed {self.stage} QC schema validation: "
                f"{type(exc).__name__}: {getattr(exc, 'parse_error', str(exc))}; "
                f"finish_reason={getattr(exc, 'finish_reason', None)!r}",
                route_receipt={**receipt, "cost_status": receipt.get("cost_status", "unknown")},
            ) from exc
        result["route_receipt"] = receipt
        result["usage_status"] = "consumed"
        return result


def configured_qc_evaluator(
    *,
    stage: str = "image",
    route: NineRouterRoute | None = None,
    transport: Callable[[urllib.request.Request, float], dict[str, Any]] | None = None,
) -> Any:
    """Return the default Vertex evaluator unless the audited 9Router flag is valid."""
    if route is None:
        route = _route_from_config("vision_qc")
    if route is None:
        from video_factory.qc.evaluator import MultimodalEvaluator

        return MultimodalEvaluator()
    # Vision QC can take longer than the default client timeout. A request may
    # finish successfully upstream after the caller times out, leaving a paid
    # outcome with no usable QC response to persist. Keep the wait above the
    # observed ~134-second window from attempt creation to its usage entry.
    return NineRouterVisionEvaluator(
        NineRouterOpenAIClient(
            route,
            api_key=os.environ.get("NINE_ROUTER_API_KEY"),
            timeout=180.0,
            transport=transport,
        ),
        stage=stage,
    )


def configured_creative_model() -> Any | None:
    """Return the Story-Script-Agent model only after its route contract passes."""
    route = _route_from_config("creative")
    if route is None:
        return None
    from video_factory.creative.model_adapter import NineRouterHermesStructuredCreativeModel

    return NineRouterHermesStructuredCreativeModel(
        NineRouterOpenAIClient(route, api_key=os.environ.get("NINE_ROUTER_API_KEY"), timeout=180.0)
    )


__all__ = [
    "NineRouterConfigurationError",
    "NineRouterOpenAIClient",
    "NineRouterRoute",
    "NineRouterSubmissionUncertain",
    "NineRouterVisionEvaluator",
    "configured_qc_evaluator",
    "configured_creative_model",
]
