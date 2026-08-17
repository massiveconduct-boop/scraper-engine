# browser/_botasaurus_extension.py
"""Round 60 — Driver(extensions=[...]) (installed botasaurus_driver 4.0.100,
driver.py:2074) does not accept raw path strings; core/config.py's
create_extensions_string() calls `.load(with_command_line_option=False)` on
each list item to build the `--load-extension=` flag. No such loader ships
in the installed package for a plain local unpacked-extension directory
(only a documented-but-unimplemented Capsolver example exists), so this
wraps one directory path in the object shape Driver expects.
"""

from __future__ import annotations

from pathlib import Path


class LocalExtension:
    __slots__ = ("path",)

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self, with_command_line_option: bool = False) -> str:
        return str(Path(self.path).resolve())
