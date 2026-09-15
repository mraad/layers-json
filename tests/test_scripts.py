"""The loaders live in bash heredocs ruff and pytest cannot see. ``bash -n`` is
the syntax check; the path-normalization cases are the ones that already bit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
LOADERS = ("fgdb_to_duckdb.sh", "fgdb_to_postgis.sh", "fgdb_to_oracle.sh")


@pytest.mark.parametrize("name", LOADERS)
def test_loader_is_valid_bash(name: str) -> None:
    subprocess.run(["bash", "-n", str(SCRIPTS / name)], check=True)


@pytest.mark.parametrize("name", LOADERS)
def test_trailing_slashes_do_not_land_inside_the_gdb(tmp_path: Path, name: str) -> None:
    # `dirname NorthSea.gdb/` is NorthSea.gdb itself, so Layers.json would
    # resolve inside the folder and PG_DB would stem to empty. The scripts
    # strip every trailing slash before either lookup.
    missing = tmp_path / "NorthSea.gdb"
    env = os.environ.copy()
    env.pop("LAYERS", None)
    env["ORA_PASSWORD"] = "x"
    env["LOG_DIR"] = str(tmp_path / "logs")
    result = subprocess.run(
        ["bash", str(SCRIPTS / name), str(missing) + "//"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert "GDB not found:" in text
    assert "NorthSea.gdb//" not in text
    assert str(missing) in text
