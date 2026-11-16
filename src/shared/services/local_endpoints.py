"""Self-hosted OpenAI-compatible endpoints (vLLM, Ollama, LM Studio, llama.cpp server).

A ``local`` connection with a ``base_url`` is a real model server reached through its
``/v1`` chat completions API; a ``local`` connection without one stays the offline stub.

On a hosted, multi-tenant deployment the URL comes from a customer, so the gateway must not
become a proxy into its own network (SSRF). Unless private addresses are allowed (a self-hosted
deployment next to its own model servers, ``GATEWAY_LOCAL_ENDPOINTS_ALLOW_PRIVATE``), an endpoint
must be HTTPS and every address its host resolves to must be public. The check runs when the
connection is saved and again before each call, so a DNS change cannot point it inward later.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


class LocalEndpointError(ValueError):
    """The base URL cannot be used as a local endpoint; the message is shown to the user."""


def normalize_local_base_url(base_url: str, *, allow_private: bool = True) -> str:
    """'http://localhost:11434' -> 'http://localhost:11434/v1'; an explicit path is kept as given.

    Checks the URL's shape only; a host name is resolved by ``ensure_endpoint_reachable``.
    """
    parts = urlsplit(base_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise LocalEndpointError("A local endpoint needs an http(s) URL with a host, such as http://localhost:8000/v1.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise LocalEndpointError("A local endpoint URL cannot carry credentials, a query or a fragment.")
    if not allow_private:
        if parts.scheme != "https":
            raise LocalEndpointError("The endpoint must use HTTPS.")
        try:
            _check_public(ipaddress.ip_address(parts.hostname))
        except ValueError as exc:
            if isinstance(exc, LocalEndpointError):
                raise
            # A host name, not an IP literal: checked when it is resolved.
            if parts.hostname.lower() in {"localhost", "localhost.localdomain"} or parts.hostname.endswith(
                (".localhost", ".local", ".internal")
            ):
                raise LocalEndpointError("The endpoint must be reachable on the public internet.") from None
    path = parts.path.rstrip("/") or "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


async def ensure_endpoint_reachable(base_url: str, *, allow_private: bool) -> None:
    """Resolve the endpoint's host and refuse it when any address is not public."""
    if allow_private:
        return
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise LocalEndpointError(f"The endpoint host {host!r} could not be resolved.") from exc
    if not infos:
        raise LocalEndpointError(f"The endpoint host {host!r} could not be resolved.")
    for info in infos:
        _check_public(ipaddress.ip_address(info[4][0]))


def _check_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if not address.is_global or address.is_multicast:
        raise LocalEndpointError("The endpoint must be reachable on the public internet.")


def is_local_endpoint(provider: object) -> bool:
    return getattr(provider, "kind", None) == "local" and bool(getattr(provider, "base_url", None))
