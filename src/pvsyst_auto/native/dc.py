"""Read-only DC cabling state helper (delegates to headless pvsyst_auto.energy.dc)."""
from __future__ import annotations

from pvsyst_auto.energy.dc import create_dc_cabling_variant, dc_cabling_state

__all__ = ["dc_cabling_state", "create_dc_cabling_variant"]
