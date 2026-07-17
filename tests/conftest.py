"""Test package setup without importing Home Assistant integration bootstrap code."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[1]
COMPONENTS = ROOT / "custom_components"
INTEGRATION = COMPONENTS / "dji_romo"

custom_components = ModuleType("custom_components")
custom_components.__path__ = [str(COMPONENTS)]
sys.modules.setdefault("custom_components", custom_components)

dji_romo = ModuleType("custom_components.dji_romo")
dji_romo.__path__ = [str(INTEGRATION)]
sys.modules.setdefault("custom_components.dji_romo", dji_romo)
