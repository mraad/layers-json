"""Shared catalog serialization, metadata parsing, and query hints (standard library only)."""

import datetime as dt
import json
import os
import tempfile
import xml.etree.ElementTree as ET
import xml.parsers.expat
from collections import Counter
from contextlib import suppress
from typing import List


def coded_hint(
    field_name,
    code,
    label,
    cast_as=None,
    suffix="",
):
    """One WHERE-fragment hint for a coded value -- a subtype code or a coded domain.

    ``cast_as`` supplies the SQL type from PrepareTool.type_mapping, so every
    numeric code includes its cast and string codes are quoted and escaped.
    Omitting it preserves the legacy three-argument format.

    ``suffix`` is the layer alias, appended to a subtype label so the phrase
    names the feature the way a user would ask for it -- ``oil discoveries``,
    ``dry wells`` -- when the same labels recur across layers. A label that
    already ends with the alias is left alone.

    Both cases are the same sentence: this code means that phrase, and here is
    the filter that selects it. Naming the column inside the quotes makes the
    hint pasteable (``PIPETYPE=3``) rather than something the reader has to
    reassemble from the surrounding JSON, and it keeps subtypes and domains on
    one grammar instead of two.

    The label is the noun phrase a user would actually say, so ``/`` becomes
    ``or`` -- "Water/Sewer" reads as two choices, not a path -- and the whole
    thing lowercases so it cannot be mistaken for a second identifier. Coined by
    an earlier toolbox; carried here so both writers emit the same bytes.

    ``split()`` rather than a single ``replace("  ", " ")``: substituting a
    padded slash leaves a run of three spaces, and one non-overlapping pass only
    takes two of them back out.
    """
    label = " ".join(str(label).replace("/", " or ").split()).lower()
    suffix = " ".join(str(suffix).split()).lower()
    if suffix and not label.endswith(suffix):
        label = f"{label} {suffix}"
    if cast_as == "VARCHAR":
        value = "'" + str(code).replace("'", "''") + "'"
    elif cast_as:
        value = f"cast({code} as {cast_as})"
    else:
        return f"Use '{field_name}={code}' for '{label}'."
    return f"Use '{field_name} = {value}' for '{label}'."


def _safe_fromstring(
    xml_bytes,
):
    """Parse XML with DOCTYPE/ENTITY declarations rejected.

    Mitigates XXE (XML External Entity) and billion-laughs attacks by
    using :mod:`xml.parsers.expat` directly to reject real DOCTYPE and
    ENTITY declarations at the parser level.  Unlike a substring scan
    this only fires on actual DTD syntax, not on occurrences inside
    comments, CDATA sections, or text content.  Expat handles all XML
    encodings (UTF-8/16/32, with or without BOM) natively.

    :param xml_bytes: XML content as bytes or str.
    :return: Root :class:`xml.etree.ElementTree.Element`.
    :raises ValueError: If the document contains a DOCTYPE or ENTITY declaration,
        or is not well-formed XML.
    """
    raw = xml_bytes if isinstance(xml_bytes, bytes) else xml_bytes.encode("utf-8")

    # Validate with a raw expat parser -- handlers trigger only on real DTD syntax.
    checker = xml.parsers.expat.ParserCreate()

    def _reject_doctype(
        *_args,
        **_kwargs,
    ):
        raise ValueError("DOCTYPE declarations are not allowed")

    def _reject_entity(
        *_args,
        **_kwargs,
    ):
        raise ValueError("ENTITY declarations are not allowed")

    checker.StartDoctypeDeclHandler = _reject_doctype
    checker.EntityDeclHandler = _reject_entity
    try:
        checker.Parse(raw, True)
        return ET.fromstring(xml_bytes)
    except (xml.parsers.expat.ExpatError, ET.ParseError) as exc:
        raise ValueError(f"invalid XML: {exc}") from exc


class Column:
    """A single field description serialized into Layers.json."""

    def __init__(
        self,
        name,
        alias,
        dtype,
        utype=None,
        minmax=None,
        keyval=None,
        hints=None,
        values=None,
    ):
        self.name = name
        self.alias = alias
        self.dtype = dtype
        self.utype = utype
        self.minmax = minmax if minmax is not None else []
        self.keyval = keyval if keyval is not None else {}
        self.hints = hints if hints is not None else []
        self.values = values if values is not None else []

    def to_dict(
        self,
    ):
        # Field order matches the catalog field contract.
        return {
            "name": self.name,
            "alias": self.alias,
            "dtype": self.dtype,
            "utype": self.utype,
            "minmax": self.minmax,
            "keyval": self.keyval,
            "hints": self.hints,
            "values": self.values,
        }


class Layer:
    """A layer/table description serialized into Layers.json."""

    def __init__(
        self,
        name,
        alias,
        stype,
        uri,
        display=None,
        subtype=None,
        columns=None,
        hints=None,
        table_name=None,
    ):
        self.name = name
        self.alias = alias
        self.stype = stype
        self.uri = uri
        self.display = display
        self.subtype = subtype
        self.columns = columns if columns is not None else []
        self.hints = hints if hints is not None else []
        # Physical DB table, set ONLY for sources the loaders actually export
        # (map feature layers and standalone tables). Web-service layers must
        # leave it None: emitting it would mark them database-backed, and the
        # agent would be handed a SELECT against a table that does not exist.
        self.table_name = table_name

    @property
    def has_columns(
        self,
    ):
        """True if the layer has at least one column."""
        return bool(self.columns)

    def prune_columns(
        self,
        min_len_values=1,
    ):
        """Return a copy keeping only columns that have collected values.

        ``subtype`` is carried over so the pruned copy retains the layer's
        subtype field.
        """
        columns = [c for c in self.columns if len(c.values) >= min_len_values]
        return Layer(
            name=self.name,
            alias=self.alias,
            stype=self.stype,
            uri=self.uri,
            display=self.display,
            subtype=self.subtype,
            columns=columns,
            hints=self.hints,
            table_name=self.table_name,
        )

    def to_dict(
        self,
    ):
        # Field order matches the catalog field contract, with table_name
        # inserted after name (the field it derives from).
        #
        # table_name identifies the physical database table;
        # `alias` is search/display text only (it comes from desc.aliasName, a
        # different property than `name`, and its "_" -> " " mapping is lossy).
        # Emitting it also marks the layer DB-backed: _is_db_layer() treats a
        # layer carrying only a `uri` as service-only and skips enrichment —
        # which is why it is omitted entirely for web-service layers rather
        # than emitted as None.
        #
        # Mixed case is deliberate — the same catalog serves three dialects.
        # PostGIS lowercases it, Oracle uppercases at query time, DuckDB needs
        # it verbatim. Matches DuckDBToolbox._collect_export_layers, which
        # already computes this exact string and refuses to export when a layer
        # name cannot map to it cleanly.
        out = {"name": self.name}
        if self.table_name:
            out["table_name"] = self.table_name
        out.update(
            {
                "alias": self.alias,
                "stype": self.stype,
                "display": self.display,
                "subtype": self.subtype,
                "columns": [c.to_dict() for c in self.columns],
                "hints": self.hints,
                "uri": self.uri,
            }
        )
        return out


class Layers:
    """Top-level Layers.json container."""

    def __init__(
        self,
        layers=None,
    ):
        self.layers = layers if layers is not None else []

    def dump(
        self,
        filename,
        indent=2,
    ):
        """Write Layers.json. If ``filename`` is a directory, append Layers.json.

        Writes UTF-8, raw (non-escaped)
        unicode, no trailing newline.
        """
        filename = os.path.expanduser(filename)
        if os.path.isdir(filename):
            filename = os.path.join(filename, "Layers.json")
        target_dir = os.path.dirname(os.path.abspath(filename))
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="UTF-8",
                dir=target_dir,
                prefix=".Layers.",
                suffix=".tmp",
                delete=False,
            ) as fp:
                temp_name = fp.name
                json.dump(
                    {"layers": [layer.to_dict() for layer in self.layers]},
                    fp,
                    indent=indent,
                    ensure_ascii=False,
                )
            os.replace(temp_name, filename)
            temp_name = None
        finally:
            if temp_name:
                with suppress(OSError):
                    os.unlink(temp_name)
        return filename


class CatalogBuilder:
    """Settings and transformations shared by ArcPy and GDAL readers."""

    def __init__(
        self,
    ):
        """Initialize the PrepareTool instance with default settings."""
        self.layers = []
        self.exclude_types = (
            "oid",
            "guid",
            "globalid",
            "geometry",
            "blob",
            "raster",
            "xml",
        )
        self.exclude_names = (
            "globalid",
            "shape",
            "shape_length",
            "shape_area",
            "shape__length",
            "shape__area",
            "st_area(shape)",
            "st_perimeter(shape)",
            "createdby",
            "createddate",
            "editedby",
            "editeddate",
            "modifiedby",
            "modifieddate",
            "created_by",
            "created_date",
            "edited_by",
            "edited_date",
            "modified_by",
            "modified_date",
            "lon",
            "lat",
            "longitude",
            "latitude",
            "point_x",
            "point_y",
        )
        self.type_mapping = {
            "String": "VARCHAR",
            "SmallInteger": "SMALLINT",
            "Integer": "INTEGER",
            "BigInteger": "BIGINT",
            "Single": "REAL",
            "Double": "DOUBLE PRECISION",
            "Float": "FLOAT",
            "Date": "DATE",
            "DateTime": "TIMESTAMP",
            "Boolean": "BOOLEAN",
            "Geometry": "GEOMETRY",
        }
        self.max_method = "COUNT"
        self.max_records = 20000
        self.max_values = 20
        self.use_ilike = False  # When True, emit ILIKE hints instead of LIKE
        self.subtype_alias_suffix = (
            False  # When True, subtype hints read 'oil discoveries', not 'oil'
        )
        self._round_range = 7
        self.message = lambda text: None

    def _esri_field_type_to_dtype(
        self,
        esri_field_type: str,
    ) -> str:
        """Convert ESRI field type to simplified data type string.

        Parameters
        ----------
        esri_field_type : str
            The ESRI field type (e.g., "esriFieldTypeString").

        Returns
        -------
        str
            Simplified data type string (e.g., "String", "Integer").
        """
        return {
            "esriFieldTypeBigInteger": "BigInteger",
            "esriFieldTypeDate": "Date",
            "esriFieldTypeDateOnly": "DateOnly",
            "esriFieldTypeTimeOnly": "TimeOnly",
            "esriFieldTypeTimestampOffset": "TimestampOffset",
            "esriFieldTypeDouble": "Double",
            "esriFieldTypeGUID": "GUID",
            "esriFieldTypeGeometry": "Geometry",
            "esriFieldTypeGlobalID": "GlobalID",
            "esriFieldTypeInteger": "Integer",
            "esriFieldTypeOID": "OID",
            "esriFieldTypeSingle": "Single",
            "esriFieldTypeSmallInteger": "SmallInteger",
            "esriFieldTypeString": "String",
        }.get(esri_field_type, esri_field_type)

    def _esri_shape_type_to_stype(
        self,
        esri_shape_type: str,
    ) -> str:
        """Convert ESRI shape type to simplified data type string.

        Parameters
        ----------
        esri_shape_type : str
            The ESRI shape type (e.g., "esriGeometryPoint", "esriGeometryPolyline", "esriGeometryPolygon").

        Returns
        -------
        str
            Simplified data type string (e.g., "Point", "Polyline" or "Polygon").
        """
        return {
            "esriGeometryPoint": "Point",
            "esriGeometryPolyline": "Polyline",
            "esriGeometryPolygon": "Polygon",
            "esriGeometryMultipoint": "Multipoint",
        }.get(esri_shape_type, esri_shape_type)

    def _add_domains_to_values(
        self,
        columns: list[Column],
    ) -> None:
        """Add domains to columns values.

        :param columns: List of columns to add domains to.
        :type columns: List[Column]
        """
        for column in columns:
            # Preserve observed frequency order and deterministic domain order.
            column.values = list(dict.fromkeys([*column.values, *column.keyval.keys()]))

    def _round(
        self,
        v,
    ):
        """Normalize sampled dates/times to seconds and round numeric artifacts.

        Truncate fractional seconds before counting distinct values, preserving
        timezone offsets and leaving date-only values unchanged.
        """
        if isinstance(v, (dt.datetime, dt.time)):
            return v.replace(microsecond=0)
        if isinstance(v, float) and v != 0.0:
            r = v
            for n in range(self._round_range):
                r = round(v, n)
                if abs((v - r) / v) < 1.0e-6:
                    break
            return r
        return v

    def _add_values_hints(
        self,
        columns: List[Column],
        counters: List[Counter],
    ) -> None:
        """ "Add hints to columns.

        :param columns: list of Column.
        :param counters: list of Counters.
        """
        op = "ILIKE" if self.use_ilike else "LIKE"
        for column, counter in zip(columns, counters):
            # Add the most common values.
            column.values.extend([v for (v, _) in counter.most_common(self.max_values)])
            len_col_val = len(column.values)
            value = "value" if len_col_val == 1 else "values"
            self.message(f"Column {column.name} has {len_col_val} sampled {value}.")
            if len_col_val == 0:
                continue
            if "Integer" in column.dtype or column.dtype in ("Single", "Double", "Float"):
                cast_as = self.type_mapping.get(column.dtype, column.dtype)
                column.hints.append(
                    f"ALWAYS use SQL CAST AS {cast_as} when comparing a value with the column {column.name}."
                )
                if not column.keyval:
                    text = column.values[0]
                    column.hints.append(
                        coded_hint(
                            column.name,
                            text,
                            f"{column.alias or column.name} is {text}",
                            cast_as,
                        )
                    )
            elif column.dtype in (
                "Date",
                "DateOnly",
                "DateTime",
                "Timestamp",
                "TimestampOffset",
                "TimeOnly",
            ):
                # Date/time values require dialect-specific literals and should
                # never receive the generic string LIKE guidance below.
                continue
            # Check if all the values are in upper case or in lower case
            elif column.keyval and column.dtype.lower() == "string":
                # If there are coded values, don't use like query
                column.hints.append(
                    f"NEVER use SQL LIKE in the WHERE clause for the values of the column {column.name}."
                )
            elif all(v.isupper() for v in column.values):
                text = column.values[0]
                column.hints.append(
                    f"Make sure to uppercase the compared values of the column {column.name} in the where clause."
                )
                column.hints.append(
                    # TODO - should we use the upper function on the value ?
                    f"For example: given the value >>{text.lower()}<< becomes >>{text}<<"
                )
            elif all(v.islower() for v in column.values):
                text = column.values[0]
                column.hints.append(
                    f"Make sure to lowercase the compared values of the column {column.name} in the where clause."
                )
                column.hints.append(
                    # TODO - should we use the lower function on the value ?
                    f"For example: given the value >>{text.upper()}<< becomes >>{text}<<"
                )
            else:
                column.hints.append(
                    f"Make sure to use the SQL {op} in the WHERE clause for the values of the column {column.name}."
                )
                line = column.values[0]
                text = line.split(" ")[0]
                column.hints.append(
                    f"For example: given >>{column.alias} is {text.lower()}<< becomes >>{column.name} {op} '%{text}%'<<"
                )

    @staticmethod
    def column_infos_from_root(
        root,
    ) -> dict:
        """The ``//attr`` map from an already-parsed metadata tree.

        ``extract_column_info_from_metadata`` parses then calls this. The
        off-Pro reader already parsed the blob for ``idPurp``, so it calls
        this directly rather than paying ``_safe_fromstring`` a second time.
        """
        columns = {}
        for attr in root.findall(".//attr"):
            columns[attr.findtext("attrlabl")] = {
                "name": attr.findtext("attrlabl"),
                "type": attr.findtext("attrtype"),
                "description": attr.findtext("attrdef"),
            }
        return columns
