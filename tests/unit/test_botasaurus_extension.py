# tests/unit/test_botasaurus_extension.py
"""Round 60 — browser/_botasaurus_extension.py.

Driver(extensions=[...]) (installed botasaurus_driver 4.0.100) requires each
list item to expose .load(with_command_line_option=False) -> str, not a raw
path string — LocalExtension is the shim that gives a plain configured
directory path that shape.
"""

from __future__ import annotations

from pathlib import Path

from scraper_engine.browser._botasaurus_extension import LocalExtension


class TestLocalExtension:
    def test_load_returns_resolved_absolute_path(self, tmp_path: Path):
        ext_dir = tmp_path / "my-extension"
        ext_dir.mkdir()
        ext = LocalExtension(str(ext_dir))
        assert ext.load(with_command_line_option=False) == str(ext_dir.resolve())

    def test_load_resolves_relative_path(self):
        ext = LocalExtension("relative/dir")
        assert ext.load(with_command_line_option=False) == str(Path("relative/dir").resolve())
