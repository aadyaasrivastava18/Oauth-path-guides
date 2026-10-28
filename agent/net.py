"""Network safety helpers. Users can submit URLs, and the model can name URLs, so every
outbound request goes through assert_public_url() first (SSRF guard)."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

USER_AGENT = "oauth-path-guides/1.0 (OAuth setup research bot; read-only, no credentials)"

_SECOND_LEVEL = {"co.uk", "org.uk", "ac.uk", "com.au", "net.au", "co.in", "co.jp", "co.nz", "com.br", "com.sg", "co.za"}


class UnsafeURL(ValueError):
    pass


async def assert_public_url(url: str) -> None:
    """Raise UnsafeURL unless url is http(s) on a standard port and resolves only to public IPs."""
    try:
        p = urlparse(url)
        port = p.port
    except ValueError as exc:
        raise UnsafeURL("malformed URL") from exc
    if p.scheme not in ("http", "https"):
        raise UnsafeURL("only http(s) URLs are allowed")
    host = (p.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        raise UnsafeURL("host is not allowed")
    if p.username or p.password:
        raise UnsafeURL("credentials in URLs are not allowed")
    if port not in (None, 80, 443):
        raise UnsafeURL("non-standard port")
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM
            )
        except socket.gaierror as exc:
            raise UnsafeURL("host does not resolve") from exc
        ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    if not ips:
        raise UnsafeURL("host does not resolve")
    for ip in ips:
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL("host resolves to a non-public address")


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def registrable_domain(host: str) -> str:
    """Naive eTLD+1 (good enough for ranking; not for security decisions)."""
    labels = [l for l in host.lower().strip(".").split(".") if l]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])
