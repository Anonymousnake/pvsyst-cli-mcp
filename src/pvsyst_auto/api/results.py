"""Results MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError
from pvsyst_auto.native.cli import (is_batch_summary_name, parse_batch_results,
                                    read_result_page, summarize_results)
from pvsyst_auto.results import compare_results
from pvsyst_auto.api.context import categorized_tool, result_path, workspace_path


@categorized_tool()
def pvsyst_read_results(csv_name: str, columns: str = "E_Grid,PR",
                        folder: str = "Results", compare_to: str = "",
                        compare_folder: str | None = None) -> dict:
    """Summarize hourly or subhour SFI rows in Results or UserHourly.
    Complete energy requires finite samples and regular cadence; observed energy excludes missing samples.
    With compare_to, return target (csv_name) minus baseline (compare_to), using
    1..64 distinct columns, identical explicit units and identical increasing
    timestamps. compare_folder defaults to folder. No resampling/conversion.
    Statistics use finite pairs only; missing pairs and coverage are explicit.
    Power differences in W/kW integrate to kWh only at a regular cadence.
    Comparison verifies both file hashes before/after reading; it runs no simulation."""
    requested = tuple(column.strip() for column in columns.split(",") if column.strip())
    if compare_to:
        baseline_folder = folder if compare_folder is None else compare_folder
        try:
            result = compare_results(result_path(baseline_folder, compare_to),
                                     result_path(folder, csv_name), requested)
        except (ValueError, OSError) as exc:
            raise ToolError(str(exc)) from None
        result["baseline"]["folder"] = baseline_folder
        result["target"]["folder"] = folder
        return result
    if compare_folder is not None:
        raise ValueError("compare_folder requires compare_to")
    return summarize_results(result_path(folder, csv_name), requested)

@categorized_tool()
def pvsyst_read_rows(csv_name: str, columns: str = "E_Grid,PR",
                     offset: int = 0, limit: int = 100,
                     folder: str = "Results") -> dict:
    """Read a bounded page from Results or batch UserHourly CSVs."""
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError("Offset must be >= 0 and limit between 1 and 500")
    selected = [s.strip() for s in columns.split(",") if s.strip()]
    return read_result_page(result_path(folder, csv_name), selected, offset, limit)

@categorized_tool()
def pvsyst_read_batch_results(summary_name: str,
                              columns: str = "E_Grid,PR") -> dict:
    """Read SIM_* scenario values from a UserBatch/*Results[_<n>].CSV summary."""
    if not is_batch_summary_name(summary_name):
        raise ValueError("Expected a batch Results.CSV filename")
    target = workspace_path("UserBatch", summary_name, ".csv")
    requested = tuple(s.strip() for s in columns.split(",") if s.strip())
    if not requested:
        raise ValueError("Select at least one batch result column")
    return parse_batch_results(target, requested)
