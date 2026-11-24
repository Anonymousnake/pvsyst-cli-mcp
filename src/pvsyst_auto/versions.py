"""Native PVsyst versions this adapter accepts, kept in one place.

Every value is an explicit allow-list, never a version prefix: a release is added only
after its saved records and CLI behaviour have been checked.
Unknown versions keep failing closed in each editor's own structural gate.
"""
from __future__ import annotations

# Declared `Version=` of text records that exact-profile editors rewrite in place
# (losses, energy management, orientations, curves, commercial forms, ...).
NATIVE_TEXT: tuple[str, ...] = ("8.1.6", "8.1.7")

# Editors that also accept the 8.1.0 records shipped as DataRO demo/seed variants.
NATIVE_TEXT_OR_SEED: tuple[str, ...] = ("8.1.0",) + NATIVE_TEXT

# Version token written into new shading/scene records by the headless importer.
HEADLESS_RECORD_VERSION = "8.1.6"

# Version token written into new ground-surface (pvShdGround) records matching the seed grammar.
SEED_RECORD_VERSION = NATIVE_TEXT_OR_SEED[0]

# Version token emitted by zero-template pure-text component, project, and shading synthesizers.
SYNTH_VERSION = NATIVE_TEXT[-1]

# Stored osPVCollada PV-table declarations whose grammar the scene reader maps.
PV_TABLE_SOURCES = frozenset({("osPVCollada", "8.1.6"), ("osPVCollada", "8.1.7")})

# `PVsystCLI version` lines accepted by the tracking simulation/revision flows.
CLI_TRACKING_BUILDS: tuple[str, ...] = (
    "PVsystCLI 8.1.6 (rev. 49278)",
    "PVsystCLI 8.1.7 (rev. 49382)",
)

TEXT_LABEL = "/".join(NATIVE_TEXT)
SEED_LABEL = "/".join(NATIVE_TEXT_OR_SEED)
