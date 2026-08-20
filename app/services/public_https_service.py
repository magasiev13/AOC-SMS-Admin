from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit


REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
ALLOWED_CONTENT_TYPES = {
    "application/xhtml+xml",
    "text/html",
    "text/plain",
}


def _secure_tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class PublicHttpsFetchError(RuntimeError):
    """Raised when a URL cannot be fetched without crossing a private boundary."""


@dataclass(frozen=True)
class PublicHttpsTarget:
    url: str
    hostname: str
    port: int
    request_target: str
    resolved_addresses: tuple[str, ...]


@dataclass(frozen=True)
class PublicHttpsResponse:
    final_url: str
    status_code: int
    body: str
    redirect_count: int


def _normalized_hostname(raw_hostname: str) -> str:
    try:
        normalized = raw_hostname.encode("idna").decode("ascii").strip(".").lower()
    except UnicodeError as exc:
        raise PublicHttpsFetchError("URL hostname is not valid IDNA.") from exc
    if not normalized or len(normalized) > 253:
        raise PublicHttpsFetchError("URL hostname is missing or too long.")
    return normalized


def _public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PublicHttpsFetchError(f"Public URL hostname {hostname!r} could not be resolved.") from exc

    addresses = tuple(dict.fromkeys(str(record[4][0]) for record in records))
    if not addresses:
        raise PublicHttpsFetchError(f"Public URL hostname {hostname!r} returned no addresses.")

    unsafe_addresses: list[str] = []
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise PublicHttpsFetchError(
                f"Public URL hostname {hostname!r} resolved to an invalid address {address!r}."
            ) from exc
        if not parsed_address.is_global:
            unsafe_addresses.append(address)
    if unsafe_addresses:
        joined = ", ".join(unsafe_addresses)
        raise PublicHttpsFetchError(
            f"Public URL hostname {hostname!r} resolved to non-public address(es): {joined}."
        )
    return addresses


def resolve_public_https_target(url: str) -> PublicHttpsTarget:
    if "\r" in url or "\n" in url:
        raise PublicHttpsFetchError("Public URL contains prohibited control characters.")

    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise PublicHttpsFetchError("Public URL must use HTTPS.")
    if parsed.username or parsed.password:
        raise PublicHttpsFetchError("Public URL must not contain user information.")
    if not parsed.hostname:
        raise PublicHttpsFetchError("Public URL hostname is required.")

    hostname = _normalized_hostname(parsed.hostname)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise PublicHttpsFetchError("Public URL port is invalid.") from exc
    if port != 443:
        raise PublicHttpsFetchError("Public URL must use the standard HTTPS port 443.")

    path = parsed.path or "/"
    request_target = f"{path}?{parsed.query}" if parsed.query else path
    addresses = _public_addresses(hostname, port)
    return PublicHttpsTarget(
        url=url,
        hostname=hostname,
        port=port,
        request_target=request_target,
        resolved_addresses=addresses,
    )


def _read_response(
    target: PublicHttpsTarget,
    timeout_seconds: float,
    max_bytes: int,
    user_agent: str,
) -> tuple[int, dict[str, str], bytes]:
    last_error: Exception | None = None
    for address in target.resolved_addresses:
        raw_socket: socket.socket | None = None
        tls_socket: ssl.SSLSocket | None = None
        try:
            raw_socket = socket.create_connection((address, target.port), timeout=timeout_seconds)
            tls_socket = _secure_tls_context().wrap_socket(
                raw_socket,
                server_hostname=target.hostname,
            )
            request_bytes = (
                f"GET {target.request_target} HTTP/1.1\r\n"
                f"Host: {target.hostname}\r\n"
                f"User-Agent: {user_agent}\r\n"
                "Accept: text/html,application/xhtml+xml,text/plain\r\n"
                "Accept-Encoding: identity\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            tls_socket.sendall(request_bytes)
            response = http.client.HTTPResponse(tls_socket)
            response.begin()
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise PublicHttpsFetchError(
                    f"Public URL response exceeded the {max_bytes}-byte validation limit."
                )
            headers = {name.lower(): value for name, value in response.getheaders()}
            return int(response.status), headers, body
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            if tls_socket is not None:
                tls_socket.close()
            elif raw_socket is not None:
                raw_socket.close()

    raise PublicHttpsFetchError(
        f"Public URL {target.url!r} could not be fetched from its validated address set."
    ) from last_error


def fetch_public_https_text(
    url: str,
    timeout_seconds: float,
    max_bytes: int,
    max_redirects: int,
    user_agent: str,
) -> PublicHttpsResponse:
    current_url = url
    for redirect_count in range(max_redirects + 1):
        target = resolve_public_https_target(current_url)
        status_code, headers, body = _read_response(
            target,
            timeout_seconds,
            max_bytes,
            user_agent,
        )

        if status_code in REDIRECT_STATUS_CODES:
            location = (headers.get("location") or "").strip()
            if not location:
                raise PublicHttpsFetchError("Public URL returned a redirect without a Location header.")
            if redirect_count >= max_redirects:
                raise PublicHttpsFetchError("Public URL exceeded the redirect validation limit.")
            current_url = urljoin(current_url, location)
            continue

        content_type = (headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type and content_type not in ALLOWED_CONTENT_TYPES:
            raise PublicHttpsFetchError(
                f"Public URL returned unsupported content type {content_type!r}."
            )
        return PublicHttpsResponse(
            final_url=current_url,
            status_code=status_code,
            body=body.decode("utf-8", errors="ignore"),
            redirect_count=redirect_count,
        )

    raise PublicHttpsFetchError("Public URL validation terminated unexpectedly.")
