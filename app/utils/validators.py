"""URL validation and SSRF protection.

Every URL the service fetches (the submitted URL, each redirect hop and every
internal sub-page) goes through ``ensure_public_url`` which:

* only allows http/https with a real hostname and the default ports
* rejects credentials embedded in the URL
* resolves the hostname and rejects it if ANY resolved address is not a
  globally routable IP (loopback, private, link-local / cloud metadata,
  multicast, reserved, CGNAT, unspecified ...)
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from app.errors import BlockedURLError, InvalidURLError

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443}
BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet")


def normalize_url(raw: str) -> str:
    """Validate syntax and return a normalized absolute URL (adds https:// if missing)."""
    raw = (raw or "").strip()
    if not raw:
        raise InvalidURLError("URL must not be empty.")
    if any(c.isspace() for c in raw):
        raise InvalidURLError("URL must not contain whitespace.")
    if "://" not in raw:
        raw = "https://" + raw

    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError(f"Malformed URL: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidURLError("Only http and https URLs are supported.")
    if parts.username or parts.password:
        raise InvalidURLError("URLs with embedded credentials are not allowed.")

    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise InvalidURLError("URL must include a hostname.")
    if port not in ALLOWED_PORTS:
        raise BlockedURLError("Only default web ports (80/443) are allowed.")

    is_ip = _parse_ip(host) is not None
    if not is_ip:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise InvalidURLError("Invalid international domain name.") from exc
        if host in BLOCKED_HOSTNAMES:
            raise BlockedURLError("Access to local or internal hostnames is not allowed.")
        if "." not in host:
            raise InvalidURLError("Hostname must be a fully qualified domain name.")
        if len(host) > 253 or any(not label or len(label) > 63 for label in host.split(".")):
            raise InvalidURLError("Hostname is not valid.")

    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


async def ensure_public_url(url: str) -> str:
    """Normalize ``url`` and verify it does not point to an internal network address."""
    url = normalize_url(url)
    host = urlsplit(url).hostname or ""

    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise BlockedURLError("Access to local or internal hostnames is not allowed.")

    literal = _parse_ip(host)
    if literal is not None:
        if not is_public_ip(literal):
            raise BlockedURLError("Access to private or internal IP addresses is not allowed.")
        return url

    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=5
        )
    except (socket.gaierror, asyncio.TimeoutError, UnicodeError) as exc:
        raise InvalidURLError(f"Domain '{host}' could not be resolved.") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise InvalidURLError(f"Domain '{host}' could not be resolved.")
    for addr in addresses:
        ip = _parse_ip(addr.split("%")[0])
        if ip is None or not is_public_ip(ip):
            raise BlockedURLError("Domain resolves to a private or internal network address.")
    return url
