"""Unit coverage for the OKF bundle author.

The reading half is ``layers_from_aprx``'s and is covered there; what is new here
is the *writing*, so these build toolbox ``Layer`` objects directly (no .aprx, no
GDB, no GDAL) and assert the bundle comes out conformant with OKF v0.2 §11: every
concept parses as YAML frontmatter carrying a non-empty ``type``, and ``index.md``
carries frontmatter only for ``okf_version``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from layers_json import catalog
from layers_json import okf_from_aprx as okf

yaml = pytest.importorskip("yaml", reason="frontmatter parsing check needs PyYAML")


@pytest.fixture(scope="module")
def toolbox():
    return catalog


@pytest.fixture
def layers(toolbox):
    """One feature layer with a domain, a hint and a pipe in a value; one table."""
    wells = toolbox.Layer(
        name="Deep Wells",
        table_name="Deep_Wells",
        uri="/data/NorthSea.gdb/Wellbores",
        alias="wellbores",
        stype="Point",
        display="NAME",
        subtype="STATUS",
        hints=["Offshore wellbores.\nOne row per bore."],
        columns=[
            toolbox.Column(
                name="STATUS",
                alias="status",
                dtype="Integer",
                keyval={"1": "Active", "2": "Idle"},
                hints=["Use 'STATUS=1' for 'active'."],
                values=["1", "2"],
            ),
            toolbox.Column(
                name="NAME",
                alias="name",
                dtype="String",
                values=["A|B", "C"],
            ),
        ],
    )
    tops = toolbox.Layer(
        name="Tops",
        table_name="Tops",
        uri="/data/NorthSea.gdb/Tops",
        alias="tops",
        stype="Table",
        columns=[toolbox.Column(name="DEPTH", alias="depth", dtype="Double", minmax=[0, 5000])],
    )
    return [wells, tops]


def _split(text: str) -> tuple[dict, str]:
    """Return (frontmatter, body) for an OKF document."""
    assert text.startswith("---\n")
    _, block, body = text.split("---\n", 2)
    return yaml.safe_load(block) or {}, body


def test_concept_names_the_store_the_uri_points_at(toolbox) -> None:
    # A --pg-table layer reaches this writer too, and claiming a File GDB there
    # would cite a store that does not exist.
    pg = toolbox.Layer(
        name="Sites",
        table_name="Sites",
        uri="localhost:5433/nsgdb/ns.sites",
        alias="sites",
        stype="Polygon",
        display="site_name",
        columns=[toolbox.Column(name="site_name", alias="site name", dtype="String")],
    )

    text = okf.concept(pg, aprx=Path("/p/NorthSea.aprx"), generated={"by": "x", "at": "y"})
    front = yaml.safe_load(text.split("---")[1])

    assert front["tags"] == ["arcgis", "postgis", "polygon"]
    assert front["sources"][0]["title"] == "PostGIS database nsgdb"
    assert "come from the database;" in text
    assert "[^gdb]: Database" in text


def test_concept_still_cites_a_file_gdb_by_name(toolbox, layers) -> None:
    text = okf.concept(layers[0], aprx=Path("/p/NS.aprx"), generated={"by": "x", "at": "y"})
    front = yaml.safe_load(text.split("---")[1])

    assert front["tags"] == ["arcgis", "filegdb", "point"]
    assert front["sources"][0]["title"] == "File GDB NorthSea.gdb"
    # Not "File Gdb" — the footnote label is not title-cased.
    assert "[^gdb]: File GDB" in text


def test_bundle_is_conformant(tmp_path: Path, layers) -> None:
    at = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)

    written = okf.write_bundle(layers, tmp_path, aprx=Path("/data/NorthSea.aprx"), at=at)

    assert [p.name for p in written] == ["index.md", "Deep_Wells.md", "Tops.md"]
    meta, body = _split((tmp_path / "Deep_Wells.md").read_text(encoding="UTF-8"))
    # §11.2: type is the one always-required key.
    assert meta["type"] == "ArcGIS Feature Layer"
    assert meta["title"] == "Deep Wells"
    assert meta["table_name"] == "Deep_Wells"
    assert meta["generated"] == {"by": okf.producer(), "at": "2026-08-11T12:00:00Z"}
    # The summary is one line in `description` and intact in the body.
    assert "\n" not in meta["description"]
    assert "One row per bore." in body
    # A pipe in a sampled value must not break the schema table.
    assert "| `A\\|B`" in body
    assert "| `STATUS` | status | Integer |" in body
    assert "| `1` | Active |" in body  # decoded domain
    assert "Use 'STATUS=1' for 'active'." in body

    table_meta, table_body = _split((tmp_path / "Tops.md").read_text(encoding="UTF-8"))
    assert table_meta["type"] == "ArcGIS Table"
    # Empty optionals are dropped, not emitted blank: absence carries meaning in OKF.
    assert "subtype" not in table_meta and "display" not in table_meta
    assert "range 0 to 5000" in table_body


def test_a_layer_without_a_table_name_cannot_name_a_file(tmp_path: Path, toolbox) -> None:
    # A layer name is free text; "../elsewhere" is one. There is no fallback.
    rogue = toolbox.Layer(
        name="../elsewhere",
        uri="/data/NorthSea.gdb/X",
        alias="x",
        stype="Point",
        columns=[toolbox.Column(name="A", alias="a", dtype="String", values=["v"])],
    )

    with pytest.raises(ValueError, match="no table_name"):
        okf.write_bundle([rogue], tmp_path, aprx=Path("/data/NorthSea.aprx"))

    assert not (tmp_path.parent / "elsewhere.md").exists()


def test_a_narrower_rerun_warns_about_the_concepts_it_no_longer_lists(
    tmp_path: Path, layers, capsys
) -> None:
    okf.write_bundle(layers, tmp_path, aprx=Path("/data/NorthSea.aprx"))

    okf.write_bundle(layers[:1], tmp_path, aprx=Path("/data/NorthSea.aprx"))

    # Warned, never deleted: -o is a directory the user chose.
    assert "Tops.md" in capsys.readouterr().err
    assert (tmp_path / "Tops.md").exists()


def test_index_lists_every_concept(tmp_path: Path, layers) -> None:
    okf.write_bundle(layers, tmp_path, aprx=Path("/data/NorthSea.aprx"))

    meta, body = _split((tmp_path / "index.md").read_text(encoding="UTF-8"))
    # §8/§12: the bundle-root index carries okf_version and nothing else.
    assert meta == {"okf_version": okf.OKF_VERSION}
    assert "* [Deep Wells](Deep_Wells.md) - Offshore wellbores. One row per bore." in body
    assert "* [Tops](Tops.md) - ArcGIS table 'tops' with 1 described columns." in body
    assert "# Feature Layers" in body and "# Tables" in body


def test_path_fields_are_followable_uris_not_bundle_relative_paths(tmp_path: Path, toolbox) -> None:
    """§6.2 reads a leading ``/`` as bundle-relative, so a local path goes out as file://."""
    gdb = tmp_path / "NorthSea.gdb"
    gdb.mkdir()
    aprx = tmp_path / "NorthSea.aprx"
    aprx.write_text("", encoding="UTF-8")
    layer = toolbox.Layer(
        name="Wells",
        table_name="Wells",
        uri=str(gdb / "Wellbores"),
        alias="wells",
        stype="Point",
        columns=[toolbox.Column(name="A", alias="a", dtype="String", values=["v"])],
    )

    front = yaml.safe_load(
        okf.concept(layer, aprx=aprx, generated={"by": "x", "at": "y"}).split("---")[1]
    )

    assert front["resource"] == (gdb / "Wellbores").as_uri()
    assert front["resource"].startswith("file://")
    assert front["sources"][0]["resource"] == gdb.as_uri()
    assert front["sources"][1]["resource"] == aprx.as_uri()
    # §5.1: the source's own recency, distinct from generated.at.
    assert front["sources"][0]["last_modified"].endswith("Z")
    assert front["sources"][1]["last_modified"].endswith("Z")


def test_an_enterprise_workspace_label_is_left_alone_and_carries_no_mtime(toolbox) -> None:
    """``host:port/database`` is not a filesystem path — no file:// scheme, no stat."""
    layer = toolbox.Layer(
        name="Sites",
        table_name="Sites",
        uri="dbhost:5432/nsgdb/sde.sites",
        alias="sites",
        stype="Polygon",
        columns=[toolbox.Column(name="A", alias="a", dtype="String", values=["v"])],
    )

    front = yaml.safe_load(
        okf.concept(layer, aprx=Path("/p/NorthSea.aprx"), generated={"by": "x", "at": "y"}).split(
            "---"
        )[1]
    )

    assert front["resource"] == "dbhost:5432/nsgdb/sde.sites"
    assert front["sources"][0]["resource"] == "dbhost:5432/nsgdb"
    assert "last_modified" not in front["sources"][0]


def test_every_timestamp_carries_an_explicit_utc_offset() -> None:
    """§5: any offset-bearing input normalizes to the same instant, written as UTC."""
    aware = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

    assert okf.stamp(aware) == "2026-08-11T10:00:00Z"
    assert (
        okf.stamp(dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)) == "2026-08-11T12:00:00Z"
    )


def test_empty_catalog_message_goes_to_stderr(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(okf, "build_layers", lambda *_args, **_kwargs: [])
    aprx = tmp_path / "NorthSea.aprx"
    aprx.write_text("", encoding="UTF-8")

    assert okf.main([str(aprx), "-o", str(tmp_path / "bundle")]) == 1
    captured = capsys.readouterr()
    assert "Did not find any feature layers" in captured.err
    assert "Did not find any feature layers" not in captured.out


def test_a_naive_timestamp_is_refused_rather_than_read_as_local_time(
    tmp_path: Path, layers
) -> None:
    """Assuming local time would stamp a shifted instant ``Z`` — wrong, but explicit-looking."""
    naive = dt.datetime(2026, 8, 11, 12, 0)

    with pytest.raises(ValueError, match="naive"):
        okf.stamp(naive)

    with pytest.raises(ValueError, match="naive"):
        okf.write_bundle(layers, tmp_path, aprx=Path("/data/NorthSea.aprx"), at=naive)
