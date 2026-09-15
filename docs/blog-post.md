# From an ArcGIS project to a portable data catalog

Imagine asking an LLM how many active wells are in your study area. It can write
SQL, but your database has a field called `STATUS` containing numbers. Which
number means “active”? Which table contains wells? Without the metadata that
answers those questions, even a very capable model has to guess. A query can run
successfully and still answer the wrong question.

The ArcGIS platform gives us a useful head start. Your ArcGIS Pro project and its
supporting geodatabases, including a File Geodatabase, can contain years of work
explaining the data. Layer names identify what a feature class represents. Field
aliases give storage names a meaning people recognize. Metadata descriptions add
context, domains explain codes and allowed ranges, and subtypes distinguish kinds
of features within the same table. The data itself provides examples of the values
a query will encounter.

Those details help connect a person's question to the database. Reading the
project alongside its referenced geodatabase brings the map's presentation and
the source's metadata together. It gives an LLM something concrete to work with:
which field to use, what a value means, and how to filter for it.

That is why the project produces both `Layers.json` and OKF. The `layers-json`
command writes the JSON catalog, and `layers-okf` writes the OKF bundle. The JSON catalog
gives applications a structured description of the data. The OKF bundle makes
the same knowledge readable as Markdown with structured frontmatter. Applications
can use either format to supply relevant database context to a model.

This matters for smaller LLMs as well. When the meaning of a code is supplied in
the context, the model has less to infer. More capable models need that information
too: model size cannot tell you what an undocumented value means in your particular
database. Better metadata gives the model a better chance of generating the right
query and returning the answer you actually wanted.

The purpose of this project is to make that existing GIS knowledge useful beyond
the map. Alongside its catalogs and knowledge bundles, it provides spatial exports
for applications that need the underlying rows in a SQL database.

## Start with the map you already maintain

Take the North Sea sample project, with Wells and Pipelines layers. The feature
class behind Wells is called `Wellbores`, and `WELL_NAME` appears in the map as
“Well name.” A useful catalog needs those distinctions.

```bash
layers-json /data/NorthSea/NorthSea.aprx --map Map -o ./catalog
```

The command reads the project's layer configuration and underlying data through
GDAL. It combines aliases, field types, domain labels, subtype information, and
sample values into `Layers.json`. This route runs without ArcGIS Pro or ArcPy.
The Pro toolbox supplies an interactive route to the same catalog format.

The result gives a consuming application more context than column names alone.
It can distinguish a layer's display name from its physical table name, decode
coded values, and use generated query hints. These hints assist SQL generation;
the project does not execute an AI model or guarantee that generated SQL is correct.

Sampling is bounded. Columns without sample or domain values are pruned, so the
catalog is designed to help querying rather than replace a full schema inventory.

## Make the context readable

```bash
layers-okf /data/NorthSea/NorthSea.aprx --map Map -o ./knowledge
```

This command uses the same reader and writes a Markdown knowledge bundle. Each
concept includes a schema table, decoded domains, query hints, and source
references. People can read it in an editor, and applications can process its
structured frontmatter. A team can keep the bundle alongside its data documentation.

## Export the rows as well

A catalog tells an application what the data means. It does not move the rows.
For that, the project includes a DuckDB exporter and File Geodatabase loaders.

From an ArcGIS Pro Python environment:

```powershell
layers-duckdb "C:\Projects\NorthSea\NorthSea.aprx" --map Map
```

The terminal exporter shows progress and writes a sibling DuckDB database. It
preserves hidden attributes, original object IDs, GlobalIDs, nulls, and temporal
types. Geometry is exported to EPSG:4326, with a spatial index for each geometry
table. A metadata table records layer-to-table mappings and transformations.

Every table's row count is checked. The output is staged and published only after
all tables and indexes succeed. An existing database requires `--overwrite`.

For a machine without Pro, separate Bash loaders use GDAL to import a File GDB
into DuckDB, PostGIS, or Oracle Spatial. They have different catalog and replacement
policies, documented in the script reference. Choose the workflow based on whether
you need a map's configured layers or the geodatabase's contents.

## One implementation behind two interfaces

The catalog's Python module owns serialization, hint generation, and metadata
parsing. The toolbox reads through ArcPy; the off-Pro command reads through GDAL.
Both call that module. The command does not load the toolbox or pretend ArcPy is
installed.

The DuckDB toolbox and terminal exporter similarly share Arrow conversion,
SQL batch writes, indexes, and the spatial reference table. Their interfaces
retain different jobs: the active-map tool exports selected visible attributes,
while the terminal command exports complete project attributes. Shared conversion
code keeps null and temporal handling consistent without making those workflows
identical.

The Hide/Update tool and its command follow the same pattern. One module decides
which fields stay visible, which are hidden, and how alias rules apply. The toolbox
hands it ArcPy field descriptions; the command hands it CIM and GDAL metadata. A
rule fixed in one place is fixed for both.

## Try it on a small project

Install from the repository, choose a project with a few local layers, and generate
a catalog into a new directory. Inspect the aliases and coded values. Then write
a knowledge bundle or export a DuckDB database, depending on what your application
needs next.

The [README](../README.md) covers installation and capabilities. The
[script reference](../scripts/README.md) explains export policies, and the
[smoke-test guide](smoke-testing.md) separates automated coverage from the checks
that need a native ArcGIS Pro environment.
