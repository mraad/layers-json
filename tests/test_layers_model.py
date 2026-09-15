"""The `model` extra is pydantic behind a wall: `layers_json.layers` may import it,
the CLI may not. `uvx layers-json` installs one wheel and that has to keep working.
"""

from __future__ import annotations

import importlib
import sys

import pytest

CLI_MODULES = (
    "layers_json.layers_from_aprx",
    "layers_json.okf_from_aprx",
    "layers_json.hide_update_aprx",
)


class _BlockPydantic:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "pydantic":
            raise ImportError("pydantic is not installed")
        return None


@pytest.mark.parametrize("name", CLI_MODULES)
def test_cli_imports_without_pydantic(name: str) -> None:
    dropped = {k: v for k, v in sys.modules.items() if k.split(".")[0] == "pydantic"}
    for key in (*dropped, name):
        sys.modules.pop(key, None)
    sys.meta_path.insert(0, _BlockPydantic())
    try:
        importlib.import_module(name)
    finally:
        sys.meta_path.pop(0)
        sys.modules.update(dropped)


def test_layers_model_round_trips_a_toolbox_dump(tmp_path) -> None:
    """The producer writes, the shared consumer model reads. One shape, two halves."""
    pytest.importorskip("pydantic")
    from layers_json import catalog
    from layers_json.layers import Layers

    toolbox = catalog
    dumped = toolbox.Layers(
        layers=[
            toolbox.Layer(
                name="Wells",
                table_name="Wells",
                alias="wells",
                stype="Point",
                uri="/data/NorthSea.gdb/Wells",
                columns=[toolbox.Column(name="STATUS", alias="status", dtype="Integer")],
            )
        ]
    ).dump(str(tmp_path))

    wells = Layers.load(dumped).find_layer("wells")
    assert wells is not None
    assert wells.stype == "Point"
    assert wells.sql_table == "Wells"
    assert wells.columns[0].name == "STATUS"


def test_layers_iterates_layers_not_pydantic_fields() -> None:
    """schema-rendering consumers do `for layer in catalog`; pydantic's default
    __iter__ would hand them ("layers", [...]) tuples instead."""
    pytest.importorskip("pydantic")
    from layers_json.layers import Layer, Layers

    catalog = Layers(layers=[Layer(name="A"), Layer(name="B")])
    assert [layer.name for layer in catalog] == ["A", "B"]
    assert len(catalog) == 2
    # serialization must not have been collateral damage
    assert '"name":"A"' in catalog.model_dump_json()
