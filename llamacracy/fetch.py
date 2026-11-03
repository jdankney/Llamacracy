# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch a public web page for a model, and nothing else.

A model choosing URLs is a server-side request forgery risk by construction:
the URL can come from the model, from a search result, or from text a hostile
page planted, and the server fetching it sits inside a home network next to
routers, NAS boxes and admin panels. So every hop is checked:

  - http(s) only, no credentials in the URL;
  - the host is resolved here, and every address it resolves to must be a
    public unicast address (no loopback, private, link-local, CGNAT, multicast
    or reserved ranges, v4 or v6);
  - the connection then goes to exactly that checked address (the URL is
    rewritten to the IP; Host and TLS SNI keep the name, and the certificate
    is still verified against the name), so DNS can't answer differently the
    second time it is asked;
  - redirects are followed by hand, each hop checked the same way;
  - no proxy from the environment, a size cap on the body, a timeout on all of it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; Llamacracy reader; +https://github.com/jdankney/Llamacracy)"


class FetchError(Exception):
    """Why a page couldn't be read, phrased for the model (and the UI)."""


@dataclass
class Fetched:
    url: str                 # after redirects
    content_type: str
    charset: str | None
    body: bytes
    truncated: bool

    def text(self) -> str:
        try:
            return self.body.decode(self.charset or "utf-8", errors="replace")
        except LookupError:          # a charset Python has never heard of
            return self.body.decode("utf-8", errors="replace")


_V4_COMPAT = ipaddress.IPv6Network("::/96")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv6 forms that carry an IPv4 address and can reach it (mapped,
        # the old IPv4-compatible ::a.b.c.d, NAT64, 6to4): judge the IPv4 one.
        # Python calls some of these global, whatever the address inside.
        n = int(ip)
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in _V4_COMPAT or ip in _NAT64:
            ip = ipaddress.IPv4Address(n & 0xFFFFFFFF)
        elif ip.sixtofour:
            ip = ip.sixtofour
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str, port: int) -> str:
    """One public address for host, or FetchError. All of them must be public:
    a name that also points inward is refused outright."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addrs = [literal]
    else:
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), 5)
        except (OSError, TimeoutError):
            raise FetchError(f"couldn't find the site {host}") from None
        addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    if not addrs:
        raise FetchError(f"couldn't find the site {host}")
    if not all(_public(a) for a in addrs):
        raise FetchError(f"{host} is not a public website")
    return str(addrs[0])


def _check_url(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("only http and https links can be read")
    if parts.username or parts.password:
        raise FetchError("links with a username or password can't be read")
    host = parts.hostname
    if not host:
        raise FetchError("that isn't a complete link")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, host, port


async def fetch(url: str, *, max_bytes: int, timeout: float = 15.0,
                client: httpx.AsyncClient | None = None) -> Fetched:
    own = client is None
    client = client or httpx.AsyncClient(
        trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(timeout, connect=5.0),
        headers={"User-Agent": USER_AGENT,
                 "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,application/pdf;q=0.8,*/*;q=0.5"})
    try:
        return await asyncio.wait_for(_fetch(client, url, max_bytes), timeout + 5)
    except TimeoutError:
        raise FetchError("the site took too long to answer") from None
    finally:
        if own:
            await client.aclose()


async def _fetch(client: httpx.AsyncClient, url: str, max_bytes: int) -> Fetched:
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port = _check_url(url)
        ip = await _resolve(host, port)
        parts = urlsplit(url)
        ip_host = f"[{ip}]" if ":" in ip else ip
        netloc = ip_host if parts.port is None else f"{ip_host}:{parts.port}"
        pinned = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
        req = client.build_request("GET", pinned, headers={"Host": parts.netloc},
                                   extensions={"sni_hostname": host})
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            raise FetchError(f"couldn't connect to {host} ({type(e).__name__})") from None
        try:
            if resp.is_redirect:
                loc = resp.headers.get("location")
                if not loc:
                    raise FetchError("the site sent a redirect with nowhere to go")
                url = urljoin(url, loc)
                continue
            if resp.status_code >= 400:
                raise FetchError(f"the site answered {resp.status_code}")
            body = bytearray()
            truncated = False
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    del body[max_bytes:]
                    truncated = True
                    break
            ctype = resp.headers.get("content-type", "")
            charset = None
            for param in ctype.split(";")[1:]:
                k, _, v = param.partition("=")
                if k.strip().lower() == "charset":
                    charset = v.strip().strip('"') or None
            return Fetched(url, ctype.split(";")[0].strip().lower(), charset, bytes(body), truncated)
        finally:
            await resp.aclose()
    raise FetchError("too many redirects")
