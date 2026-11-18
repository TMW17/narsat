"""Batch commands run end to end as a subprocess, with the settings a deployment uses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from narsat.config import DbSettings


def test_reference_agreement_command(db_settings: DbSettings) -> None:
    """Reads the matching distance from the settings (a typo there only fails at run time)."""
    env = {**os.environ, "NARSAT_DB_DSN": db_settings.dsn.get_secret_value()}
    result = subprocess.run(
        [str(Path(sys.executable).parent / "narsat"), "reference", "agreement"],
        capture_output=True, text=True, timeout=120, check=False, env=env,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr[-2000:]
    assert "narsat:dnbr" in json.loads(result.stdout)
