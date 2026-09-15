"""Load Pro adapters with test doubles; never used by application code."""

import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

TOOLBOXES = Path(__file__).resolve().parents[1] / "layers_json" / "toolboxes"


# ---------------------------------------------------------------------------
# Load the toolbox behind an arcpy stub
# ---------------------------------------------------------------------------
def load_toolbox(path: str | Path = TOOLBOXES / "Layers.pyt"):
    """Import a ``.pyt`` with its ArcGIS-only imports stubbed out.

    Only the module-level definitions matter — nothing that actually touches
    arcpy is called.
    """
    path = Path(path)
    name = f"_{path.stem}_toolbox"  # so a traceback names the right .pyt
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)

    arcpy = types.ModuleType("arcpy")
    arcpy.Parameter = object
    arcpy.da = types.SimpleNamespace(Domain=object)
    arcpy.AddMessage = lambda *_a, **_k: None
    arcpy.AddWarning = lambda *_a, **_k: None
    arcpy.AddError = lambda *_a, **_k: None
    arcgis = types.ModuleType("arcgis")
    arcgis.GIS = object
    arcgis_features = types.ModuleType("arcgis.features")
    arcgis_features.FeatureLayerCollection = object
    requests = types.ModuleType("requests")
    requests.get = lambda *_a, **_k: None

    stubs = {
        "arcpy": arcpy,
        "arcgis": arcgis,
        "arcgis.features": arcgis_features,
        # Stubbed rather than declared as dependencies: Layers.pyt imports
        # `requests` for the web-layer branch this tool does not use, and
        # DuckDBToolbox.pyt imports `duckdb` for an export nothing here calls.
        # Neither is reached by the module-level definitions we are after.
        "requests": requests,
        "duckdb": types.ModuleType("duckdb"),
    }
    with patch.dict(sys.modules, stubs):
        loader.exec_module(module)
    return module
