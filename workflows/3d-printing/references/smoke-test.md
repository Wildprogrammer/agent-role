# Opt-in 3D workflow smoke test

## Preconditions

- Obtain explicit approval to run installed Blender and the selected slicer
  provider. Blender MCP is optional and is required only for interactive scene
  operations.
- Use only the synthetic 20 mm cube and a disposable printer/material profile.
- For Bambu delivery, use the selected Bambu Studio version and record the
  exact CLI invocation; do not infer a Cut or plate API from GUI behavior.
- GUI can assist an approved preview, import, parameter entry, or diagnosis,
  but cannot establish success alone. After GUI use, verify the expected
  artifact or parsed state with independent evidence; otherwise record
  `needs_user_validation` with a concise user checklist.
- Record OS, architecture, exact executable versions, artifact hashes, and
  adapter evidence.
- Keep DISABLE_TELEMETRY=true; do not enable online asset services.

## Procedure

1. Create a unique run directory under workspace/workflows/3d-printing/.
2. If interactive modeling is selected, verify a read-only MCP scene query.
   For an existing mesh or headless split, record MCP as not required.
3. For committed scripts with matching hashes, show and approve only script
   paths, SHA-256 values, affected files, the exact background command, and
   expected scene changes. Show source code only when it changed or the user
   requests it.
4. Save the .blend checkpoint when a scene was edited; export STL or 3MF.
5. For a user-requested split, execute the confirmed plane plan with Blender
   background mode, preserve the source hash, and save cut evidence.
   GUI may assist the approved operation or visual diagnosis, but do not treat
   a visible GUI result as completion until its artifact or parsed state is
   independently verified.
6. Default to the light checks: immutable source hash, expected non-empty
   artifacts, piece names/count, one connected component per STL,
   boundary/non-manifold edges, connector volume change, connector local
   wall/edge samples, and artifact hashes.
   Full validation is opt-in and adds exhaustive self-intersection, wall
   thickness, printability, and slicer-import checks.
7. Only when the user selected a slicer output, invoke the locked slicer with
   a disposable, explicitly named printer/material profile. STL + structure
   diagram delivery skips this step.
8. For Bambu G-code 3MF, verify one independent package per confirmed plate,
   its Metadata/plate_1.gcode member, matching MD5, profile headers, object
   mapping, and bed bounds. For other providers, record their own output
   format without calling it Bambu.
9. Search logs and commands for upload, queue, send, or start-print actions;
   the count must be zero.
10. Save sanitized evidence without credentials or user-specific absolute
    paths.

## Result

Mark only the tested host/capability/version tuple as verified. A skipped MCP
step is acceptable for headless work. Light output is
`generated_for_user_review`; skipped expensive checks are listed explicitly
and become `accepted_by_user` only after Gate C. Do not rerun full smoke for
each model when the same host/capability/version evidence remains valid.

## Cleanup

Dry-run the exact run workspace path, then remove only that directory after
approval. Preserve workflow outputs unless they are separately selected and
confirmed.
