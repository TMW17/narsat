"""Commands that do not touch imagery must start without the imaging stack.

The platform image once failed with every command because importing rasterio failed
(a missing system library), although `narsat db wait` has no use for it.
"""

from __future__ import annotations

import subprocess
import sys

BLOCK_IMAGING = (
    "import sys\n"
    "for name in ('rasterio', 'pystac_client', 'pyogrio'):\n"
    "    sys.modules[name] = None  # any import of these now raises ImportError\n"
    "from narsat.cli import main\n"
    "sys.argv = ['narsat', *sys.argv[1:]]\n"
    "main()\n"
)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", BLOCK_IMAGING, *args],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip


def test_version_and_help_start_without_the_imaging_stack() -> None:
    assert run("version").returncode == 0
    for group in ("db", "stream", "lake", "firms", "events", "users"):
        result = run(group, "--help")
        assert result.returncode == 0, f"{group}: {result.stderr[-2000:]}"


def test_a_configuration_error_is_one_line_without_the_imaging_stack() -> None:
    result = subprocess.run(
        [sys.executable, "-c", BLOCK_IMAGING, "firms", "check"],
        capture_output=True, text=True, timeout=120, check=False,
        env={"PATH": "/usr/bin:/bin", "NARSAT_FIRMS_MAP_KEY": ""},
    )  # fmt: skip
    assert result.returncode == 2, result.stderr[-2000:]
    assert result.stderr.startswith("configuration error: NARSAT_FIRMS_MAP_KEY is not set")


def test_compare_wants_either_one_event_or_all() -> None:
    neither = run("map", "compare")
    both = run("map", "compare", "92ef5d48-4477-5a41-bdf4-7f22ea40d786", "--all")
    assert neither.returncode == both.returncode == 2
    assert "either an event id or --all" in neither.stderr
    assert "either an event id or --all" in both.stderr
