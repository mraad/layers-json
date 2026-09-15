#!/usr/bin/env python3
"""Author an **OKF v0.2 knowledge bundle** from an ``.aprx`` + File GDB (or PostGIS).

Same reading as ``layers-json`` — the same ``.aprx`` CIM JSON and the same GDB via
OGR, through the same ``build_layers()`` — but written out as an
`Open Knowledge Format <https://github.com/GoogleCloudPlatform/open-knowledge-format>`_
bundle instead of ``Layers.json``: one markdown concept per layer, YAML frontmatter,
a bundle-root ``index.md``.

``Layers.json`` is for the text-to-SQL agent that queries the database; the OKF
bundle is for anything that reads *knowledge* — a catalog, an agent's context, a
human with `cat`. Both describe the same layers, so neither reimplements the
reading::

    layers-okf <project.aprx> -o <bundle-dir> [--max-values 20] [--use-ilike] ...

Output::

    <bundle-dir>/
      index.md              # okf_version: "0.2", one entry per concept
      <TableName>.md        # type: ArcGIS Feature Layer | ArcGIS Table

Requires GDAL's Python bindings (the ``fgdb`` extra), same as ``layers-json``.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import textwrap
import zipfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from layers_json.layers_from_aprx import build_layers, build_parser, check_limits, check_table_names

OKF_VERSION = "0.2"


def producer() -> str:
    """The ``<producer>/<version>`` actor this tool records as ``generated.by`` (OKF §7)."""
    try:
        return f"layers-json/{version('layers-json')}"
    except PackageNotFoundError:  # running from a source tree with no dist-info
        return "layers-json/unknown"


def stamp(when: dt.datetime) -> str:
    """OKF §5: every timestamp-valued key is an ISO 8601 datetime with an explicit offset.

    A naive input is refused rather than assumed. ``astimezone`` reads one as *local*
    time, so on any machine off UTC the result is a shifted instant still labelled
    ``Z`` — a wrong answer wearing the explicit offset §5 asks for. Both callers here
    pass aware values; ``write_bundle(at=...)`` is public, and this is its guard.
    """
    if when.tzinfo is None or when.tzinfo.utcoffset(when) is None:
        raise ValueError(f"{when!r} is naive; OKF §5 needs an explicit UTC offset.")
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def path_field(path: Path) -> str:
    """A path-valued field a consumer can actually follow (OKF §6.2).

    §6.2 reads a leading ``/`` as *bundle-relative*, so a local absolute path is
    indistinguishable from a path into the bundle — ``/data/NorthSea.gdb`` would
    resolve against the bundle root. A local workspace therefore goes out as a
    ``file://`` URL, the unambiguous third form. An enterprise workspace label
    (``host:port/database``, never a filesystem path) is left as written.
    """
    return path.as_uri() if path.is_absolute() else str(path)


def source(sid: str, path: Path, title: str) -> dict:
    """One ``sources`` entry, carrying `last_modified` when the workspace is readable.

    `last_modified` is OKF's recency signal for the *source* (§5.1), distinct from
    `generated.at`, which dates this document. An enterprise workspace label has no
    file to stat, and a GDB can move between runs — neither is an error, the signal
    is simply absent, which §5.1 permits.
    """
    entry = {"id": sid, "resource": path_field(path), "title": title}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return entry
    entry["last_modified"] = stamp(dt.datetime.fromtimestamp(mtime, dt.timezone.utc))
    return entry


# ---------------------------------------------------------------------------
# YAML frontmatter — JSON is a subset of YAML 1.2, so json.dumps is the quoter
# ---------------------------------------------------------------------------
def _scalar(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _flow_map(mapping: dict) -> str:
    """``{ key: "value", ... }`` — the form the OKF spec uses for `generated` etc."""
    return "{ " + ", ".join(f"{k}: {_scalar(v)}" for k, v in mapping.items()) + " }"


def frontmatter(pairs: list[tuple[str, object]]) -> str:
    """Render ordered key/value pairs as a YAML frontmatter block.

    Handles the three shapes OKF frontmatter actually uses: scalars and scalar
    lists (JSON), mappings (flow), and lists of mappings (block list of flow
    mappings, so `sources` stays readable). Empty values are dropped — an absent
    optional field carries meaning in OKF, an empty one does not.
    """
    lines = ["---"]
    for key, value in pairs:
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, dict):
            lines.append(f"{key}: {_flow_map(value)}")
        elif isinstance(value, list) and isinstance(value[0], dict):
            lines.append(f"{key}:")
            lines += [f"  - {_flow_map(entry)}" for entry in value]
        else:
            lines.append(f"{key}: {_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Concept documents
# ---------------------------------------------------------------------------
def _cell(text: str) -> str:
    """Make a string safe inside a markdown table cell or a link label.

    Backslash goes first: escaping it after the pipe would turn the ``\\|`` this
    just inserted back into an escaped backslash followed by a live cell break.
    Brackets are escaped for the link labels ``index()`` builds from layer names.
    """
    out = str(text).replace("\\", "\\\\")
    for char in "|[]":
        out = out.replace(char, f"\\{char}")
    return out.replace("\n", " ").strip()


def _one_line(text: str, width: int = 300) -> str:
    """Collapse to a single line for `description`; the full text stays in the body."""
    return textwrap.shorten(" ".join(text.split()), width=width, placeholder=" …")


def describe(layer) -> str:
    """Fall back to a generated sentence when the GDB carries no summary."""
    kind = "table" if layer.stype == "Table" else f"{layer.stype} feature layer"
    return f"ArcGIS {kind} '{layer.alias}' with {len(layer.columns)} described columns."


def schema_table(layer) -> list[str]:
    rows = ["# Schema", "", "| Column | Alias | Type | Sample values |", "|---|---|---|---|"]
    for column in layer.columns:
        values = ", ".join(f"`{_cell(v)}`" for v in column.values)
        if column.minmax:
            values = f"range {_cell(column.minmax[0])} to {_cell(column.minmax[1])}"
        rows.append(
            f"| `{_cell(column.name)}` | {_cell(column.alias)} | {_cell(column.dtype)} | {values} |"
        )
    return rows


def domain_tables(layer) -> list[str]:
    """One table per coded-value column — the domains and subtypes, decoded."""
    coded = [c for c in layer.columns if c.keyval]
    if not coded:
        return []
    rows = ["# Domains", ""]
    for column in coded:
        rows += [f"## `{column.name}`", "", "| Code | Value |", "|---|---|"]
        rows += [f"| `{_cell(k)}` | {_cell(v)} |" for k, v in column.keyval.items()]
        rows.append("")
    return rows[:-1]


def hint_sections(layer) -> list[str]:
    """The hint strings the toolbox generated, the ones a text-to-SQL agent reads."""
    hinted = [c for c in layer.columns if c.hints]
    if not hinted:
        return []
    rows = ["# Query hints", ""]
    for column in hinted:
        rows += [f"## `{column.name}`", ""]
        rows += [f"- {hint}" for hint in column.hints]
        rows.append("")
    return rows[:-1]


def store(uri: str) -> tuple[str, str, str]:
    """``(tag, title, prose)`` for the store a layer's ``uri`` names.

    Derived from the uri rather than carried on the ``Layer``: that class mirrors
    the catalog format field for field, and a new attribute would change the Layers.json
    byte shape its consumers key off.
    """
    parent = Path(uri).parent
    if parent.suffix.lower() == ".gdb":
        return "filegdb", f"File GDB {parent.name}", "File GDB"
    return "postgis", f"PostGIS database {parent.name}", "database"


def concept(layer, *, aprx: Path, generated: dict) -> str:
    """One OKF concept document for one layer."""
    summary = layer.hints[0] if layer.hints else ""
    gdb = Path(layer.uri).parent
    tag, store_title, store_prose = store(layer.uri)
    body = [
        frontmatter(
            [
                ("type", "ArcGIS Table" if layer.stype == "Table" else "ArcGIS Feature Layer"),
                ("title", layer.name),
                ("description", _one_line(summary) if summary else describe(layer)),
                ("resource", path_field(Path(layer.uri))),
                ("tags", ["arcgis", tag, layer.stype.lower()]),
                ("generated", generated),
                # Extensions: what a consumer needs to turn this concept back into a
                # query. `table_name` is the physical table the loaders create.
                ("table_name", layer.table_name),
                ("stype", layer.stype),
                ("display", layer.display),
                ("subtype", layer.subtype),
                (
                    "sources",
                    [
                        source("gdb", gdb, store_title),
                        source("aprx", aprx, f"ArcGIS project {aprx.name}"),
                    ],
                ),
            ]
        ),
        "",
    ]
    if summary:
        body += [summary.strip(), ""]
    body += schema_table(layer)
    for section in (domain_tables(layer), hint_sections(layer)):
        if section:
            body += ["", *section]
    body += [
        "",
        f"Field names, types, domains and sampled values come from the {store_prose};[^gdb] "
        "layer name, aliases and hidden fields come from the map layer.[^aprx]",
        "",
        # .title() would render "File GDB" as "File Gdb".
        f"[^gdb]: {store_prose[0].upper() + store_prose[1:]}",
        "[^aprx]: ArcGIS project",
        "",
    ]
    return "\n".join(body)


def index(layers, stem: dict) -> str:
    """The bundle-root ``index.md`` — the only place frontmatter is allowed (OKF §12)."""
    rows = [frontmatter([("okf_version", OKF_VERSION)]), ""]
    for heading, wanted in (("Feature Layers", False), ("Tables", True)):
        group = [ell for ell in layers if (ell.stype == "Table") is wanted]
        if not group:
            continue
        rows += [f"# {heading}", ""]
        for layer in group:
            summary = layer.hints[0] if layer.hints else ""
            text = _one_line(summary, 160) if summary else describe(layer)
            rows.append(f"* [{_cell(layer.name)}]({stem[id(layer)]}.md) - {text}")
        rows.append("")
    return "\n".join(rows)


def write_bundle(layers, out: Path, *, aprx: Path, at: dt.datetime | None = None) -> list[Path]:
    """Write the bundle; returns every file written, ``index.md`` first.

    ``check_table_names`` runs here rather than at the call site: ``table_name`` is
    what names the file, so the guard that proves it is a plain ASCII identifier
    belongs where the path is built, not somewhere a second caller can forget it.
    It permits an empty ``table_name`` (web-service layers, which have no table),
    and there is no filename to fall back to — a layer name is free text and
    ``../elsewhere`` is a layer name.
    """
    check_table_names(layers)
    stem = {}
    for layer in layers:
        if not layer.table_name:
            raise ValueError(f"Layer '{layer.name}' has no table_name to name its concept after.")
        stem[id(layer)] = layer.table_name

    at = at or dt.datetime.now(dt.timezone.utc)
    generated = {"by": producer(), "at": stamp(at)}
    out.mkdir(parents=True, exist_ok=True)
    written = [out / "index.md"]
    written[0].write_text(index(layers, stem), encoding="UTF-8")
    for layer in layers:
        path = out / f"{stem[id(layer)]}.md"
        path.write_text(concept(layer, aprx=aprx, generated=generated), encoding="UTF-8")
        written.append(path)

    # A narrower re-run leaves concepts index.md no longer lists. Warned, not
    # deleted: -o can be any directory the user chose, and this tool has no
    # business removing files it did not write.
    stale = sorted(p.name for p in out.glob("*.md") if p not in written)
    if stale:
        print(
            f"warning: {out} still holds {len(stale)} unlisted: {', '.join(stale)}", file=sys.stderr
        )
    return written


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Author an OKF v0.2 knowledge bundle from an .aprx + File GDB.")
    args = parser.parse_args(argv)
    check_limits(parser, args)

    try:
        layers = build_layers(
            args.aprx,
            pg_tables=args.pg_table,
            max_records=args.max_records,
            max_values=args.max_values,
            use_ilike=args.use_ilike,
            subtype_alias_suffix=args.subtype_alias_suffix,
            include=args.include,
            exclude=args.exclude,
            map_name=args.map_name,
        )
        if not layers:
            print("Did not find any feature layers to process :-(", file=sys.stderr)
            return 1

        written = write_bundle(layers, args.out, aprx=args.aprx.resolve())
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {len(written) - 1} concepts to {args.out}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
