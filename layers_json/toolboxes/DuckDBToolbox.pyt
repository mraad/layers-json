import os
import re
import sys
from contextlib import suppress

import arcpy
import duckdb
import pyarrow as pa

# Pro disables user-site packages. A bundled toolbox can load its sibling
# package directly, whether opened from a checkout or an installed wheel.
_package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.isfile(os.path.join(_package_root, "layers_json", "__init__.py")) and _package_root not in sys.path:
    sys.path.insert(0, _package_root)

from layers_json.duckdb_export import (  # noqa: E402
    arrow_batch,
    arrow_types,
    create_indices,
    quote,
    write_batch,
    write_sp_ref,
)


class Toolbox(object):
    def __init__(
        self,
    ):
        self.label = "DuckDBToolbox"
        self.alias = "DuckDBToolbox"
        self.tools = [DuckDBToFeatureLayer, FeatureLayersToDuckDBBatched]


class DuckDBToolBase(object):
    """Base class for DuckDB tools with shared functionality."""

    CIM_VERSION = "V3"

    EXCLUDE_NAMES = (
        "globalid",
        "shape_length",
        "shape_area",
        "shape__length",
        "shape__area",
        "st_area(shape)",
        "st_perimeter(shape)",
    )
    EXCLUDE_TYPES = ("Geometry", "Raster")

    ARCGIS_TO_ARROW = {**arrow_types(pa), "OID": pa.int32()}

    def __init__(
        self,
    ):
        self.canRunInBackground = True

    def isLicensed(
        self,
    ):
        return True

    def updateParameters(
        self,
        parameters,
    ):
        return

    def updateMessages(
        self,
        parameters,
    ):
        return

    @staticmethod
    def _sanitize_table_name(name):
        """Sanitize a layer name into a safe DuckDB table identifier."""
        sanitized = re.sub(r"[^A-Za-z0-9_]", "_", name)
        if not sanitized:
            return "_"
        if sanitized[0].isdigit():
            sanitized = f"t_{sanitized}"
        return sanitized

    @staticmethod
    def _parse_multivalue(text):
        """Parse ArcGIS semicolon-delimited values without stripping apostrophes."""
        if not text:
            return []
        values = []
        chars = []
        quote = None
        i = 0
        while i < len(text):
            char = text[i]
            if quote:
                if char == quote:
                    if i + 1 < len(text) and text[i + 1] == quote:
                        chars.append(char)
                        i += 1
                    else:
                        quote = None
                else:
                    chars.append(char)
            elif char == ";":
                value = "".join(chars).strip()
                if value:
                    values.append(value)
                chars = []
            elif char in {"'", '"'} and not "".join(chars).strip():
                quote = char
            else:
                chars.append(char)
            i += 1
        if quote:
            raise ValueError("Unterminated quoted ArcGIS multi-value parameter.")
        value = "".join(chars).strip()
        if value:
            values.append(value)
        return values

    @staticmethod
    def _default_sp_ref():
        """Return the environment output coordinate system, defaulting to WGS84."""
        sp_ref = arcpy.env.outputCoordinateSystem
        if not sp_ref:
            sp_ref = arcpy.SpatialReference(4326)
            arcpy.AddMessage("Environment Output Coordinate System is not set. Using WGS84.")
        return sp_ref

    @staticmethod
    def _default_database_path():
        """Return a project-adjacent database path, including for unsaved projects."""
        curr_proj = arcpy.mp.ArcGISProject("CURRENT")
        if curr_proj.filePath:
            return os.path.splitext(curr_proj.filePath)[0] + ".ddb"
        return os.path.join(curr_proj.homeFolder, "layers.ddb")

    @staticmethod
    def _get_env_extent_polygon(default_sr):
        """Return a Polygon built from arcpy.env.extent, or None if not defined.

        :param default_sr: Fallback SpatialReference if the env extent has none.
        :return: arcpy.Polygon or None.
        """
        ext = arcpy.env.extent
        if ext is None:
            return None
        sentinels = {"", "NONE", "MAXOF", "MINOF", "DEFAULT"}
        if isinstance(ext, str) and ext.strip().upper() in sentinels:
            return None
        if str(ext).strip().upper() in sentinels:
            return None
        try:
            xmin, ymin, xmax, ymax = ext.XMin, ext.YMin, ext.XMax, ext.YMax
        except AttributeError:
            return None
        sr = getattr(ext, "spatialReference", None) or default_sr
        return arcpy.Polygon(
            arcpy.Array(
                [
                    arcpy.Point(xmin, ymin),
                    arcpy.Point(xmin, ymax),
                    arcpy.Point(xmax, ymax),
                    arcpy.Point(xmax, ymin),
                    arcpy.Point(xmin, ymin),
                ]
            ),
            sr,
        )

    @staticmethod
    def _parse_layer_filters(include_text, exclude_text):
        """Parse semicolon-delimited include/exclude layer text into lists."""
        return (
            DuckDBToolBase._parse_multivalue(include_text),
            DuckDBToolBase._parse_multivalue(exclude_text),
        )

    @staticmethod
    def _should_process(layer, include_layers, exclude_layers):
        """Return True if a feature layer or standalone table should be exported."""
        if getattr(layer, "isGroupLayer", False) or getattr(layer, "isBasemapLayer", False):
            return False
        if getattr(layer, "isBroken", False):
            return False
        if getattr(layer, "isWebLayer", False):
            return False
        source = getattr(layer, "dataSource", "") or ""
        if str(source).lower().startswith(("http://", "https://")):
            return False
        is_feature_layer = getattr(layer, "isFeatureLayer", None)
        layer_name = getattr(layer, "longName", layer.name) if is_feature_layer is not None else layer.name
        if exclude_layers and layer_name in exclude_layers:
            return False
        if include_layers and layer_name not in include_layers:
            return False
        if is_feature_layer is False:
            return False
        return True

    @classmethod
    def _collect_export_layers(cls, layers, include_layers, exclude_layers):
        """Return selected ``(layer, table_name)`` pairs after collision checks."""
        selected = []
        sources_by_table = {}
        for layer in layers:
            if not cls._should_process(layer, include_layers, exclude_layers):
                continue

            table_name = cls._sanitize_table_name(layer.name)
            source_name = getattr(layer, "longName", layer.name)
            catalog_table_name = layer.name.replace(" ", "_")
            if table_name != catalog_table_name:
                raise ValueError(
                    f"Layer '{source_name}' cannot map consistently to Layers.json and DuckDB. "
                    "Rename it to start with an ASCII letter or underscore and use only ASCII "
                    "letters, digits, underscores, and spaces."
                )
            table_key = table_name.casefold()
            if table_key == "sp_ref":
                raise ValueError(
                    f"Layer '{source_name}' maps to reserved DuckDB table 'sp_ref'; rename the layer."
                )
            if table_key in sources_by_table:
                first = sources_by_table[table_key]
                raise ValueError(
                    f"Layers '{first}' and '{source_name}' both map to DuckDB table "
                    f"'{table_name}'; rename one layer before export."
                )
            sources_by_table[table_key] = source_name
            selected.append((layer, table_name))
        return selected

    @classmethod
    def _describe_fields(cls, layer):
        """Extract field metadata for a layer, returning export-ready structures.

        :return: (oid_field, shp_field, fields, columns, schema)
        """
        desc = arcpy.Describe(layer)
        layer_cim = layer.getDefinition(cls.CIM_VERSION)
        oid_field = getattr(desc, "OIDFieldName", "") or ""
        shp_field = getattr(desc, "shapeFieldName", "") or ""

        display_table = getattr(layer_cim, "featureTable", layer_cim)
        field_descriptions = getattr(display_table, "fieldDescriptions", None) or []
        exclude_names = {
            fd.fieldName.lower() for fd in field_descriptions if not fd.visible
        } | set(cls.EXCLUDE_NAMES)
        if oid_field:
            exclude_names.discard(oid_field.lower())

        geometry_field = next(
            (
                f.name
                for f in desc.fields
                if f.name.casefold() == "geometry"
                and f.name.casefold() != shp_field.casefold()
            ),
            None,
        )
        if geometry_field:
            raise ValueError(
                f"Attribute column '{geometry_field}' collides with the reserved geometry column; "
                "rename the field before export."
            )

        name_type = [
            (f.name, f.type)
            for f in desc.fields
            if f.type not in cls.EXCLUDE_TYPES and f.name.lower() not in exclude_names
        ]
        fields = [name for (name, _) in name_type]
        columns = fields.copy()
        if shp_field:
            fields.append("SHAPE@WKB")
            columns.append(shp_field)
        # Arrow schema aligned to `columns`: attribute fields then the WKB shape column (binary).
        has_oid64 = bool(getattr(desc, "hasOID64", False))
        arrow_fields = [
            pa.field(
                name,
                pa.int64()
                if has_oid64 and name.casefold() == oid_field.casefold()
                else cls.ARCGIS_TO_ARROW.get(dtype, pa.string()),
            )
            for name, dtype in name_type
        ]
        if shp_field:
            arrow_fields.append(pa.field(shp_field, pa.binary()))
        schema = pa.schema(arrow_fields)
        return oid_field, shp_field, fields, columns, schema

    def _get_database_path_parameter(
        self,
    ):
        """Create database path parameter."""
        database_path = arcpy.Parameter(
            name="database_path",
            displayName="Database Path",
            direction="Input",
            datatype="GPString",
            parameterType="Required",
        )
        database_path.value = self._default_database_path()
        return database_path

    @staticmethod
    def _get_layer_filter_parameter(name, display_name):
        """Create a multi-valued layer/table filter parameter."""
        return arcpy.Parameter(
            displayName=display_name,
            name=name,
            datatype=["GPFeatureLayer", "GPTableView"],
            parameterType="Optional",
            direction="Input",
            multiValue=True,
        )

    def _get_include_layer_parameter(self):
        return self._get_layer_filter_parameter("include_layer", "Include Layer(s)/Table(s)")

    def _get_exclude_layer_parameter(self):
        return self._get_layer_filter_parameter("exclude_layer", "Exclude Layer(s)/Table(s)")

    def _get_install_spatial_parameter(
        self,
    ):
        """Create install spatial parameter."""
        install_spatial = arcpy.Parameter(
            name="install_spatial",
            displayName="Install Spatial Name or Path",
            direction="Input",
            datatype="GPString",
            parameterType="Required",
            category="Advanced",
        )
        install_spatial.value = "spatial"
        return install_spatial

    def _get_batch_size_parameter(
        self,
        batch_size=100000,
    ):
        """Create batch size parameter."""
        batch_size_param = arcpy.Parameter(
            name="batch_size",
            displayName="Batch Size",
            direction="Input",
            datatype="GPLong",
            parameterType="Required",
            category="Advanced",
        )
        batch_size_param.value = batch_size
        return batch_size_param

    @staticmethod
    def _get_install_spatial(spatial):
        """Format a spatial extension name/path as a safe DuckDB SQL value."""
        if spatial != "spatial":
            return "'" + str(spatial).replace("'", "''") + "'"
        return spatial

    def _connect_duckdb(
        self,
        db_path,
        spatial,
        read_only=True,
    ):
        """Connect to DuckDB and load spatial extension.

        LOAD is tried first: the common case is "already installed", so we skip
        the INSTALL catalog/network hit unless LOAD actually fails.
        """
        conn = duckdb.connect(db_path, read_only=read_only)
        try:
            conn.execute("LOAD spatial;")
        except Exception:
            try:
                spatial_param = self._get_install_spatial(spatial)
                conn.execute(f"INSTALL {spatial_param};")
                conn.execute("LOAD spatial;")
            except Exception as e:
                arcpy.AddError(f"Failed to LOAD spatial extension: {e}")
                try:
                    conn.close()
                except Exception:
                    # Suppress close errors to preserve original LOAD failure
                    pass
                raise  # Fail fast — spatial queries will not work without this
        return conn

    def _create_sp_ref_table(
        self,
        conn,
        sp_ref,
    ):
        """Create a spatial reference lookup table."""
        wkid = sp_ref.factoryCode
        text = sp_ref.exportToString()
        arcpy.AddMessage(f"Exported feature in spatial reference ID: {wkid}")
        write_sp_ref(conn, wkid, text)


class DuckDBToFeatureLayer(DuckDBToolBase):
    def __init__(
        self,
    ):
        super().__init__()
        self.label = "Query DuckDB Using SQL"
        self.description = "Execute a DuckDB SQL query and create a feature layer or table."

    def getParameterInfo(
        self,
    ):
        """Define parameters for the tool"""

        # Output Feature Class
        param0 = arcpy.Parameter(
            name="outputFC",
            displayName="outputFC",
            direction="Output",
            datatype=["Feature Layer", "Table"],
            parameterType="Derived",
        )

        # Parameter 1: DB Path (FILE)
        database_path = arcpy.Parameter(
            displayName="Database Path",
            name="database_path",
            datatype="DEFile",
            parameterType="Required",
            direction="Input",
        )
        database_path.filter.list = ["ddb", "duckdb"]
        database_path.value = self._default_database_path()

        # Parameter 2: SQL Statement (TEXT)
        sql_text = arcpy.Parameter(
            displayName="SQL Statement", name="sql_text", datatype="String", parameterType="Required", direction="Input"
        )
        # https://pro.arcgis.com/en/pro-app/latest/arcpy/geoprocessing_and_python/parameter-controls.htm
        sql_text.controlCLSID = "{E5456E51-0C41-4797-9EE4-5269820C6F0E}"
        sql_text.value = (
            "SELECT\n"
            "ST_AsWKB(geometry) AS SHAPE,\n"
            "OBJECTID AS OID,\n"
            "* EXCLUDE (geometry,OBJECTID)\n"
            "FROM ...\n"
            "LIMIT 1000"
        )

        # Parameter 3: Output Map Layer Name (STRING)
        output_name = arcpy.Parameter(
            displayName="Output Layer or Table Name",
            name="output_name",
            datatype="String",
            parameterType="Required",
            direction="Input",
        )
        output_name.value = "DuckDB"

        return [
            param0,
            sql_text,
            output_name,
            database_path,
            self._get_install_spatial_parameter(),
        ]

    def execute(
        self,
        parameters,
        messages,
    ):
        """Execute the DuckDB query and create a feature class"""
        arcpy.env.overwriteOutput = True

        sql_statement = parameters[1].valueAsText
        output_name = parameters[2].valueAsText
        db_path = parameters[3].valueAsText
        spatial = parameters[4].value

        sp_ref = self._default_sp_ref()

        # Create a feature class in the memory workspace
        workspace = "memory"
        fc = os.path.join(workspace, output_name)
        if arcpy.Exists(fc):
            arcpy.management.Delete(fc)

        last_symbology = None
        project = arcpy.mp.ArcGISProject("current")
        for layer in project.activeMap.listLayers():
            if layer.name == output_name:
                last_symbology = os.path.join(arcpy.env.scratchFolder, output_name)
                layer.saveACopy(last_symbology)
                arcpy.AddMessage(f"Layer File {last_symbology}")
                break

        messages.addMessage(f"Connecting to DuckDB: {db_path}")

        conn = self._connect_duckdb(db_path, spatial, read_only=True)
        try:
            # Only trim outer whitespace + a trailing ';'. Do NOT collapse internal
            # whitespace — that corrupts string literals (e.g. 'New  York' -> 'New York').
            # DuckDB handles newlines fine, including inside the subquery wrap below.
            sql_statement = sql_statement.strip().rstrip(";")
            if not sql_statement:
                raise ValueError("SQL Statement cannot be empty.")

            # Bind the query (no full execution) to detect a DuckDB GEOMETRY column and
            # convert it to WKB aliased as SHAPE. If the query already yields WKB (e.g.
            # ST_AsWKB(...) AS SHAPE) there is no GEOMETRY column and the SQL is left as-is.
            # This replaces a word-matching regex that corrupted identifiers in WHERE/JOIN.
            rel = conn.sql(sql_statement)
            geom_cols = [name for name, t in zip(rel.columns, rel.types) if str(t).upper() == "GEOMETRY"]
            if len(geom_cols) > 1:
                messages.addMessage(
                    f"Multiple geometry columns {geom_cols}; keeping '{geom_cols[0]}' as SHAPE, dropping the rest."
                )
            if geom_cols:
                excludes = ", ".join(quote(c) for c in geom_cols)
                gq = quote(geom_cols[0])
                sql_statement = f"SELECT * EXCLUDE({excludes}), ST_AsWKB({gq}) AS SHAPE FROM ({sql_statement})"
                rel = conn.sql(sql_statement)

            messages.addMessage(f"Executing {sql_statement}")
            tab = rel.fetch_arrow_table()
            row_count = tab.num_rows
            messages.addMessage(f"Found {row_count} row(s)")
            if row_count > 0:
                # Create Arrow schema with ESRI metadata for the SHAPE field
                metadata = {"esri.encoding": "WKB", "esri.sr_wkt": sp_ref.exportToString()}

                # Reconstruct schema with metadata on the SHAPE field.
                fields = []
                has_shape = False
                shape_names = [field.name for field in tab.schema if field.name.casefold() == "shape"]
                if len(shape_names) > 1:
                    raise ValueError("Query returned multiple fields named SHAPE (case-insensitive).")
                shape_name = shape_names[0] if shape_names else None
                if shape_name and shape_name != "SHAPE":
                    tab = tab.rename_columns(
                        ["SHAPE" if name == shape_name else name for name in tab.column_names]
                    )
                    shape_name = "SHAPE"
                for field in tab.schema:
                    if field.name == shape_name:
                        fields.append(pa.field("SHAPE", pa.binary(), nullable=field.nullable, metadata=metadata))
                        has_shape = True
                    else:
                        fields.append(field)

                schema = pa.schema(fields)
                tab = tab.cast(schema)

                if has_shape:
                    messages.addMessage(f"Creating feature class: {output_name}")
                    arcpy.management.CopyFeatures(tab, fc)
                    if last_symbology:
                        parameters[0].symbology = f"{last_symbology}.lyrx"
                    messages.addMessage(f"Success! Feature class '{output_name}' created in memory workspace.")
                else:
                    messages.addMessage(f"Creating table: {output_name}")
                    arcpy.management.CopyRows(tab, fc)
                    messages.addMessage(f"Success! Table '{output_name}' created in memory workspace.")

                parameters[0].value = fc
            else:
                messages.addMessage("No rows returned.")
        finally:
            conn.close()


class FeatureLayersToDuckDBBatched(DuckDBToolBase):
    """Export feature layers and standalone tables to DuckDB in batches."""

    def __init__(
        self,
    ):
        super().__init__()
        self.label = "Feature Layers and Tables to DuckDB"
        self.description = "Export feature layers and tables to DuckDB in memory-bounded batches"

    def getParameterInfo(
        self,
    ):
        return [
            self._get_include_layer_parameter(),
            self._get_exclude_layer_parameter(),
            self._get_database_path_parameter(),
            self._get_batch_size_parameter(100000),
            self._get_install_spatial_parameter(),
        ]

    def updateMessages(
        self,
        parameters,
    ):
        batch_size = parameters[3].value
        if batch_size is not None and batch_size < 1:
            parameters[3].setErrorMessage("Batch Size must be at least 1.")

    def execute(
        self,
        parameters,
        _,
    ):
        arcpy.env.autoCancelling = False

        include_text = parameters[0].valueAsText
        exclude_text = parameters[1].valueAsText
        duckdb_path = parameters[2].valueAsText
        batch_size = parameters[3].value
        spatial = parameters[4].value

        if batch_size is None or batch_size < 1:
            raise ValueError("Batch Size must be at least 1.")

        sp_ref = self._default_sp_ref()
        include_layers, exclude_layers = self._parse_layer_filters(include_text, exclude_text)
        extent_polygon = self._get_env_extent_polygon(sp_ref)
        if extent_polygon is not None:
            arcpy.AddMessage("Filtering features by arcpy.env.extent (INTERSECTS).")
        curr_proj = arcpy.mp.ArcGISProject("CURRENT")
        if curr_proj.activeMap is None:
            raise RuntimeError("The current ArcGIS Pro project has no active map.")
        map_items = [
            *curr_proj.activeMap.listLayers(),
            *curr_proj.activeMap.listTables(),
        ]
        layers_to_export = self._collect_export_layers(
            map_items, include_layers, exclude_layers
        )
        if not layers_to_export:
            arcpy.AddWarning("No feature layers or tables matched the export filters.")
            return

        with self._connect_duckdb(duckdb_path, spatial, read_only=False) as conn:
            # Faster, lower-memory bulk inserts; row order is irrelevant (PK + RTREE added after load).
            conn.execute("SET preserve_insertion_order=false;")
            self._create_sp_ref_table(conn, sp_ref)

            for layer, table_name in layers_to_export:
                if arcpy.env.isCancelled:
                    break

                layer_name = layer.name

                oid_field, shp_field, fields, columns, schema = self._describe_fields(layer)
                cursor_kwargs = {}
                if shp_field:
                    cursor_kwargs["spatial_reference"] = sp_ref
                    if extent_polygon is not None:
                        cursor_kwargs["spatial_filter"] = extent_polygon
                        cursor_kwargs["spatial_relationship"] = "INTERSECTS"
                suffix = " (extent filtered)" if extent_polygon is not None and shp_field else ""
                arcpy.SetProgressor("default", f"Exporting {layer_name}{suffix}...")

                transaction_open = False
                try:
                    # A transaction both reduces commit overhead and guarantees that
                    # cancellation/failure cannot leave a half-replaced table behind.
                    conn.execute("BEGIN TRANSACTION;")
                    transaction_open = True
                    rows = []
                    total_processed = 0
                    table_created = False

                    with arcpy.da.SearchCursor(layer, fields, **cursor_kwargs) as cursor:
                        for row in cursor:
                            rows.append(row)

                            if (
                                arcpy.env.isCancelled
                                and (len(rows) % 1024 == 0 or len(rows) >= batch_size)
                            ):
                                break
                            if len(rows) >= batch_size:
                                write_batch(
                                    conn, table_name, shp_field, arrow_batch(rows, schema, pa),
                                    not table_created, replace=True,
                                )
                                table_created = True

                                total_processed += len(rows)
                                arcpy.SetProgressorLabel(f"Exported {total_processed} {layer_name}")
                                rows = []

                    if arcpy.env.isCancelled:
                        conn.execute("ROLLBACK;")
                        transaction_open = False
                        arcpy.AddWarning(
                            f"Cancelled {layer_name}; any previously committed table is unchanged."
                        )
                        break

                    if rows or not table_created:
                        write_batch(
                            conn, table_name, shp_field, arrow_batch(rows, schema, pa),
                            not table_created, replace=True,
                        )
                        total_processed += len(rows)

                    create_indices(conn, table_name, oid_field, bool(shp_field))
                    arcpy.AddMessage(f"Created Table {table_name}: {total_processed} rows exported.")

                    conn.execute("COMMIT;")
                    transaction_open = False
                    arcpy.SetProgressorLabel(f"Completed {layer_name}: {total_processed} rows")

                except Exception:
                    if transaction_open:
                        with suppress(Exception):
                            conn.execute("ROLLBACK;")
                    raise
                finally:
                    arcpy.ResetProgressor()
