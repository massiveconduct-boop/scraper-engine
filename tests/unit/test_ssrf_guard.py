# tests/unit/test_ssrf_guard.py
"""SSRFGuard tests — spec §3.1."""

import ipaddress
from unittest.mock import AsyncMock, patch

import pytest

from core.exceptions import SSRFBlockedError
from core.ssrf_guard import SSRFGuard


class TestSSRFGuard:
    def test_all_denied_networks_present(self) -> None:
        guard = SSRFGuard()
        assert len(guard.DENIED_NETWORKS) == 8
        assert "127.0.0.0/8" in guard.DENIED_NETWORKS
        assert "169.254.0.0/16" in guard.DENIED_NETWORKS  # cloud metadata
        assert "10.0.0.0/8" in guard.DENIED_NETWORKS
        assert "172.16.0.0/12" in guard.DENIED_NETWORKS
        assert "192.168.0.0/16" in guard.DENIED_NETWORKS

    def test_denies_loopback(self) -> None:
        guard = SSRFGuard()
        for net in guard._denied:
            assert net.num_addresses > 0, f"Network {net} is empty"

    def test_denies_cloud_metadata(self) -> None:
        """169.254.169.254 must be blocked (AWS/GCP/Azure/OCI metadata endpoint)."""
        guard = SSRFGuard()
        addr = ipaddress.ip_address("169.254.169.254")
        denied = any(addr in net for net in guard._denied)
        assert denied, "Cloud metadata IP must be in denied networks"

    def test_allows_public_ip(self) -> None:
        guard = SSRFGuard()
        public = ipaddress.ip_address("93.184.216.34")  # example.com
        denied = any(public in net for net in guard._denied)
        assert not denied, "Public IP should not be denied"

    @pytest.mark.asyncio
    async def test_validate_rejects_private(self) -> None:
        guard = SSRFGuard()
        with patch.object(
            SSRFGuard, "_resolve_host", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = "127.0.0.1"
            with pytest.raises(SSRFBlockedError):
                await guard.validate("http://127.0.0.1:8080/admin")

    @pytest.mark.asyncio
    async def test_validate_rejects_internal(self) -> None:
        guard = SSRFGuard()
        with patch.object(
            SSRFGuard, "_resolve_host", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = "10.0.0.1"
            with pytest.raises(SSRFBlockedError):
                await guard.validate("http://10.0.0.1/api")

    @pytest.mark.asyncio
    async def test_validate_allows_public(self) -> None:
        guard = SSRFGuard()
        with patch.object(
            SSRFGuard, "_resolve_host", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = "93.184.216.34"
            # Should not raise
            await guard.validate("https://example.com/")
