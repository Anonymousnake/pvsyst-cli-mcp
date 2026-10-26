"""Compatibility launcher for existing source-checkout MCP configurations."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from pvsyst_auto.api.app import main

if __name__ == "__main__":
    main()
