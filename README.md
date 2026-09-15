# layers-json

Even a very capable LLM needs to understand the database it is querying. A column
called `STATUS` and a value of `3` tell it very little. Does that mean an active
well, an abandoned pipeline, or something else? Without the metadata that explains
your data, the model is left guessing. Writing SQL that runs is only part of the
job; answering the question correctly requires knowing what the data means.

This is where the ArcGIS platform gives us a useful starting point. An ArcGIS Pro
project and its supporting geodatabases, including a File Geodatabase, can already
hold that context: layer names, field aliases, metadata descriptions, subtypes,
domains, and the data itself. An alias makes a field understandable. A domain
explains its codes or allowed range. A subtype identifies the kind of feature a
row represents. Sample values show what is actually stored. Together, these details
help connect the words in a question to the right tables, columns, and filters.

**layers-json makes that context available outside ArcGIS Pro.** It reads the
project alongside its referenced data sources and turns their metadata into
`Layers.json` catalogs and OKF knowledge bundles. `Layers.json` gives applications
a structured description of the data; OKF presents that knowledge as readable
Markdown with structured frontmatter. Both can supply the context an LLM needs
when generating a query.

That context matters for smaller models, too. Giving a model the meaning of a
field and its coded values reduces how much it has to infer, regardless of its
size. Better metadata gives it a better chance of producing the correct query
and, ultimately, the answer you were looking for. That is the reason this project
exists: to put the knowledge already captured in your GIS to work.

The project also exports spatial data to databases, so the catalog and the rows
it describes can be used together. Its outputs support data discovery,
documentation, SQL applications, and text-to-SQL assistants without requiring a
particular downstream application.

## Choose a workflow

| Goal | Entry point | Runtime | Output |
|---|---|---|---|
| Describe an ArcGIS project or PostgreSQL tables | `layers-json` | Python + GDAL | `Layers.json` |
| Write a readable knowledge bundle | `layers-okf` | Python + GDAL | Markdown and YAML frontmatter |
| Hide fields and update aliases in a project copy | `layers-hide-update` | Python + GDAL | `<project>.updated.aprx` |
| Prepare a catalog in the active map | `Layers.pyt` | ArcGIS Pro | `Layers.json` |
| Export a complete project's attributes and geometry | `layers-duckdb` | ArcGIS Pro Python + DuckDB extra | `<project>.duckdb` |
| Export selected map layers; query DuckDB back into Pro | `DuckDBToolbox.pyt` | ArcGIS Pro + DuckDB extra | DuckDB tables / Pro layers |
| Bulk-load a File Geodatabase | `scripts/fgdb_to_*.sh` | Bash + GDAL + target tools | DuckDB, PostGIS, or Oracle Spatial |

Catalogs describe data; they do not copy its rows. The exporters and bulk loaders
create the databases. See the [script reference](scripts/README.md) for their
requirements, options, and replacement behavior.

## Install from source

Use Python 3.11 or newer. Start with a checkout:

```bash
git clone https://github.com/mraad/layers-json.git
cd layers-json
python -m pip install .
```

For the off-Pro commands, install system GDAL and matching Python bindings:

```bash
python -m pip install "gdal==$(gdal-config --version).*"
```

The `fgdb` extra declares the GDAL requirement, but the bindings must match your
system library. If GDAL is missing, the commands print installation guidance.
ArcPy is not required for these commands. The base package uses the standard
library; optional dependencies are grouped into `fgdb`, `duckdb`, `model`, and `dev`.

For development with uv:

```bash
uv sync --extra dev --extra fgdb --extra duckdb
```

For ArcGIS Pro, install the checkout into the Pro Python environment or a clone:

```powershell
python -m pip install -e ".[duckdb]"
```

Then add `layers_json/toolboxes/Layers.pyt` and/or `DuckDBToolbox.pyt` from the
Catalog pane in Pro. Keep the bundled `layers_json` directory intact: the toolboxes
load shared Python modules beside them, including when Pro disables user-site
packages. They are not standalone files. The catalog service-reader path also uses Pro's `arcgis` and `requests` packages.

## Create a metadata catalog

```bash
layers-json /data/NorthSea/NorthSea.aprx
# Writes /data/NorthSea/Layers.json

layers-json /data/NorthSea/NorthSea.aprx --map Map \
  --include "Wells" "Pipelines" --max-values 20 -o ./catalog
```

The reader combines the project's CIM layer configuration with the underlying
File Geodatabase or PostgreSQL metadata. Layer aliases and hidden fields come
from the map; domains, subtypes, and field descriptions come from the data source.
Definition queries constrain sampling when GDAL accepts the expression; a rejected
expression produces a warning. Columns with no sampled or domain values are pruned,
so this is a query-oriented catalog rather than an exhaustive schema dump.

| Option | Behavior |
|---|---|
| `-o`, `--out` | Output directory; defaults to the project's parent |
| `--map` | Map name; required for projects containing multiple maps |
| `--include`, `--exclude` | Map layer names, including `Group\Layer` paths |
| `--max-records` | Maximum sampled rows per layer; default 20000 |
| `--max-values` | Maximum sampled distinct values per field; default 20 |
| `--use-ilike` | Generate ILIKE guidance for mixed-case strings |
| `--subtype-alias-suffix` | Append the layer alias to subtype descriptions |
| `--pg-table` | Add `schema.table[=Name[:display_field]]`; repeatable |

PostgreSQL tables can be catalogued without a project:

```bash
PGDATABASE=northsea PGHOST=localhost PGUSER=reader \
  layers-json --pg-table 'public.wells=Wells:well_name' -o ./catalog
```

Use libpq authentication (`~/.pgpass` or `PGPASSWORD`). SDE connection information
in the project supplies PostgreSQL connection defaults; `PGHOST`, `PGPORT`,
`PGUSER`, and `PGDATABASE` override them. Encrypted project passwords are not used.
PostgreSQL-only tables lack the map aliases and File GDB subtype metadata.
Other SDE backends and web services are skipped by the off-Pro catalog reader.

`Layers.json` contains a `layers` array. Each layer describes its name, alias,
geometry type, display field, subtype, columns, hints, and source URI. Database
layers include `table_name`; service-only layers omit it. Columns contain their
name, alias, type, ranges, coded values, sampled values, and query hints.
Catalog database names must map to unique ASCII identifiers (spaces become
underscores); `sp_ref` is reserved. Rename unsuitable layers before export.

A consumer can use the optional validated read model:

```bash
python -m pip install ".[model]"
```

```python
from layers_json.layers import Layers

catalog = Layers.load("catalog/Layers.json")
wells = catalog.find_layer("Wells")
print(wells.sql_table)
```

## Publish a readable knowledge bundle

```bash
layers-okf /data/NorthSea/NorthSea.aprx --map Map -o ./knowledge
```

The same reader and catalog rules produce an OKF v0.2 bundle: `index.md` plus one
Markdown concept per table. Concepts include schema tables, decoded domains,
query hints, and source references. Re-running updates the index and current
concepts; old concept files remain on disk with a warning. The output does not
claim that generated content has been independently verified.

## Update field presentation

```bash
layers-hide-update /data/NorthSea/NorthSea.aprx --map Map
layers-hide-update /data/NorthSea/NorthSea.aprx --map Map \
  -c rules.json -o /data/NorthSea/NorthSea.filtered.aprx --force
```

Rules use the same `HideUpdateTool.json` format as the Pro toolbox:

```json
{
  "include_layers": ["Wells"],
  "exclude_layers": [],
  "exclude_fields": ["Wells\\.INTERNAL_.*"],
  "field_aliases": [["Wells\\.WELL_NAME", "Well name"]]
}
```

Patterns match `Group\Layer.field` from the start, case-insensitively. System
field visibility takes precedence. This command supports File GDB layers and
writes a new sibling project atomically; it never modifies the source project.
The new project stays beside the original so relative data paths remain valid.

## Work inside ArcGIS Pro

`Layers.pyt` provides four tools for the active map:

1. **Enable CIM on Layers** clears the object-ID alias.
2. **Hide and Update Fields** applies optional field visibility and alias rules.
3. **Set Definition Query** sets or clears optional object-ID range filters.
4. **Prepare Metadata** writes the catalog from local layers, supported web
   layers, and standalone tables.

`DuckDBToolbox.pyt` exports selected local feature layers and tables in batches,
with an object-ID primary key and an R-tree index for geometry. It honors hidden
fields and the geoprocessing extent, and replaces each table transactionally.
Its query tool turns SQL results back into an in-memory feature layer or table.

For a complete project export from a Pro terminal:

```powershell
layers-duckdb "C:\Projects\NorthSea\NorthSea.aprx" --map Map
# The checkout wrapper calls the same entry point:
python scripts/aprx_to_duckdb.py "C:\Projects\NorthSea\NorthSea.aprx"
```

This exporter preserves hidden attributes and GlobalIDs, clears saved selections,
honors definition queries, and exports to EPSG:4326. It handles table-name
collisions with suffixes and records the mapping in `_export_layers`. All tables
are staged into a new database; `--overwrite` replaces existing output only after
a successful export. These policies differ intentionally from the filtered
active-map toolbox export. Details are in the [script reference](scripts/README.md).

## Shared implementation

```text
Layers.pyt (ArcPy reader) ───────┐
                               ├── catalog.py: models, JSON writer, hints, XML
layers-json / layers-okf (GDAL) ┘

DuckDBToolbox.pyt ──────────────┐
                              ├── duckdb_export.py: Arrow types, conversion, SQL batches/indexes
layers-duckdb / script wrapper ┘
```

The CLI never loads `.pyt` code or installs fake ArcPy modules. ArcGIS-specific
reading and UI stay in the adapters. Shared modules define the common behavior;
`layers_json.layers` is the optional Pydantic model for reading a saved catalog.
The old `--toolbox` override has been removed: customize the shared module so
both readers use the same rules.

## Development and verification

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

Tests cover catalog serialization, XML safety, CIM parsing, subtype/domain hints,
field updates, Arrow types, DuckDB transactions, and loader validation. GDAL and
DuckDB integration tests need their optional dependencies. Pro adapter tests use
ArcPy doubles; native UI, licensing, authenticated services, and project loading
still require an ArcGIS Pro environment. See [smoke testing](docs/smoke-testing.md)
for an end-to-end checklist and the [blog draft](docs/blog-post.md) for a capability
walkthrough.

### Shared Hide/Update rules

`Layers.pyt` and `layers-hide-update` both use `layers_json/hide_update.py` for
configuration validation, layer selection, field visibility, and alias rules.
Neither entry point calls the other. The toolbox adapts ArcPy fields; the command
adapts CIM JSON and GDAL metadata, preserving its source project and writing a copy.
The shared module also decides which fields are always visible (object ID, shape,
subtype) or always hidden (area, length, GlobalID, and blob/GUID/raster/XML types),
so both entry points classify fields identically.
Both reject invalid regex patterns and alias replacements before editing fields.
Missing CIM field descriptions are initialized automatically from source field
names and aliases before applying rules. No Enable CIM step or dataset schema
change is required. Existing field settings are preserved, and unchanged toolbox
layers are not written back.
