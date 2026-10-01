# Third-party notices

## Blender Addon Updater

`updates/addon_updater.py` and `updates/addon_updater_ops.py` derive from
[CG Cookie Blender Addon Updater](https://github.com/CGCookie/blender-addon-updater).
Their GNU GPL version 2 or later license headers are retained. Package identity,
lifecycle integration and bounded transactional installation are adapted here.

## HD2PHY1 core

`physbone/core/format_v1.py` is migrated from the HD2 Independent Rig Runtime PhysBone producer
provided in the private research worktree. Its original MIT terms are retained verbatim in
`physbone/LICENSE`. The model and validation modules are non-transforming HD2PhysBoneTool
integration facades around that producer; this notice does not replace any other component's terms.

## Native motion preview

`bin/HD2PhysBonePreview.dll` compiles the HD2 Independent Rig Runtime's math,
Rig parser, HD2PHY1 parser and SpringBone solver with a standalone preview ABI.
It contains no ReShade or game code. The same MIT terms in `physbone/LICENSE`
cover these modules. `bin/HD2PhysBonePreview.build.json` records the binary and
source-input SHA-256 hashes for comparison with the companion game build.

## Preprocessed VRM physics conversion

The converter is an original adapter for the VRMC_springBone JSON representation
and the limit/extended-collider records found in the user's privately supplied
PhysBoneSupport reference project. That project was used to inspect input and
parameter semantics; its solver, avatar preprocessing scripts, models and assets
are not copied into or redistributed with this plugin. This is not an official
VRChat PhysBone implementation and does not claim motion equivalence.
