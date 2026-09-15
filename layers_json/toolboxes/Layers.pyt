"""ArcGIS Pro tools; shared catalog behavior lives in layers_json.catalog."""

import json
import os
import sys
from collections import Counter
from contextlib import suppress
from itertools import islice
from typing import List

import arcpy
import requests
from arcgis import GIS
from arcgis.features import FeatureLayerCollection

# Pro disables user-site packages. A bundled toolbox can load its sibling
# package directly, whether opened from a checkout or an installed wheel.
_package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.isfile(os.path.join(_package_root, "layers_json", "__init__.py")) and _package_root not in sys.path:
    sys.path.insert(0, _package_root)

from layers_json.catalog import (  # noqa: E402 - resolve bundled package first
    CatalogBuilder,
    Column,
    Layer,
    Layers,
    _safe_fromstring,
    coded_hint,
)
from layers_json.hide_update import apply_rules, build_schema, rules_from_config  # noqa: E402


# ---------------------------------------------------------------------------
# ArcGIS layer helpers
# ---------------------------------------------------------------------------
def should_skip_layer(
        layer,
        layer_name,
        include_layers=None,
        exclude_layers=None,
):
    """Return True if the layer should be skipped during processing.

    :param layer: An arcpy Layer object.
    :param layer_name: The display name of the layer (typically layer.longName).
    :param include_layers: Optional list of layer names to include. If non-empty, only these layers are processed.
    :param exclude_layers: Optional list of layer names to exclude.
    :return: True if the layer should be skipped.
    """
    if layer.isGroupLayer or layer.isBasemapLayer or layer.isRasterLayer:
        return True

    if layer.supports("isBroken") and layer.isBroken:
        return True

    if not layer.supports("dataSource"):
        return True

    if exclude_layers and layer_name in exclude_layers:
        return True

    if include_layers and layer_name not in include_layers:
        return True

    return False


def is_web_backed(
        layer,
):
    """True when a layer is served over the web rather than exported to the DB.

    A hosted feature service is BOTH ``isFeatureLayer`` and ``isWebLayer``, so
    the ``isFeatureLayer`` branch of ``_desc_layer_list`` claims it before the
    ``isWebLayer`` branch is ever reached. Such a layer has no table in the
    spatial database, so it must not be given a ``table_name``: that marks it
    DB-backed and the agent is handed SQL against a table that does not exist.

    Fails toward "service" — the agent still reaches these through
    ``map_add_service``, whereas a bad table name has no recovery.
    """
    if getattr(layer, "isWebLayer", False):
        return True
    source = getattr(layer, "dataSource", "") or ""
    return str(source).lower().startswith(("http://", "https://"))


def parse_arcpy_multivalue(
        text,
):
    """Parse an arcpy multi-value string parameter into a list of clean names.

    :param text: Semicolon-delimited string with optionally quoted names, or None.
    :return: List of unquoted name strings.
    """
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


# ---------------------------------------------------------------------------
# Tool: Hide and Update Fields
# ---------------------------------------------------------------------------
class HideUpdateTool:
    def __init__(
            self,
    ) -> None:
        """Define the Hide and Update tool for managing field properties."""
        self.label = "01 - Hide and Update Fields"
        self.description = "Hide fields and update aliases for feature layers"
        self.canRunInBackground = True
        self.rules = rules_from_config({})

    def getParameterInfo(
            self,
    ):
        """Define the tool parameters."""
        # Try to load previous parameters from JSON file
        params_from_file = {}
        with suppress(Exception):
            curr_proj = arcpy.mp.ArcGISProject("CURRENT")
            json_file = os.path.join(curr_proj.homeFolder, "HideUpdateTool.json")
            with open(json_file, mode="r", encoding="utf-8") as f:
                params_from_file = json.load(f)

        # Parameter 0: Include Layer(s)
        include_layer = arcpy.Parameter(
            displayName="Include Layer(s)",
            name="include_layer",
            datatype=["GPFeatureLayer", "GPMapServerLayer"],
            parameterType="Optional",
            direction="Input",
            multiValue=True
        )
        if "include_layers" in params_from_file:
            include_layer.value = params_from_file["include_layers"]

        # Parameter 1: Exclude Layer(s)
        exclude_layer = arcpy.Parameter(
            displayName="Exclude Layer(s)",
            name="exclude_layer",
            datatype=["GPFeatureLayer", "GPMapServerLayer"],
            parameterType="Optional",
            direction="Input",
            multiValue=True
        )
        if "exclude_layers" in params_from_file:
            exclude_layer.value = params_from_file["exclude_layers"]

        # Parameter 2: Exclude Field Patterns (regex)
        exclude_fields = arcpy.Parameter(
            displayName="Exclude Field Patterns",
            name="exclude_fields",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            multiValue=True
        )
        if "exclude_fields" in params_from_file:
            exclude_fields.value = params_from_file["exclude_fields"]

        # Parameter 3: Field Alias Updates (pattern -> new alias)
        alias_fields = arcpy.Parameter(
            displayName="Field Alias Updates",
            name="alias_fields",
            datatype="GPValueTable",
            parameterType="Optional",
            direction="Input"
        )
        # Define the structure of the table (2 columns, both strings)
        alias_fields.columns = [
            ["GPString", "Field Pattern"],
            ["GPString", "New Alias"]
        ]
        if "field_aliases" in params_from_file:
            alias_fields.value = params_from_file["field_aliases"]

        return [include_layer, exclude_layer, exclude_fields, alias_fields]

    def isLicensed(
            self,
    ):
        """Set whether the tool is licensed to execute."""
        return True

    def updateParameters(
            self,
            parameters,
    ):
        """Modify the values and properties of parameters before internal validation."""
        return

    def updateMessages(
            self,
            parameters,
    ):
        """Modify the messages created by internal validation for each tool parameter."""
        return

    @staticmethod
    def _parameter_rows(value, columns):
        """Convert Pro multivalue/value-table inputs to plain configuration lists."""
        if value is None:
            return []
        if hasattr(value, "rowCount"):
            if columns == 1:
                return [value.getValue(row, 0) for row in range(value.rowCount)]
            return [[value.getValue(row, col) for col in range(columns)]
                    for row in range(value.rowCount)]
        if isinstance(value, list):
            return value if columns == 1 else [list(row) for row in value]
        raise ValueError(f"Unsupported parameter value: {type(value).__name__}")

    def _process_layer(self, layer, layer_name):
        """Adapt ArcPy metadata/CIM fields to the shared rule engine."""
        arcpy.AddMessage(f"Processing layer: {layer_name}")
        cim = layer.getDefinition("V3")
        fields = cim.featureTable.fieldDescriptions or []
        desc = arcpy.Describe(layer)
        schema = build_schema(
            (desc.OIDFieldName, desc.shapeFieldName, desc.subtypeFieldName),
            (desc.areaFieldName, desc.lengthFieldName, desc.globalIDFieldName),
            [(field.name, field.aliasName, field.type) for field in desc.fields],
        )
        doc = {"featureTable": {"fieldDescriptions": [
            {"fieldName": field.fieldName, "alias": field.alias, "visible": field.visible}
            for field in fields
        ]}}
        change = apply_rules(doc, layer_name, schema, self.rules)
        changed = not fields
        if not fields:
            fields = [arcpy.cim.CreateCIMObjectFromClassName("CIMFieldDescription", "V3")
                      for _ in doc["featureTable"]["fieldDescriptions"]]
            for field, updated in zip(fields, doc["featureTable"]["fieldDescriptions"]):
                field.fieldName = updated["fieldName"]
            cim.featureTable.fieldDescriptions = fields
        for field, updated in zip(fields, doc["featureTable"]["fieldDescriptions"]):
            if field.visible != updated["visible"]:
                field.visible = updated["visible"]
                changed = True
            if field.alias != updated["alias"]:
                field.alias = updated["alias"]
                changed = True
        if changed:
            layer.setDefinition(cim)
        arcpy.AddMessage(f"Hidden {len(change.hidden)} field(s), updated {len(change.aliases)} alias(es)")
        return change

    def execute(
            self,
            parameters,
            messages,
    ):
        """Execute the hide and update fields operation."""
        try:
            arcpy.env.autoCancelling = False
            # Get parameter values
            include_text = parameters[0].valueAsText
            exclude_text = parameters[1].valueAsText
            exclude_fields_param = parameters[2].value
            alias_fields_param = parameters[3].value

            # Parse layer filters
            include_layers = parse_arcpy_multivalue(include_text)
            exclude_layers = parse_arcpy_multivalue(exclude_text)

            # Set up pattern matching
            self.rules = rules_from_config({
                "include_layers": include_layers,
                "exclude_layers": exclude_layers,
                "exclude_fields": self._parameter_rows(exclude_fields_param, 1),
                "field_aliases": self._parameter_rows(alias_fields_param, 2),
            })

            # Get the current project and active map
            curr_proj = arcpy.mp.ArcGISProject("CURRENT")
            map_obj = curr_proj.activeMap
            if map_obj is None:
                raise RuntimeError("The current ArcGIS Pro project has no active map.")

            # Process each layer
            layer_count = 0
            for layer in map_obj.listLayers():
                if arcpy.env.isCancelled:
                    arcpy.AddWarning("Hide/Update cancelled; earlier layer changes remain applied.")
                    return
                layer_name = layer.longName
                if (not layer.isFeatureLayer or should_skip_layer(layer, layer_name)
                        or not self.rules.includes_layer(layer_name)):
                    continue

                self._process_layer(layer, layer_name)
                layer_count += 1

            # Save parameters to JSON file
            try:

                # Prepare parameters data
                params_data = self.rules.to_config()

                # Save to JSON file
                json_file = os.path.join(curr_proj.homeFolder, "HideUpdateTool.json")
                with open(json_file, mode="w", encoding="utf-8") as f:
                    json.dump(params_data, f, indent=2, ensure_ascii=False)
                arcpy.AddMessage(f"Saved parameters to {json_file}")
            except Exception as e:
                arcpy.AddWarning(f"Error saving parameters to JSON: {str(e)}")

            arcpy.AddMessage(f"Successfully processed {layer_count} layer(s)")

        except Exception as e:
            arcpy.AddError(f"Error executing Hide and Update Fields tool: {str(e)}")
            raise

    def postExecute(
            self,
            parameters,
    ):
        """This method takes place after outputs are processed and added to the display."""
        return


# ---------------------------------------------------------------------------
# Tool: Set Definition Query
# ---------------------------------------------------------------------------
class SetDefinitionQuery:
    """
    Set or clear definition queries for layers based on OBJECTID range.

    This tool filters layers to display only a subset of features by applying
    a definition query based on OBJECTID values. Useful for working with large
    datasets by limiting display to a manageable range.
    """

    def __init__(
            self,
    ) -> None:
        """Initialize the Set Definition Query tool."""
        self.label = "02 - Set Definition Query"
        self.description = "Set or clear definition queries for layers based on OBJECTID range"
        self.canRunInBackground = True

    def getParameterInfo(
            self,
    ) -> List[arcpy.Parameter]:
        include_layer = arcpy.Parameter(
            displayName="Include Layer(s)",
            name="include_layer",
            datatype=["GPFeatureLayer", "GPMapServerLayer"],
            parameterType="Optional",
            direction="Input",
            multiValue=True
        )

        exclude_layer = arcpy.Parameter(
            displayName="Exclude Layer(s)",
            name="exclude_layer",
            datatype=["GPFeatureLayer", "GPMapServerLayer"],
            parameterType="Optional",
            direction="Input",
            multiValue=True
        )

        clear_queries = arcpy.Parameter(
            displayName="Clear Definition Queries",
            name="clear_queries",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        clear_queries.value = False

        objectid_range = arcpy.Parameter(
            displayName="OBJECTID Range",
            name="objectid_range",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input"
        )
        objectid_range.value = 20000

        use_dbms = arcpy.Parameter(
            displayName="Use DBMS",
            name="use_dbms",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        use_dbms.value = False

        return [include_layer, exclude_layer, objectid_range, use_dbms, clear_queries]

    def isLicensed(
            self,
    ) -> bool:
        return True

    def updateParameters(
            self,
            parameters: List[arcpy.Parameter],
    ) -> None:
        clear_queries = bool(parameters[4].value)
        parameters[2].enabled = not clear_queries
        parameters[3].enabled = not clear_queries
        return

    def updateMessages(
            self,
            parameters: List[arcpy.Parameter],
    ) -> None:
        objectid_range = parameters[2].value
        if objectid_range is not None and objectid_range < 1:
            parameters[2].setErrorMessage("OBJECTID Range must be at least 1.")
        return

    def execute(
            self,
            parameters: List[arcpy.Parameter],
            messages,
    ) -> None:
        arcpy.env.overwriteOutput = True
        arcpy.env.addOutputsToMap = False
        try:
            # Get parameter values
            include_text = parameters[0].valueAsText
            exclude_text = parameters[1].valueAsText
            objectid_range = parameters[2].value
            use_dbms = parameters[3].value
            clear_queries = parameters[4].value

            # Parse layer filters
            include_layers = parse_arcpy_multivalue(include_text)
            exclude_layers = parse_arcpy_multivalue(exclude_text)

            # Determine statistics function name
            stat_function = "DBMS_MIN" if use_dbms else "MIN"

            arcpy.AddMessage("=" * 60)
            arcpy.AddMessage("Set Definition Query Tool")
            arcpy.AddMessage("=" * 60)
            arcpy.AddMessage(f"Clear queries: {clear_queries}")
            arcpy.AddMessage(f"OBJECTID range: {objectid_range}")
            arcpy.AddMessage(f"Use DBMS: {use_dbms} ({stat_function})")

            # Get the current project and active map
            try:
                curr_proj = arcpy.mp.ArcGISProject("CURRENT")
                map_obj = curr_proj.activeMap
                layer_list = map_obj.listLayers()
            except Exception as e:
                arcpy.AddError(f"Failed to get layers from current project: {str(e)}")
                return

            # Collect layers to process
            layers_to_process = []
            for layer in layer_list:
                # Skip non-feature layers
                if layer.isGroupLayer or layer.isBasemapLayer or layer.isRasterLayer:
                    continue

                if layer.isBroken if hasattr(layer, "isBroken") else False:
                    continue

                if (not layer.isFeatureLayer or
                        not layer.supports("dataSource") or
                        not layer.supports("definitionQuery")):
                    continue

                # Use longName for matching, handle both longName and name
                layer_name = layer.longName if hasattr(layer, "longName") else layer.name

                # Apply layer filters
                if exclude_layers and layer_name in exclude_layers:
                    continue

                if include_layers and layer_name not in include_layers:
                    continue

                layers_to_process.append((layer, layer_name))

            # Set up progressor
            arcpy.SetProgressor(
                "step",
                "Processing definition queries...",
                0,
                len(layers_to_process),
                1
            )

            try:
                layer_count = 0
                for layer, layer_name in layers_to_process:
                    arcpy.SetProgressorPosition()
                    if clear_queries:
                        layer.definitionQuery = ""
                        arcpy.AddMessage(f"  Cleared definition query for: {layer_name}")
                    else:
                        try:
                            desc = arcpy.Describe(layer)
                            prop = layer.connectionProperties
                            workspace_factory = prop.get("workspace_factory", "unk")
                            statistics_oper = "MIN" if workspace_factory == "File Geodatabase" else stat_function
                            out_table = arcpy.CreateUniqueName("_statistics", "memory")
                            original_query = layer.definitionQuery
                            query_applied = False

                            try:
                                # Statistics must run against the full source, not a
                                # definition query left by a previous invocation.
                                layer.definitionQuery = ""
                                # Get the min value for OID using specified statistics function
                                arcpy.analysis.Statistics(
                                    in_table=layer,
                                    out_table=out_table,
                                    statistics_fields=f"{desc.OIDFieldName} {statistics_oper}",
                                    case_field=None,
                                    concatenation_separator="",
                                )

                                # Build field name based on statistics function used
                                min_field_name = f"{statistics_oper}_{desc.OIDFieldName}"
                                with arcpy.da.SearchCursor(
                                        out_table,
                                        [min_field_name]
                                ) as sc:
                                    (min_objectid,) = next(sc, (None,))
                                    if min_objectid is None:
                                        arcpy.AddWarning(f"No records found for: {layer_name}")
                                        continue
                                    oid_field = arcpy.AddFieldDelimiters(
                                        layer, desc.OIDFieldName
                                    )
                                    query_def = (
                                        f"{oid_field} BETWEEN {min_objectid} "
                                        f"AND {min_objectid + objectid_range - 1}"
                                    )
                                    layer.definitionQuery = query_def
                                    query_applied = True
                                    arcpy.AddMessage(
                                        f"  Updated definition query for: {layer_name}"
                                    )
                                    arcpy.AddMessage(f"    Query: {query_def}")
                            finally:
                                if not query_applied:
                                    with suppress(Exception):
                                        layer.definitionQuery = original_query
                                # Always clean up the in-memory temp table
                                if arcpy.Exists(out_table):
                                    arcpy.management.Delete(out_table)
                        except Exception as e:
                            arcpy.AddWarning(
                                f"Error processing layer {layer_name}: {str(e)}"
                            )

                    layer_count += 1
            finally:
                arcpy.ResetProgressor()

            arcpy.AddMessage("=" * 60)
            arcpy.AddMessage(f"Successfully processed {layer_count} layer(s)")
            arcpy.AddMessage("=" * 60)

        except Exception as e:
            arcpy.AddError(f"Error executing Set Definition Query tool: {str(e)}")

    def postExecute(
            self,
            parameters: List[arcpy.Parameter],
    ) -> None:
        return


# ---------------------------------------------------------------------------
# Tool: Prepare (metadata only -- writes Layers.json)
# ---------------------------------------------------------------------------
class PrepareTool(CatalogBuilder):
    """Describe layers/tables and write ``Layers.json`` (metadata only).

    Analyzes the active map's feature layers, web layers and standalone tables,
    extracting field metadata, domains and sample values, then writes a
    ``Layers.json`` catalog. Examples and embeddings are NOT generated.

    Attributes
    ----------
    label : str
        The label for this tool.
    description : str
        The description for this tool.
    layers : list
        List of Layer objects containing processed layer information.
    exclude_types : tuple
        Field types to exclude from processing.
    exclude_names : tuple
        Field names to exclude from processing.
    max_method : str
        Method for limiting record selection ("COUNT", "TOP", "LIMIT").
    max_records : int
        Maximum number of records to process.
    max_values : int
        Maximum number of unique values to collect per field.
    use_ilike : bool
        When True, mixed-case string columns get an ILIKE hint instead of LIKE.
    """

    def __init__(self):
        super().__init__()
        self.label = "03 - Prepare Metadata"
        self.description = "Describe layers/tables and write Layers.json (metadata only)"
        self.message = arcpy.AddMessage
        self._gis = None
        self._gis_dict = None
        self._domains_by_workspace = {}

    def getParameterInfo(
            self,
    ) -> List[arcpy.Parameter]:
        """Define and return the parameter information for the ArcGIS tool.

        Returns
        -------
        List of arcpy.Parameter objects defining the tool's parameters.
        """
        include_layer = arcpy.Parameter(
            displayName="Include Layer(s)/Table(s)",
            name="include_layer",
            datatype=[
                "GPFeatureLayer",
                "GPTableView",
                "GPMapServerLayer",
                "GPFeatureRecordSetLayer",
                "GPInternetTiledLayer",
            ],
            parameterType="Optional",  # Parameter is optional
            direction="Input",
            multiValue=True,  # Allows selecting multiple layers
        )

        exclude_layer = arcpy.Parameter(
            displayName="Exclude Layer(s)/Table(s)",
            name="exclude_layer",
            datatype=[
                "GPFeatureLayer",
                "GPTableView",
                "GPMapServerLayer",
                "GPFeatureRecordSetLayer",
                "GPInternetTiledLayer",
            ],
            parameterType="Optional",  # Parameter is optional
            direction="Input",
            multiValue=True,  # Allows selecting multiple layers
        )

        max_method = arcpy.Parameter(
            displayName="Max Records Method",
            name="max_method",
            datatype="GPString",
            parameterType="Required",  # Required parameter
            direction="Input",
        )

        # Set the allowed values and default
        max_method.filter.type = "ValueList"
        max_method.filter.list = ["TOP", "LIMIT", "COUNT"]
        max_method.value = "COUNT"  # Default value

        max_records = arcpy.Parameter(
            name="max_records",
            displayName="Max Records Read",
            direction="Input",
            datatype="GPLong",
            parameterType="Required",
        )
        max_records.value = self.max_records

        max_values = arcpy.Parameter(
            name="max_values",
            displayName="Max Values Per Field",
            direction="Input",
            datatype="GPLong",
            parameterType="Required",
        )
        max_values.value = self.max_values

        kb_path = arcpy.Parameter(
            name="kb_path",
            displayName="Output Folder",
            direction="Input",
            datatype="DEFolder",
            parameterType="Required",
        )
        curr_proj = arcpy.mp.ArcGISProject("CURRENT")
        kb_path.value = curr_proj.homeFolder

        use_ilike = arcpy.Parameter(
            name="use_ilike",
            displayName="Enable ILIKE (case-insensitive)",
            direction="Input",
            datatype="GPBoolean",
            parameterType="Optional",
        )
        use_ilike.value = self.use_ilike

        subtype_alias_suffix = arcpy.Parameter(
            name="subtype_alias_suffix",
            displayName="Append layer alias to subtype hints (oil discoveries)",
            direction="Input",
            datatype="GPBoolean",
            parameterType="Optional",
        )
        subtype_alias_suffix.value = self.subtype_alias_suffix

        return [
            include_layer,
            exclude_layer,
            max_method,
            max_records,
            max_values,
            kb_path,
            use_ilike,
            subtype_alias_suffix,
        ]

    def isLicensed(
            self,
    ):
        """Check if the tool is licensed to run.

        Returns
        -------
        bool
            Always returns True as no special license is required.
        """
        return True

    def updateParameters(
            self,
            parameters,
    ):
        """Update parameter values and properties.

        This method is called whenever a parameter has been changed.

        Parameters
        ----------
        parameters : list
            List of parameter objects to update.
        """
        return

    def updateMessages(
            self,
            parameters,
    ):
        """Update tool messages.

        This method is called after internal validation.

        Parameters
        ----------
        parameters : list
            List of parameter objects to validate.
        """
        max_records = parameters[3].value
        max_values = parameters[4].value
        if max_records is not None and max_records < 1:
            parameters[3].setErrorMessage("Max Records Read must be at least 1.")
        if max_values is not None and max_values < 0:
            parameters[4].setErrorMessage("Max Values Per Field cannot be negative.")
        return


        # TODO - Add Range values !

    def _desc_columns_web(
            self,
            fl,
            layer_name,
            layer_alias="",
    ) -> list[Column]:
        """Describe all columns in a web-based feature layer.

        Parameters
        ----------
        fl : arcgis.features.FeatureLayer
            The feature layer to analyze.
        layer_name : str
            Name of the layer for reference.
        layer_alias : str
            Alias appended to subtype hints when ``subtype_alias_suffix`` is set.

        Returns
        -------
        list[Column]
            List of Column objects describing the layer's fields.
        """
        columns = []
        properties = fl.properties
        for field in properties.fields:
            hints = []
            keyval = {}
            minmax = []
            dtype = self._esri_field_type_to_dtype(field.type)
            field_alias = getattr(field, "alias", None)
            self.message(f"{field.name=} {field.type=} alias={field_alias}")
            if dtype.lower() in self.exclude_types:
                continue
            if field.name.lower() in self.exclude_names:
                continue
            geom_props = getattr(properties, "geometryProperties", None)
            if geom_props is not None:
                if field.name == getattr(geom_props, "shapeLengthFieldName", None):
                    continue
                if field.name == getattr(geom_props, "shapeAreaFieldName", None):
                    continue
            alias = (field_alias or field.name).replace("_", " ")
            subtype_field = properties.get("typeIdField") or properties.get("subtypeField", "")
            if subtype_field and field.name.casefold() == subtype_field.casefold():
                for subtype in properties.get("types", []) or []:
                    code, label = subtype["id"], subtype["name"]
                    keyval[str(code)] = label
                    hints.append(coded_hint(
                        field.name, code, label, self.type_mapping.get(dtype),
                        suffix=layer_alias if self.subtype_alias_suffix else "",
                    ))
            if not keyval and field.domain and field.domain.type == "codedValue":
                for coded_value in field.domain.codedValues:
                    keyval[str(coded_value.code).strip()] = str(coded_value.name).strip()
                    hints.append(coded_hint(field.name, coded_value.code, coded_value.name, self.type_mapping.get(dtype)))
                    self.message(f"  Coded {coded_value.code} = {coded_value.name}")
            elif not keyval and field.domain and field.domain.type == "range":
                minmax = field.domain.range
                hints.append(f"Range is from {minmax[0]} to {minmax[1]}")
                self.message(f"  Range: {minmax[0]} to {minmax[1]}")

            try:
                if field.description:
                    hints.append(field.description)
            except (AttributeError, KeyError):
                pass

            columns.append(
                Column(
                    name=field.name,
                    alias=alias.lower(),
                    dtype=dtype,
                    minmax=minmax,
                    keyval=keyval,
                    hints=hints,
                )
            )
        return columns

    def _desc_columns(
            self,
            layer_name,
            desc,
            domains: dict[str, arcpy.da.Domain],
            hidden_fields=None,
    ) -> list[Column]:
        """Describe all columns in a local feature layer.

        Parameters
        ----------
        layer_name : str
            Name of the layer for reference.
        desc : arcpy.Describe
            ArcPy describe object for the layer.
        domains : dict[str, arcpy.da.Domain], optional
            Dictionary of domain objects keyed by domain name.
        hidden_fields : set[str] or None
            Case-folded CIM field names to exclude.

        Returns
        -------
        list[Column]
            List of Column objects describing the layer's fields.
        """
        columns = []
        for field in desc.fields:
            hints = []
            keyval = {}
            minmax = []
            self.message(f"{field.name=} {field.type=} {field.aliasName=}")
            if hidden_fields and field.name.casefold() in hidden_fields:
                continue
            if field.type.lower() in self.exclude_types:
                continue
            if field.name.lower() in self.exclude_names:
                continue
            if field.name == getattr(desc, "lengthFieldName", ""):
                continue
            if field.name == getattr(desc, "areaFieldName", ""):
                continue
            if field.name == getattr(desc, "globalIDFieldName", ""):
                continue
            alias = (field.aliasName or field.name).replace("_", " ")

            subtype_field = getattr(desc, "subtypeFieldName", "")
            if subtype_field and field.name.lower() == subtype_field.lower():
                subtypes = arcpy.da.ListSubtypes(desc.catalogPath)
                layer_alias = (getattr(desc, "aliasName", "") or layer_name).lower().replace("_", " ")
                for code, info in subtypes.items():
                    name = info["Name"]
                    keyval[str(code)] = name
                    hints.append(coded_hint(
                        field.name, code, name, self.type_mapping.get(field.type),
                        suffix=layer_alias if self.subtype_alias_suffix else "",
                    ))
                    self.message(f"  Subtype {code} = {name}")

            if field.domain and not keyval:
                domain = domains.get(field.domain)
                if domain and domain.domainType == "CodedValue":
                    for code, value in domain.codedValues.items():
                        keyval[str(code).strip()] = str(value).strip()
                        hints.append(coded_hint(field.name, code, value, self.type_mapping.get(field.type)))
                        self.message(f"  Coded {code} = {value}")
                elif domain and domain.domainType == "Range":
                    minmax = domain.range
                    hints.append(f"Range is from {minmax[0]} to {minmax[1]}")
                    self.message(f"  Range: {minmax[0]} to {minmax[1]}")

            columns.append(
                Column(
                    name=field.name,
                    alias=alias.lower(),
                    dtype=field.type,
                    minmax=minmax,
                    keyval=keyval,
                    hints=hints,
                )
            )
        return columns

    def _get_domains(
            self,
            desc,
    ) -> dict:
        """Return workspace domains, loading each geodatabase only once."""
        path = getattr(desc, "path", "") or ""
        if not path:
            return {}

        gdb_end = path.lower().find(".gdb")
        workspace = path[:gdb_end + 4] if gdb_end >= 0 else path
        if workspace not in self._domains_by_workspace:
            arcpy.AddMessage(f"Domains path: {workspace}")
            try:
                self._domains_by_workspace[workspace] = {
                    domain.name: domain for domain in arcpy.da.ListDomains(workspace)
                }
            except Exception as e:
                arcpy.AddWarning(f"Cannot list the domains for {path}. {e}")
                self._domains_by_workspace[workspace] = {}
        return self._domains_by_workspace[workspace]


    def _desc_values_web(
            self,
            feature_layer,
            layer_name,
            columns,
            def_query: str = "",
    ) -> None:
        """Extract and analyze sample values from a web-based feature layer.

        Parameters
        ----------
        feature_layer : arcgis.features.FeatureLayer
            The feature layer to query.
        layer_name : str
            Name of the layer for logging.
        columns : list[Column]
            List of Column objects to populate with values.
        def_query : str, optional
            Definition query to filter records (default: "").
        """
        arcpy.AddMessage(f"{layer_name=} {def_query=}")
        counters = [Counter() for _ in columns]
        try:
            s_columns = [_.name for _ in columns]
            # Cap the fetch so we never pull an entire service into memory; the server
            # also clamps to its own maxRecordCount.
            result = feature_layer.query(
                where=def_query or "1=1",
                out_fields=s_columns,
                return_geometry=False,
                result_record_count=self.max_records,
                return_all_records=False,
            )

            result_is_dict = isinstance(result, dict)
            features = result["features"] if result_is_dict else result.features
            records_read = 0
            for feature in islice(features, self.max_records):
                records_read += 1
                # Index by column name so each counter aligns with its column
                # regardless of the feature's attribute iteration order.
                attr = feature["attributes"] if result_is_dict else feature.attributes
                for counter, name in zip(counters, s_columns):
                    v = attr.get(name)
                    if v is not None:
                        v = self._round(v)
                        key = str(v).strip()
                        if key:
                            counter[key] += 1
            if records_read == 0:
                arcpy.AddWarning(f"No records found in {layer_name}. Check the query definition.")
        except RuntimeError as e:
            arcpy.AddWarning(f"Search Error on {layer_name}. Runtime error {e}")
        self._add_values_hints(columns, counters)

    def _desc_values(
            self,
            layer,
            layer_name,
            desc,
            columns,
    ) -> None:
        """Extract and analyze sample values from a local feature layer.

        Parameters
        ----------
        layer : arcpy mapping layer
            The layer object to query.
        layer_name : str
            Name of the layer for logging.
        desc : arcpy.Describe
            ArcPy describe object for the layer.
        columns : list[Column]
            List of Column objects to populate with values.
        """
        match self.max_method:
            case "TOP":
                sql_clause = (f"TOP {self.max_records}", "")
            case "LIMIT":
                sql_clause = ("", f"LIMIT {self.max_records}")
            case _:
                sql_clause = ("", "")
        if hasattr(layer, "definitionQuery"):
            where_clause = layer.definitionQuery or None
        else:
            where_clause = None
        arcpy.AddMessage(f"{where_clause=}")
        counters = [Counter() for _ in columns]
        try:
            s_columns = [_.name for _ in columns] if columns else "*"
            with arcpy.da.SearchCursor(
                    layer,
                    s_columns,
                    where_clause=where_clause,
                    sql_clause=sql_clause,
            ) as cursor:
                records_read = 0
                for r in islice(cursor, self.max_records):
                    records_read += 1
                    for counter, v in zip(counters, r):
                        if v is not None:
                            v = self._round(v)
                            key = str(v).strip()
                            if key:
                                counter[key] += 1

                if records_read == 0:
                    arcpy.AddWarning(f"No records found in {layer_name}. Check the query definition.")
        except RuntimeError as e:
            arcpy.AddWarning(f"Search Error on {layer_name}. Runtime error {e}")

        self._add_values_hints(columns, counters)

    def _get_gis_for_data_source(
            self,
            data_source: str,
    ) -> GIS:
        """Get GIS object for a given data source.

        If a service needs authentication, then we are expecting a JSON file with the following structure:
        {
            "https://gisportal.com/server/rest/services/foobar/MapServer": {
                "portal_url": "https://myportal.com/portal",
                "username": "foobar",
                "password": "foobar-secret-password",
            },
            ...
        }
        """
        if self._gis_dict is None:
            gis_cred_path = os.environ.get("GIS_CREDENTIALS_PATH")
            if gis_cred_path:
                gis_cred_path = os.path.realpath(gis_cred_path)
            if gis_cred_path and os.path.isfile(gis_cred_path):
                with open(gis_cred_path, "r", encoding="utf-8") as f:
                    self._gis_dict = json.load(f)
            else:
                arcpy.AddMessage(
                    "Env var GIS_CREDENTIALS_PATH is missing or referenced file is not found."
                    " Created an empty GIS dictionary."
                )
                self._gis_dict = {}
        if data_source not in self._gis_dict:
            arcpy.AddMessage(f"No GIS credentials found for {data_source}, returning GIS('home').")
            return GIS("home")
        gis_elem = self._gis_dict[data_source]
        if "gis" not in gis_elem:
            arcpy.AddMessage(f"Creating GIS object for {data_source}.")
            gis_elem["gis"] = GIS(
                gis_elem["portal_url"],
                gis_elem["username"],
                gis_elem["password"],
            )
        return gis_elem["gis"]

    def _desc_layer_web(
            self,
            layer,
            layer_name,
    ) -> None:
        """Process and describe a web-based layer completely.

        Parameters
        ----------
        layer : arcpy mapping layer
            The web layer to process.
        layer_name : str
            Name of the layer.
        """
        arcpy.AddMessage("=" * 60)
        arcpy.AddMessage(f"Processing WebLayer {layer_name}.")
        arcpy.AddMessage("=" * 60)

        cim = layer.getDefinition("V3")

        # `isWebLayer` is true for EVERY service-backed layer, but this branch
        # reads only the two that expose queryable sublayers -- a map service
        # and a feature service. A WMS, WMTS, tiled, image or vector-tile layer
        # reaches here as well, and its CIM is a different shape: a CIMWMSLayer's
        # children are CIMWMSSubLayer, which carries no `definitionExpression`,
        # so the loop below raised AttributeError and failed the whole tool
        # instead of skipping the one layer it could not read. None of them has
        # an attribute table to catalog anyway -- WMS answers GetFeatureInfo,
        # not a query, and a tile service serves pixels.
        def queryable_sublayers(parent, visible=True):
            for child in getattr(parent, "subLayers", None) or []:
                child_visible = visible and getattr(child, "visibility", True)
                if hasattr(child, "definitionExpression"):
                    yield child, child_visible
                yield from queryable_sublayers(child, child_visible)

        sub_layers = list(queryable_sublayers(cim))
        if not sub_layers:
            arcpy.AddWarning(
                f"Skipping {layer_name}: {type(cim).__name__} exposes no queryable"
                " sublayers, so there is nothing to describe."
            )
            return

        def_queries = {}
        viz_names = set()
        for sub_layer, visible in sub_layers:
            def_queries[sub_layer.name] = sub_layer.definitionExpression
            if visible:
                viz_names.add(sub_layer.name)

        # _get_gis_for_data_source lazily builds and caches the GIS per data source,
        # so always go through it (the cached "gis" key only exists after that call).
        self._gis = self._get_gis_for_data_source(layer.dataSource)
        flc = FeatureLayerCollection(layer.dataSource, self._gis)
        for fl in flc.layers:
            properties = fl.properties
            if properties.name in viz_names:
                arcpy.AddMessage(f"Processing {layer_name} {properties.name}.")
                alias = properties.name
                summary = properties.description
                hints = [summary] if summary else []
                columns = self._desc_columns_web(fl, layer_name, alias.lower().replace("_", " "))
                if columns:
                    stype = self._esri_shape_type_to_stype(properties.geometryType)
                    display_field = properties.get("displayField", "")
                    if any(display_field == column.name for column in columns):
                        display = display_field
                    else:
                        display = columns[0].name if columns else ""
                    def_query = def_queries.get(properties.name, "1=1")
                    self._desc_values_web(fl, layer_name, columns, def_query)
                    self._add_domains_to_values(columns)
                    self.layers.append(
                        Layer(
                            name=f"{layer_name}/{properties.name}",
                            uri=fl.url,
                            alias=alias.lower().replace("_", " "),
                            stype=stype,
                            display=display,
                            subtype=properties.get("typeIdField") or properties.get("subtypeField", ""),
                            columns=columns,
                            hints=hints,
                        )
                    )
                else:
                    arcpy.AddWarning(f"Skipping {properties.name} because columns do not exist.")

    def _desc_layer(
            self,
            layer,
            layer_name,
    ) -> None:
        """Process and describe a local feature layer completely.

        Parameters
        ----------
        layer : arcpy mapping layer
            The local layer to process.
        layer_name : str
            Name of the layer.
        """
        arcpy.AddMessage("=" * 60)
        arcpy.AddMessage(f"Processing FeatureLayer {layer_name}.")
        arcpy.AddMessage("=" * 60)

        # https://pro.arcgis.com/en/pro-app/latest/arcpy/mapping/python-cim-access.htm
        desc = arcpy.Describe(layer)
        cim = layer.getDefinition("V3")
        field_descriptions = cim.featureTable.fieldDescriptions or []
        hidden_fields = (
            {
                field_desc.fieldName.casefold()
                for field_desc in field_descriptions
                if not field_desc.visible
            }
            if field_descriptions
            else None
        )

        domains = self._get_domains(desc)

        columns = self._desc_columns(
            layer_name,
            desc,
            domains,
            hidden_fields=hidden_fields,
        )
        if not columns:
            arcpy.AddWarning(f"Skipping {layer_name} because columns do not exist.")
            return

        alias = desc.aliasName if hasattr(desc, "aliasName") else layer_name
        hints = []

        metadata = None
        try:
            metadata = layer.metadata
            if metadata.summary:
                hints.append(metadata.summary.strip())
        except AttributeError:
            pass
        except Exception as e:
            arcpy.AddMessage(f"Cannot get layer.metadata.summary for {layer_name}. {e}")

        if not hints:
            try:
                metadata = arcpy.metadata.Metadata(desc.catalogPath)
                if metadata.summary:
                    hints.append(metadata.summary)
            except Exception as e:
                arcpy.AddMessage(f"Cannot get arcpy.metadata.Metadata for {layer_name}. {e}")

        if not hints and desc.catalogPath.startswith("http"):
            try:
                url = os.path.dirname(desc.catalogPath) + "/info/metadata"
                resp = requests.get(url, timeout=(5, 30))
                resp.raise_for_status()
                root = _safe_fromstring(resp.content)
                for elem in root.findall(".//idPurp"):
                    if elem.text:
                        hints.append(elem.text)
            except Exception as e:
                arcpy.AddMessage(f"Cannot get /info/metadata for {layer_name}. {e}")

        if columns:
            # MGR
            if any(cim.featureTable.displayField == _.name for _ in columns):
                display = cim.featureTable.displayField
            else:
                display = columns[0].name if columns else ""

            # get column info
            try:
                if hasattr(desc, "catalogPath") and desc.catalogPath:
                    arcpy.AddMessage(f"Getting column metadata for {desc.catalogPath}.")
                    if metadata is None:
                        metadata = arcpy.metadata.Metadata(desc.catalogPath)
                    column_infos = self.extract_column_info_from_metadata(metadata.xml)
                    for column in columns:
                        if column.name in column_infos and column_infos[column.name].get("description"):
                            column.hints.append(column_infos[column.name]["description"])
            except Exception as e:
                arcpy.AddMessage(f"Cannot get column metadata for {layer_name}. {e}")

            self._desc_values(layer, layer_name, desc, columns)
            self._add_domains_to_values(columns)
            self.layers.append(
                Layer(
                    # Database table names are based on the layer's own name;
                    # longName is only a UI/filter path and includes group names.
                    name=layer.name,
                    # Same string DuckDBToolbox._collect_export_layers builds
                    # (and validates) for the table it creates — but only for
                    # layers that actually get exported. A hosted feature
                    # service reaches this function too (both flags are set);
                    # it has no table, so it stays service-only.
                    table_name=(
                        None if is_web_backed(layer) else layer.name.replace(" ", "_")
                    ),
                    uri=layer.dataSource,
                    alias=alias.lower().replace("_", " "),
                    stype=desc.shapeType,
                    display=display,
                    subtype=desc.subtypeFieldName,
                    columns=columns,
                    hints=hints,
                )
            )
        else:
            arcpy.AddWarning(f"Skipping {layer_name} because columns do not exist.")


    def extract_column_info_from_metadata(
            self,
            xml_string,
    ) -> dict:
        """Extract column information from XML metadata string.

        Parameters
        ----------
        xml_string : str
            XML metadata string.

        Returns
        -------
        dict: Dictionary containing column information."""

        try:
            return self.column_infos_from_root(_safe_fromstring(xml_string))
        except Exception as e:
            arcpy.AddError(f"Error parsing XML string: {e}")
            return {}

    def _desc_table(
            self,
            table,
            table_name,
    ) -> None:
        """Process and describe a standalone table.

        Parameters
        ----------
        table : arcpy mapping table
            The table object to process.
        table_name : str
            Name of the table.
        """
        arcpy.AddMessage("=" * 60)
        arcpy.AddMessage(f"Processing Table {table_name}.")
        arcpy.AddMessage("=" * 60)

        desc = arcpy.Describe(table)

        domains = self._get_domains(desc)

        columns = self._desc_columns(table_name, desc, domains)
        if not columns:
            arcpy.AddWarning(f"Skipping {table_name} because columns do not exist.")
            return

        alias = desc.aliasName if hasattr(desc, "aliasName") else table_name
        hints = []

        metadata = None
        try:
            metadata = arcpy.metadata.Metadata(desc.catalogPath)
            if metadata.summary:
                hints.append(metadata.summary)
        except Exception as e:
            arcpy.AddMessage(f"Cannot get arcpy.metadata.Metadata for {table_name}. {e}")

        display = columns[0].name

        try:
            if hasattr(desc, "catalogPath") and desc.catalogPath:
                arcpy.AddMessage(f"Getting column metadata for {desc.catalogPath}.")
                if metadata is None:
                    metadata = arcpy.metadata.Metadata(desc.catalogPath)
                column_infos = self.extract_column_info_from_metadata(metadata.xml)
                for column in columns:
                    if column.name in column_infos and column_infos[column.name].get("description"):
                        column.hints.append(column_infos[column.name]["description"])
        except Exception as e:
            arcpy.AddMessage(f"Cannot get column metadata for {table_name}. {e}")

        self._desc_values(table, table_name, desc, columns)
        self._add_domains_to_values(columns)
        self.layers.append(
            Layer(
                name=table_name,
                table_name=(
                    None if is_web_backed(table) else table_name.replace(" ", "_")
                ),
                uri=table.dataSource,
                alias=alias.lower().replace("_", " "),
                stype="Table",
                display=display,
                subtype=getattr(desc, "subtypeFieldName", ""),
                columns=columns,
                hints=hints,
            )
        )

    def _desc_layer_list(
            self,
            exclude_layers: list[str],
            include_layers: list[str],
    ):
        """Process all layers and tables in the current ArcGIS Pro project.

        Parameters
        ----------
        exclude_layers : list[str]
            List of layer/table names to exclude from processing.
        include_layers : list[str]
            List of layer/table names to include (if empty, all are included).
        """
        self.layers = []
        self._domains_by_workspace.clear()

        curr_proj = arcpy.mp.ArcGISProject("CURRENT")
        active_map = curr_proj.activeMap
        if active_map is None:
            raise RuntimeError("The current ArcGIS Pro project has no active map.")

        # Process feature layers and web layers
        for layer in active_map.listLayers():
            layer_name = layer.longName
            arcpy.SetProgressorLabel(f"Processing {layer_name}...")
            if arcpy.env.isCancelled:
                break

            if should_skip_layer(layer, layer_name, include_layers, exclude_layers):
                continue

            if layer.isFeatureLayer:
                self._desc_layer(layer, layer_name)
            elif layer.isWebLayer:
                self._desc_layer_web(layer, layer_name)

        # Process standalone tables
        for table in active_map.listTables():
            table_name = table.name
            arcpy.SetProgressorLabel(f"Processing {table_name}...")
            if arcpy.env.isCancelled:
                break

            if exclude_layers and table_name in exclude_layers:
                continue
            if include_layers and table_name not in include_layers:
                continue
            if hasattr(table, "isBroken") and table.isBroken:
                continue

            self._desc_table(table, table_name)

    def _execute(
            self,
            parameters,
    ) -> None:
        """Execute the main tool logic with given parameters.

        Parameters
        ----------
        parameters : list
            List of parameter values from the tool interface.
        """
        include_text = parameters[0].valueAsText
        exclude_text = parameters[1].valueAsText

        include_layers = parse_arcpy_multivalue(include_text)
        exclude_layers = parse_arcpy_multivalue(exclude_text)

        self.max_method = parameters[2].valueAsText
        self.max_records = parameters[3].value
        self.max_values = parameters[4].value
        self.use_ilike = parameters[6].value
        self.subtype_alias_suffix = bool(parameters[7].value)
        self._desc_layer_list(exclude_layers, include_layers)

    def execute(
            self,
            parameters,
            _,
    ) -> None:
        """Describe the selected layers/tables and write Layers.json (metadata only)."""
        arcpy.env.autoCancelling = False
        self._execute(parameters)
        if arcpy.env.isCancelled:
            return

        kb_path = parameters[5].valueAsText

        arcpy.SetProgressorLabel("Preparing metadata...")
        arcpy.AddMessage("=" * 60)

        os.makedirs(kb_path, exist_ok=True)

        layers = [layer.prune_columns() for layer in self.layers]
        layers = [layer for layer in layers if layer.has_columns]

        if layers:
            fel_layers = Layers(layers=layers)
            output_path = fel_layers.dump(kb_path)
            arcpy.AddMessage(f"Saved Layers.json to {output_path}.")
        else:
            arcpy.AddWarning("Did not find any feature layers to process :-(")

    def postExecute(
            self,
            parameters,
    ):
        """Perform any post-execution cleanup or processing.

        Parameters
        ----------
        parameters : list
            List of parameter values from the tool interface.
        """
        return


# ---------------------------------------------------------------------------
# Tool: Enable CIM on Layers
# ---------------------------------------------------------------------------
class CIMTool:
    def __init__(
            self,
    ):
        self.label = "00 - Enable CIM on Layers"
        self.description = "Enable CIM on Layers"

    def getParameterInfo(
            self,
    ):
        include_layer = arcpy.Parameter(
            displayName="Include Layer(s)",
            name="include_layer",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
            multiValue=True
        )

        exclude_layer = arcpy.Parameter(
            displayName="Exclude Layer(s)",
            name="exclude_layer",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
            multiValue=True
        )

        return [
            include_layer,
            exclude_layer,
        ]

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

    def execute(
            self,
            parameters,
            messages,
    ):
        arcpy.env.autoCancelling = False

        include_text = parameters[0].valueAsText
        exclude_text = parameters[1].valueAsText

        include_layers = parse_arcpy_multivalue(include_text)
        exclude_layers = parse_arcpy_multivalue(exclude_text)

        curr_proj = arcpy.mp.ArcGISProject("CURRENT")
        layer_list = curr_proj.activeMap.listLayers()
        arcpy.SetProgressor("step", "CIM...", 0, len(layer_list), 1)
        try:
            for pos, layer in enumerate(layer_list):
                arcpy.SetProgressorPosition(pos)
                if layer.isGroupLayer or layer.isBroken or layer.isBasemapLayer:
                    continue
                if exclude_layers and layer.longName in exclude_layers:
                    continue
                if include_layers and layer.longName not in include_layers:
                    continue
                if not layer.isFeatureLayer:
                    continue
                if arcpy.env.isCancelled:
                    break

                desc = arcpy.Describe(layer)
                layer_name = layer.longName
                arcpy.SetProgressorLabel(layer_name)
                try:
                    arcpy.management.AlterField(
                        in_table=layer,
                        field=desc.OIDFieldName,
                        clear_field_alias="CLEAR_ALIAS"
                    )
                except arcpy.ExecuteError as e:
                    arcpy.AddWarning(f"Skipping {layer_name} - {str(e)}")
        finally:
            arcpy.ResetProgressor()


# ---------------------------------------------------------------------------
# Toolbox
# ---------------------------------------------------------------------------
class Toolbox:
    def __init__(
            self,
    ):
        self.label = "Layers"
        self.alias = "Layers"
        self.tools = [
            CIMTool,
            HideUpdateTool,
            SetDefinitionQuery,
            PrepareTool,
        ]
