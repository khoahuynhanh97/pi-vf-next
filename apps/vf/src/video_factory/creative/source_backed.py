"""Fail-closed checks for Catalog v2 SOURCE_BACKED claims."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import http.client
import ipaddress
import socket
import ssl
from typing import Any
from urllib.parse import urljoin, urlparse


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection whose TCP peer is a previously validated IP address."""

    def __init__(self, host: str, port: int, pinned_ip: str, *, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _ResponseWithConnection:
    def __init__(self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection) -> None:
        self.status = response.status
        self.headers = response.headers
        self._response = response
        self._connection = connection

    def read(self, limit: int) -> bytes:
        try:
            return self._response.read(limit)
        finally:
            self._connection.close()


class SourceEvidenceVerifier:
    """Check Catalog evidence, and safely fetch sources only when asked to collect them."""

    def __init__(
        self,
        record_lookup: Callable[[str], Mapping[str, Any] | None] | None = None,
        *,
        resolve_host: Callable[[str], list[str]] | None = None,
        open_url: Callable[[str, str], Any] | None = None,
        timeout: float = 5.0,
        max_bytes: int = 1_000_000,
    ) -> None:
        self._record_lookup = record_lookup
        self._resolve_host = resolve_host or self._default_resolve_host
        self._open_url = open_url or self._default_open_url
        self._timeout = timeout
        self._max_bytes = max_bytes

    @staticmethod
    def _default_resolve_host(host: str) -> list[str]:
        return sorted({item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})

    @staticmethod
    def _is_public_ip(value: str) -> bool:
        try:
            return bool(ipaddress.ip_address(value).is_global)
        except ValueError:
            return False

    def _pinned_ip(self, locator: str) -> str | None:
        parsed = urlparse(locator)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or host in {"localhost", "exchange.local", "example.com"}
            or host.endswith(".local")
            or host.endswith(".example")
        ):
            return None
        try:
            answers = self._resolve_host(host)
        except (OSError, ValueError, TypeError):
            return None
        if not answers or any(not self._is_public_ip(answer) for answer in answers):
            return None
        return sorted(answers)[0]

    def _default_open_url(self, url: str, pinned_ip: str) -> _ResponseWithConnection:
        parsed = urlparse(url)
        host = parsed.hostname
        if host is None:
            raise ValueError("URL hostname is required")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        host_header = host if parsed.port is None else f"{host}:{port}"
        if parsed.scheme == "https":
            connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
                host, port, pinned_ip, timeout=self._timeout
            )
        elif parsed.scheme == "http":
            connection = http.client.HTTPConnection(pinned_ip, port, timeout=self._timeout)
        else:
            raise ValueError("unsupported source URL scheme")
        connection.request(
            "GET",
            target,
            headers={"Host": host_header, "User-Agent": "VideoFactorySourceVerifier/1.0"},
        )
        return _ResponseWithConnection(connection.getresponse(), connection)

    def fetch_source(self, locator: str) -> str | None:
        """Fetch source text with an inspected IP pin and redirect revalidation."""
        current = locator
        for _ in range(4):
            pinned_ip = self._pinned_ip(current)
            if pinned_ip is None:
                return None
            try:
                response = self._open_url(current, pinned_ip)
            except (OSError, ValueError, TypeError, ssl.SSLError, http.client.HTTPException):
                return None
            status = int(getattr(response, "status", 0) or 0)
            location = getattr(response, "headers", {}).get("Location") if 300 <= status < 400 else None
            if location:
                current = urljoin(current, str(location))
                continue
            if not 200 <= status < 300:
                return None
            try:
                return response.read(self._max_bytes).decode("utf-8", errors="replace")
            except (OSError, ValueError, TypeError, UnicodeError):
                return None
        return None

    def verify(
        self, source_url: str, locator: str, snippet: str, record_id: str,
        *, claim_text: str = "", product_key: str = "",
    ) -> bool:
        """Accept only an independently validated Catalog v2 record with exact fields."""
        if (
            not self._pinned_ip(source_url) or not locator.strip() or len(snippet.strip()) < 8
            or not claim_text.strip() or not product_key.strip()
        ):
            return False
        if self._record_lookup is None or not record_id.strip():
            return False
        try:
            record = self._record_lookup(record_id)
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(record, Mapping):
            return False
        archive_hash = str(record.get("archive_sha256") or "").removeprefix("sha256:")
        return (
            str(record.get("validator") or "") == "validate_catalog_v2"
            and str(record.get("validation_status") or "") == "passed"
            and bool(str(record.get("snapshot_id") or "").strip())
            and len(archive_hash) == 64
            and all(char in "0123456789abcdefABCDEF" for char in archive_hash)
            and str(record.get("source_url") or "") == source_url
            and str(record.get("evidence_locator") or "") == locator
            and str(record.get("evidence_snippet") or "") == snippet
            and str(record.get("claim_text") or "") == claim_text
            and str(record.get("canonical_product_key") or "") == product_key
        )


def is_source_backed_claim(value: Mapping[str, object]) -> bool:
    """Check intake shape only; runtime additionally needs an independent record."""
    status = str(value.get("status") or "").strip().upper().replace("-", "_")
    if status != "SOURCE_BACKED":
        return False
    locator = str(value.get("source_locator") or value.get("locator") or value.get("source_url") or value.get("url") or "").strip()
    snippet = str(value.get("evidence_snippet") or value.get("snippet") or value.get("quote") or "").strip()
    parsed = urlparse(locator)
    host = (parsed.hostname or "").lower()
    return (
        len(snippet) >= 8
        and parsed.scheme in {"http", "https"}
        and bool(host)
        and host not in {"exchange.local", "example.com"}
        and not host.endswith((".local", ".example"))
    )


def source_backed_refs(value: Mapping[str, object]) -> tuple[str, ...]:
    if not is_source_backed_claim(value):
        return ()
    return (str(value.get("source_locator") or value.get("locator") or value.get("source_url") or value.get("url")).strip(),)
