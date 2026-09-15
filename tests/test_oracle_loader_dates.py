"""The Oracle loader's date parser, lifted out of its heredoc and tested.

`scripts/fgdb_to_oracle.sh` carries its loader as a shell heredoc, so neither ruff
nor pytest sees it, and `bash -n` is the only check the loaders otherwise get. That
holds for the parts that just talk to Oracle. `parse_dt` is different: it is pure
logic whose failure mode is a *silently NULL column* rather than an error, and it
has produced one twice — once by not handling the `+00` offset ogr2ogr writes, once
by reading the `-26` of a date-only `2024-07-26` as that offset. Both loaded clean,
counted the right number of rows, and exited 0.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

LOADER = Path(__file__).resolve().parent.parent / "scripts" / "fgdb_to_oracle.sh"
HEREDOC = re.compile(r"^cat > \"\$STAGE/loader\.py\" <<'PYEOF'\n(.*?)^PYEOF$", re.S | re.M)


@pytest.fixture(scope="module")
def loader(monkeypatch_module: pytest.MonkeyPatch) -> dict:
    """Exec the heredoc'd loader and hand back its namespace."""
    match = HEREDOC.search(LOADER.read_text(encoding="utf-8"))
    assert match, f"loader.py heredoc not found in {LOADER} — did the markers change?"
    # The module reads its configuration and imports the driver at import time;
    # neither is needed to exercise the parsing.
    source = match.group(1).replace("import oracledb", "oracledb = None")
    for var in ("STAGE_DIR", "ORA_USER", "ORA_PASSWORD", "ORA_DSN"):
        monkeypatch_module.setenv(var, "unused")
    namespace: dict = {"__name__": "fgdb_to_oracle_loader"}
    exec(compile(source, str(LOADER), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # What ogr2ogr actually writes for a GDB datetime.
        ("2024/07/26 12:07:38+00", datetime(2024, 7, 26, 12, 7, 38)),
        ("2025/11/21 00:00:00+00", datetime(2025, 11, 21, 0, 0, 0)),
        # Offsets fold into UTC, in both directions and in every spelling.
        ("2024/07/26 12:07:38+02:00", datetime(2024, 7, 26, 10, 7, 38)),
        ("2024/07/26 12:07:38-0530", datetime(2024, 7, 26, 17, 37, 38)),
        ("2024-07-26T12:07:38Z", datetime(2024, 7, 26, 12, 7, 38)),
        ("2024/07/26 12:07:38 +01:00", datetime(2024, 7, 26, 11, 7, 38)),
        # Date-only values keep their day. The ISO one is the regression: a
        # trailing '-26' is a day of month, not a UTC offset.
        ("2024-07-26", datetime(2024, 7, 26)),
        ("2024/07/26", datetime(2024, 7, 26)),
        # No offset at all.
        ("2024-07-26 12:07:38", datetime(2024, 7, 26, 12, 7, 38)),
    ],
)
def test_parse_dt_reads_the_formats_ogr_emits(loader: dict, raw: str, expected: datetime) -> None:
    assert loader["parse_dt"](raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "garbage",
        "2024/07/26 12:07:38+99:99",  # timedelta would happily normalise this
        "2024/07/26 12:07:38+15:00",  # past the real UTC offset range
    ],
)
def test_parse_dt_refuses_what_it_cannot_read(loader: dict, raw: str) -> None:
    """Unreadable is None — which `cast` counts and reports, rather than guessing."""
    assert loader["parse_dt"](raw) is None


def test_cast_counts_the_dates_it_could_not_parse(loader: dict) -> None:
    """The counter is what makes an unknown format visible instead of silent."""
    before = loader["BAD_DATES"]
    assert loader["cast"]("2024/07/26 12:07:38+00", "DateTime") == datetime(2024, 7, 26, 12, 7, 38)
    assert loader["cast"]("not a date", "DateTime") is None
    assert loader["cast"]("", "DateTime") is None  # empty is absent, not unparseable
    # Only the unreadable value is counted; the good one and the empty one are not.
    assert loader["BAD_DATES"] == before + 1
