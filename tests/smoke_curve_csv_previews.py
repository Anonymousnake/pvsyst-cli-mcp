"""Read-only CSV preview comparison with previously simulated point tables.

Set PVSYST_CURVE_TABLE_EVIDENCE to evidence.json from a successful
smoke_live_curve_tables.py run whose workspace copies have been restored.
Checks that CSV and array inputs produce the exact native-tested component
SHA-256 for every recorded run, without editing files or running simulations.
"""
import csv
import hashlib
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvsyst_components import ComponentStore
from pvsyst_curve_csv import HEADERS


def main():
    evidence = json.loads(Path(os.environ["PVSYST_CURVE_TABLE_EVIDENCE"]).read_text(encoding="utf-8"))
    assert evidence["source_unchanged"] and evidence["cases"]
    checks = []
    for case in evidence["cases"]:
        assert case["native_curve_consumption_verified"]
        config = case["configuration"]
        kind, name = config["kind"], config["filename"]
        for run_name, run in case["runs"].items():
            assert case["restored_exactly"][run_name]
            csv_path = Path(run["csv"])
            assert hashlib.sha256(csv_path.read_bytes()).hexdigest() == run["csv_sha256"]
            store = ComponentStore(csv_path.parent.parent)
            info = store.inspect(kind, name)
            assert info["sha256"] == run["component_before_sha256"]
            curve = "Converter/ProfilPIO" if kind == "OND" else info["curves"]["control"]["path"]
            points = config["points"][run_name]
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(HEADERS[kind, curve])
            writer.writerows(points)
            args = {"expected_sha256": info["sha256"], "dry_run": True, "use_file_curve": True}
            array_preview = store.update(kind, name, {}, curve_updates={curve: points}, **args)
            csv_preview = store.update(kind, name, {}, curve_csv={curve: stream.getvalue()}, **args)
            assert array_preview == csv_preview
            assert csv_preview["sha256"] == run["component_sha256"]
            assert store.inspect(kind, name)["sha256"] == info["sha256"]
            checks.append({"kind": kind, "run": run_name, "native_tested_sha256": run["component_sha256"],
                           "csv_array_previews_identical": True, "component_unchanged": True})
    print(json.dumps({"checks": checks, "new_simulations": 0}, indent=2))


if __name__ == "__main__":
    main()
