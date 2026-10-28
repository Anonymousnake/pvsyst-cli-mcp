# Project sources, cloning, and file transactions

Project and variant tools operate directly on `.PRJ` and `.VCx` text files in the workspace `Projects` directory. Every write checks current file SHA-256 hashes before touching disk and creates a rollback snapshot.

## Updating `.SIT` and `.MET` across a project (`pvsyst_update_project_sources`)

In PVsyst, the site (`.SIT`) and weather (`.MET`) filenames are stored in the `.PRJ` file **and** embedded inside each `.VCx` variant file. Changing only the `.PRJ` file leaves existing variants pointing to the old weather file.

- `pvsyst_update_project_sources` updates the `.SIT`/`.MET` references across the `.PRJ` and every `.VCx` variant in one transaction, while leaving historical weather-origin comment fields intact.
- Pass the complete `files` SHA-256 map returned by `pvsyst_inspect_project` as the precondition.
- Before writing the first file, the tool stages backups and re-verifies all member hashes and the directory listing; if an external editor modified a file or added a new `.VCx` sibling in the meantime, the operation aborts cleanly without overwriting external changes.
- To roll back, pass the returned backup manifest and current `files` map to `pvsyst_restore_project_sources`.

## Cloning variants within or across projects (`pvsyst_clone_variant`)

- Omitting `source_project` clones a variant into a new `.VCx` ID within the same project.
- Passing `source_project` copies a variant from `source_project` into a different existing project under a new `.VCx` ID, leaving both `.PRJ` files and the source variant untouched.
- You can optionally swap `.PAN`/`.OND`/`.BTR`/`.GEN` references during the clone. Note that embedded `.SIT`/`.MET` and 3D scene references inside the cloned `.VCx` are copied as-is from the source variant; call `pvsyst_update_project_sources` afterward if you want all variants in the destination project to share the same `.SIT`/`.MET`.

## Creating a project from raw text (`pvsyst_create_project`)

`pvsyst_create_project(project, project_content, variants)` writes a new `.PRJ` file and 1 to 127 `.VCx` files from complete UTF-8 strings:
- Variant IDs are normalized to uppercase (`VC0`, `VC1`, etc.), with a 32 MB limit per file and up to 128 total files (`1 PRJ + 127 VCs`).
- Before writing any file, the tool validates the `.PRJ` and `.VCx` block headers and subarray sections in memory, and checks that no existing `.PRJ`, orphan `.VCx`, or same-prefix sidecar files exist in `Projects`.
- Returns the initial `files` SHA-256 map for subsequent guarded edits or archiving.

## Display titles and UTF-8 BOM handling

- `pvsyst_update_project_parameters`: Edits the root `Comment` display title in `.PRJ` (see [project display title](project-title.md)).
- `pvsyst_update_variant_parameters(..., variant_name=..., add_utf8_bom=False)`: Edits the root `pvVCalcul` `Comment` display name in `.VCx`.

**Important UTF-8 BOM quirk in PVsyst 8.1.6**:
If a `.PRJ` or `.VCx` file does not start with a UTF-8 BOM (`EF BB BF`), PVsyst 8.1.6 GUI and CLI interpret the file using a legacy single-byte encoding (such as `cp1252`), which turns Chinese or other non-ASCII titles into mojibake in the UI and PDF reports.
- Consequently, setting a non-ASCII `project_name` or `variant_name` requires the file to already have a UTF-8 BOM, or you must explicitly pass `add_utf8_bom=true`.
- You can also pass the existing ASCII/non-ASCII title with `add_utf8_bom=true` to add a BOM to an unmarked file (`changed=true`, `bom_added=true` even though the text diff is empty).
- If the project is currently open in PVsyst GUI, save and close it in the GUI before running MCP file transactions, then call `pvsyst_inspect_project` to get a fresh `files` map.
