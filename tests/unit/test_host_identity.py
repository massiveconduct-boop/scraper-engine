# tests/unit/test_host_identity.py
"""core/host_identity.py — which host a process is on (round 65)."""

from scraper_engine.core.host_identity import resolve_host_id


def test_env_override_wins(monkeypatch, tmp_path):
    boot = tmp_path / "boot_id"
    boot.write_text("from-kernel\n")
    monkeypatch.setenv("SCRAPER_HOST_ID", " node-7 ")
    assert resolve_host_id(boot) == "node-7"


def test_boot_id_is_used_without_override(monkeypatch, tmp_path):
    boot = tmp_path / "boot_id"
    boot.write_text("69ea20df-c3fa\n")
    monkeypatch.delenv("SCRAPER_HOST_ID", raising=False)
    assert resolve_host_id(boot) == "69ea20df-c3fa"


def test_missing_or_empty_boot_id_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRAPER_HOST_ID", "  ")
    assert resolve_host_id(tmp_path / "absent") == "local"
    empty = tmp_path / "empty"
    empty.write_text("")
    assert resolve_host_id(empty) == "local"


def test_default_path_reads_the_real_kernel_value(monkeypatch):
    monkeypatch.delenv("SCRAPER_HOST_ID", raising=False)
    assert resolve_host_id()
