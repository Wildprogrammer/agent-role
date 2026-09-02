# Mesh validation evidence

Run checks against the exported mesh, not only the editable Blender scene. Save
machine-readable results in `validation.json`; add screenshots only when they
help a human review.

| Check | Required evidence | Pass | Fail |
| --- | --- | --- | --- |
| Dimensions | X/Y/Z in millimeters and source units | Matches confirmed dimensions within tolerance | Missing units or any dimension outside tolerance |
| Manifold | Boundary and non-manifold edge counts | Both counts are zero unless an explicitly approved open surface is intended | Unexpected boundary or non-manifold edge |
| Normals | Inward/inconsistent face count | Zero | Any unresolved inward or inconsistent face |
| Wall thickness | Minimum measured wall and configured threshold | Minimum is at or above the confirmed material/process threshold | Below threshold or not measured |
| Self-intersection | Intersecting face-pair count | Zero | Any unresolved intersection |
| Overhang | Threshold angle and unsupported area | Within the confirmed support strategy | Unsupported area conflicts with the strategy |
| Orientation | Bed-contact face and bounding box | Stable contact and fits confirmed build volume | Unstable orientation or exceeds build volume |
| Clearance/tolerance | Measured mating clearances | Each matches the confirmed tolerance | Any clearance is missing or out of range |

Record `model_sha256`, Blender version, checker name/version, profile inputs,
result, warnings, and timestamp. A warning never silently becomes a pass.

Official references:

- Blender 3D Print Toolbox: https://docs.blender.org/manual/en/latest/addons/mesh/3d_print_toolbox.html
- Blender mesh analysis: https://docs.blender.org/manual/en/latest/modeling/meshes/mesh_analysis.html
