# Project sources and file transactions

Project and variant tools edit or copy actual PVsyst files in the workspace. They do not create physical defaults, import referenced dependencies automatically, or interpret binary MET contents. Inspect references and validate the resulting structure before native execution.

## Source update and recovery

`pvsyst_update_project_sources` changes a PRJ and all its VC members to one workspace SIT/MET pair while preserving historical weather-origin fields. Use the complete inspected file map as the precondition. `pvsyst_restore_project_sources` restores the entire saved set using its manifest, backup identities and current file map.

All members and candidate envelopes are checked before writing. After backup/staging, the operation rechecks both content and membership immediately before the first replacement. External changes or a new sibling VC stop the operation, preserve the external data and retain backups; temporary candidates are removed. Changed-member replacement errors attempt rollback of already-written files.

This is optimistic checking, not cross-process exclusion or crash-atomic multi-file replacement. Coordinate external editors; process/machine failure and edits after the final check remain separate risks. Restore validates complete unmixed backup sets and their manifest, not arbitrary recorded paths.

## Cross-project variant copy

`pvsyst_clone_variant(..., source_project=None)` defaults to same-project copying. An explicit source_project imports one existing source VC into another existing project prefix/new ID. It preserves both PRJs and the source VC; optional scoped component replacement follows its own rules.

Embedded SIT/MET, components and external scene references remain inherited, not synchronized with the destination PRJ. Dependencies are not copied or rebuilt. Inspect source differences and availability; use the full-project source transaction only when deliberately changing every member. This is a copy, not a move or blank design generator.

## Content-based project creation

`pvsyst_create_project(project, project_content, variants)` accepts complete UTF-8 PRJ text and a map of 1..127 VC IDs to complete contents. IDs normalize to uppercase; normalized duplicates stop. BOM/newlines and references are preserved.

Envelopes and subarray sections are checked in memory before any write. Each member is limited to 32 MB and the complete set to 128 PRJ/VC files. Each output is exclusively created; handled failures remove only the new files created by this operation. Existing projects, orphan VCs or unknown same-prefix sidecars conflict. The returned files map supports subsequent guarded archive/recovery.

Creation is not complete physical/schema validation, dependency import, cross-process locking or crash-atomic creation. SIT/MET, components and scenes must be available separately. No existing source project is needed and no hidden physical design is generated.

## Display names and UTF-8

`pvsyst_update_project_parameters(project, expected_files, project_name, dry_run=False, add_utf8_bom=False)` changes the owning PRJ header Comment. `pvsyst_update_variant_parameters(..., variant_name=..., add_utf8_bom=False)` changes only the owning pvVCalcul header Comment. Neither renames files/IDs, changes association, reorders variants or rewrites nested names.

Names are strict printable single-line strings, 1..80 UTF-8 bytes, without surrounding whitespace. The limit is an adapter contract, not a measured universal GUI limit. The writer requires one existing owning header name and a complete recognized tree; missing, duplicate, nested or late root names stop. PrintSettings is not a substitute owner. Raw inspection reports header names and utf8_bom, not the effective native decoding of malformed or unmarked files.

Non-ASCII names require an existing UTF-8 BOM or explicit add_utf8_bom=true. Marker insertion changes whole-file decoding, not only the name; bom_added exposes this change. Variant marker insertion requires a variant_name request. Ordinary non-name edits retain their marker state. No implicit ANSI conversion or BOM insertion occurs.

A same-name marker-only update can have equal before/after names and empty text diff while changed=true. Repetition on the marked file is a no-op without backup. Formal recovery restores the original marker and newline state. CLI 8.1.6 may display BOM-less Chinese UTF-8 through a cp1252-like interpretation even when raw inspection returns the intended text; a marked file is required for the supported native name path, not inferred from generic UTF-8 format documentation.

Project title edits protect and snapshot the complete PRJ/VC set but write only changed bytes, preserving unchanged VC data, timestamps and file identities. Recovery reuses restore_project_sources. Variant names use normal variant snapshots; supported ordinary scalar updates may accompany a name, while mode/sharing transactions keep their own exclusivity.

For a project open in GUI, save and close it before file edits, then obtain a fresh complete files map. Native GUI saves may change PRJ format/version fields; stale preconditions correctly stop further edits. Window captions identify the file, not necessarily its project title. See [project title usage](project-title.md) for GUI and recovery details.
