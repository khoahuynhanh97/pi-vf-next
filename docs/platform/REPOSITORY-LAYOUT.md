# Repository layout

Destination: D:/work/pi-vf-next. This is the proposed folder layout for source consolidation. The existing app trees determine the internal layout.

~~~text
pi-vf-next/
├── apps/
│   ├── pi/
│   │   ├── product_scout/       # Existing discovery, including Legacy Discovery and its guards
│   │   ├── media/               # Existing research, assets, and persistence
│   │   ├── tools/               # Existing file intake, Spaiauto, and Exchange publishing
│   │   ├── scripts/
│   │   ├── tests/
│   │   ├── pyproject.toml
│   │   └── uv.lock
│   └── vf/
│       ├── src/video_factory/   # Existing CLI, Studio, workflow, creative, QC, and render
│       ├── config/              # Tracked runtime profiles and safe examples
│       ├── scripts/
│       ├── tests/
│       ├── pyproject.toml
│       └── uv.lock
├── automation/                  # Phase 02+: unified PI → VF orchestration
│   ├── pipelines/               # Integration using the existing PI/VF handoff
│   ├── scheduler/               # Phase 03: daily trigger and runtime bindings
│   ├── execution/               # Only cross-app execution logic proven to be missing
│   └── agents/                  # Phase 04: only agent logic proven to be missing
├── shared/                      # Add when both apps have a concrete shared caller
├── infrastructure/              # Add when shared deployment/configuration needs it
├── docs/
│   ├── platform/                # Architecture, layout, roadmap, and handoff prompt
│   └── adr/                     # Architecture decisions
├── AGENTS.md
└── README.md
~~~

## What to create in Phase 01

Create apps/pi and apps/vf. Preserve the internal packages, entrypoints, dependency declarations, migrations, tests and static assets required by each app. The tree above shows the main paths. The move inventory must also include required assets, docs, configuration files at an app root, and local compatibility packages where call sites prove they are needed. Keep the app-specific lockfiles until a shared dependency environment is proven compatible.

The current root src/pi_vf, tests, pyproject.toml, uv.lock and Run.ps1 belong to the earlier pilot. Keep them identifiable during the transfer. Reassess them after the unified pipeline proves which orchestration logic is useful. Never mix pilot implementations into the original apps merely to match the tree.

## Where the second tree fits

The discovery, crawling, validation, storyboard, generation and workflow names are logical responsibilities. Their existing implementations stay in the app that owns them. Legacy Discovery stays in apps/pi/product_scout. VF worker and job code stay in apps/vf/src/video_factory. Add root-level orchestration only when integrating those existing entrypoints.

Use apps/ as the canonical physical layout. A second modules/ or workers/ tree would imply moving or duplicating existing behavior. Split automation into its proposed subfolders when the code earns the split. Phase 01 can pass with only apps/ and docs/ added to the current repo layout.

## Runtime files

Use D:/work/pi-vf-next/runtime as the proposed default after runtime cutover. Keep it outside the app source trees and Git tracking. The folder layout reuses the original PI importer, Exchange and VF workspace conventions.

~~~text
runtime/
├── pi/
│   └── runs/<intake-run>/
│       ├── resources.json
│       └── .../<import>/
│           ├── raw/download.zip
│           ├── raw/capture.json
│           ├── raw/page.png
│           └── media/candidates/
├── exchange/inbox/<date>/run_<batch>/
│   ├── manifest.json
│   ├── products/<key>/images/
│   └── READY
└── vf/workspace/
    ├── projects/<project_id>/
    │   ├── creative/
    │   ├── storyboard/
    │   └── renders/v<version>/
    │       ├── scenes/<scene_id>/attempt-<NNN>/clip.mp4
    │       └── composite/
    └── exports/export_<project_id>_<version>/
        ├── final.mp4
        ├── caption.txt
        └── metadata.json
~~~

The PI runner supplies the intake run directory explicitly. Its existing importer controls the nested import paths. PI owns raw ZIP/capture and candidate images. After review, PI publishes eligible product metadata and selected images to the shared inbox using the existing Exchange publisher. The publisher copies selected assets, verifies their hashes and writes READY last. VF reads the manifest and its relative image paths, checks admission, then uses its workspace for project artifacts. VF consumes the published package rather than walking PI's raw import directories.

Set HERMES_EXCHANGE_INBOX to runtime/exchange/inbox for VF and pass the same directory to the PI publisher. Set HERMES_VIDEO_FACTORY_WORKSPACE to runtime/vf/workspace. Preserve the original DB bindings during the source move. Rebind DB paths and browser profile explicitly at runtime cutover, independently of the two old checkouts. The location to retrieve a completed video is vf/workspace/exports, with one versioned package per export.

Observed reference runtime: D:/work/pi-vf-runtime-20261102 uses inbox and workspace at its root. Its historical Nhà Mứt output is workspace/exports/export_proj-pv-daily-534f6e82cab9e9c0_1/final.mp4. The paths in the proposed tree describe a future configuration of the consolidated original apps. The earlier pi-vf-next pilot has a separate data/products output convention.
