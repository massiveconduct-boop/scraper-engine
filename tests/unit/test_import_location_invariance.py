# tests/unit/test_import_location_invariance.py
"""Every package (api/core/proxy/...) resolves correctly via the editable
install regardless of the process's current working directory — proven here
by running the import in a fresh subprocess with cwd set to somewhere other
than the repo root, not just asserted from within pytest's own cwd."""

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_imports_resolve_from_outside_repo_root():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import api, core, proxy; print(core.__file__)",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert result.returncode == 0, result.stderr
    resolved = Path(result.stdout.strip()).resolve()
    assert resolved == (REPO_ROOT / "core" / "__init__.py")
