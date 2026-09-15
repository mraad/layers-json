"""Shared Hide/Update rules; independent of ArcPy, GDAL, and command entry points."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Rules:
    include: frozenset[str]
    exclude: frozenset[str]
    hide: tuple[re.Pattern[str], ...]
    aliases: tuple[tuple[re.Pattern[str], str], ...]

    def includes_layer(self, name: str) -> bool:
        return name not in self.exclude and (not self.include or name in self.include)

    def to_config(self) -> dict[str, Any]:
        return {
            "include_layers": sorted(self.include),
            "exclude_layers": sorted(self.exclude),
            "exclude_fields": [pattern.pattern for pattern in self.hide],
            "field_aliases": [
                [pattern.pattern, replacement] for pattern, replacement in self.aliases
            ],
        }


@dataclass(frozen=True)
class Schema:
    visible: frozenset[str]
    hidden: frozenset[str]
    fields: tuple[tuple[str, str], ...] = ()


UNSUPPORTED_FIELD_TYPES = frozenset({"blob", "guid", "raster", "xml"})


def build_schema(visible, hidden, fields) -> Schema:
    """Classify fields once for both adapters.

    ``visible``/``hidden`` are role field names (OID, shape, subtype / area, length,
    global ID); empty names are ignored. ``fields`` are ``(name, alias, type)`` rows;
    unsupported types are hidden. Types may carry the ``esriFieldType`` prefix.
    """
    hidden_keys = {name.casefold() for name in hidden if name}
    rows = []
    for name, alias, field_type in fields:
        if not name:
            continue
        rows.append((name, alias or name))
        if (field_type or "").casefold().removeprefix("esrifieldtype") in UNSUPPORTED_FIELD_TYPES:
            hidden_keys.add(name.casefold())
    return Schema(
        frozenset(name.casefold() for name in visible if name),
        frozenset(hidden_keys),
        tuple(rows),
    )


@dataclass(frozen=True)
class LayerChange:
    name: str
    hidden: tuple[str, ...]
    aliases: tuple[tuple[str, str], ...]


def _string_list(config: dict[str, Any], key: str) -> list[str]:
    values = config.get(key, [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{key} must be a list of strings")
    return values


def rules_from_config(config: dict[str, Any]) -> Rules:
    """Validate and compile the configuration used by both entry points."""
    if not isinstance(config, dict):
        raise ValueError("rules must contain a JSON object")

    try:
        hide = tuple(
            re.compile(value, re.IGNORECASE)
            for value in _string_list(config, "exclude_fields")
            if value
        )
    except re.error as exc:
        raise ValueError(f"invalid exclude_fields regex: {exc}") from exc

    raw_aliases = config.get("field_aliases", [])
    if not isinstance(raw_aliases, list):
        raise ValueError("field_aliases must be a list of [pattern, replacement] pairs")
    aliases = []
    for row in raw_aliases:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or not all(isinstance(value, str) for value in row)
        ):
            raise ValueError("field_aliases must be a list of [pattern, replacement] pairs")
        if not row[0] or not row[1]:
            continue
        try:
            pattern = re.compile(row[0], re.IGNORECASE)
            pattern.sub(row[1], "")  # Validate backreferences even when no field matches.
            aliases.append((pattern, row[1]))
        except (re.error, IndexError) as exc:
            raise ValueError(f"invalid field_aliases regex {row[0]!r}: {exc}") from exc

    return Rules(
        include=frozenset(_string_list(config, "include_layers")),
        exclude=frozenset(_string_list(config, "exclude_layers")),
        hide=hide,
        aliases=tuple(aliases),
    )


def load_rules(path: Path) -> Rules:
    """Read the shared HideUpdateTool.json configuration."""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    return rules_from_config(config)


def apply_rules(doc: dict[str, Any], layer_name: str, schema: Schema, rules: Rules) -> LayerChange:
    """Apply the original toolbox's field precedence to one CIM layer document."""
    hidden: list[str] = []
    aliases: list[tuple[str, str]] = []
    table = doc.setdefault("featureTable", {})
    fields = table.get("fieldDescriptions") or []
    if not fields:
        if not schema.fields:
            raise ValueError(f"No field metadata available for {layer_name}")
        fields = [
            {"type": "CIMFieldDescription", "fieldName": name, "alias": alias, "visible": True}
            for name, alias in schema.fields
        ]
        table["fieldDescriptions"] = fields
    for field in fields:
        name = field.get("fieldName") or ""
        key = name.casefold()
        if key in schema.hidden:
            if field.get("visible", True):
                hidden.append(name)
            field["visible"] = False
            continue
        if key in schema.visible:
            field["visible"] = True
            continue
        if not field.get("visible", True):
            continue

        full_name = f"{layer_name}.{name}"
        if any(pattern.match(full_name) for pattern in rules.hide):
            field["visible"] = False
            hidden.append(name)
            continue

        for pattern, replacement in rules.aliases:
            if not pattern.match(full_name):
                continue
            try:
                alias = pattern.sub(replacement, full_name).replace("_", " ").title()
            except re.error as exc:
                raise ValueError(f"invalid alias replacement for {full_name}: {exc}") from exc
            if field.get("alias") != alias:
                field["alias"] = alias
                aliases.append((name, alias))
            field["visible"] = True
            break
    return LayerChange(layer_name, tuple(hidden), tuple(aliases))
