# core/ssrf_guard.py
from __future__ import annotations

import asyncio
import ipaddress
import socket

from .exceptions import SSRFBlockedError


class SSRFGuard:
    """Reject any scrape target resolving to a non-public network destination.

    Must run **before** the URL reaches Quota/enqueue, so a blocked URL
    never consumes tenant quota.

    Checked at enqueue time and after every redirect hop.
    """

    DENIED_NETWORKS: list[str] = [
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",  # cloud metadata (AWS/GCP/Azure/OCI)
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    ]

    def __init__(self) -> None:
        self._denied = [ipaddress.ip_network(n) for n in self.DENIED_NETWORKS]

    async def validate(self, url: str) -> None:
        """Raises SSRFBlockedError if ANY address the host resolves to is in a
        denied range. Checking only the first `getaddrinfo` result would let a
        multi-record DNS answer (public IP first, private IP second) slip past —
        so every candidate address is checked, not just one."""
        hosts = await self._resolve_hosts(url)
        for host in hosts:
            addr = ipaddress.ip_address(host)
            for net in self._denied:
                if addr in net:
                    raise SSRFBlockedError(
                        url=url,
                        host=host,
                        network=str(net),
                    )

    async def validate_redirect_chain(self, response: object) -> None:
        """Called by every fetcher after following redirects; re-resolves final host."""
        # Re-resolve the final URL after redirects
        final_url = str(getattr(response, "url", ""))
        await self.validate(final_url)

    @staticmethod
    async def _resolve_hosts(url: str) -> list[str]:
        """Resolve every address a host resolves to via async DNS lookup using
        getaddrinfo in an executor (not just the first record — see validate())."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        hostname = parsed.hostname
        if hostname is None:
            raise ValueError(f"Cannot extract hostname from URL: {url}")

        loop = asyncio.get_running_loop()

        def _resolve() -> list[str]:
            info = socket.getaddrinfo(hostname, None)
            addrs = {str(sockaddr[0]) for _family, _, _, _, sockaddr in info}
            if not addrs:
                raise SSRFBlockedError(
                    url=url, host=hostname, network="<unresolvable>"
                )
            return list(addrs)

        return await loop.run_in_executor(None, _resolve)
