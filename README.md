# godot-cli-builder

A single-file Python CLI that automates the full **build → export → test** pipeline for
**Godot 4.x C#/.NET** projects, with zero extra Python dependencies.

Designed to run comfortably inside Docker containers (including Apple Silicon / Arm64 Linux),
making it a practical sandbox for AI-assisted development.

```
python3 scripts/cli_builder.py prepare-env --godot-version 4.6.2
source env.sh
python3 scripts/cli_builder.py -p linux_arm64 --project test-01 --run
```

---

## Motivation

Godot's headless CLI mode is uniquely well-suited as a **C#/.NET sandbox**:

- Native Arm64 Linux support → runs inside Docker on Apple Silicon without Rosetta
- MIT licence → no per-seat or per-machine restrictions in CI / agentic loops
- `--headless --import / --build-solutions / --export-*` pipeline is fully scriptable
- Lightweight: the whole toolchain fits in a container image with no GUI dependencies

The tool grew out of a project at [fondi Inc.](https://www.fondi.fun/top/) where Godot is used as a
**complement to an existing Unity project** rather than a replacement — providing a
runtime-agnostic sandbox to build and test pure-C# business logic at speed, with no
Unity licence required per sandboxed instance.

Presented at [Godot Meetup Tokyo Vol.7](https://godot-jp.connpass.com/event/386275/) (2026-04-16)**.

---

## Features

| Feature | Description |
|---|---|
| `prepare-env` | One-shot installer: [mise](https://mise.jdx.dev) → dotnet 9 → Godot binary → export templates |
| Incremental build cache | Skips import / build when nothing changed (stamp + mtime scan) |
| Multi-platform export | macOS (universal), Linux x86\_64, Linux arm64, Android APK |
| Android auto-bootstrap | Downloads cmdline-tools, installs SDK packages, generates debug keystore, patches editor settings |
| Package cache | `--package-cache-dir` caches Godot zip, templates tpz, mise downloads, NuGet packages |
| `--run` | Runs the exported binary headlessly; streams stdout; stops on `##GODOT_TEST_EOM##` marker |
| In-scene NUnit tests | Auto-starts a TCP echo server on port 8028 for networking fixtures; parses XML report |
| `--run-tests` | Runs `tests/*.csproj` via `dotnet test` and writes a NUnit XML report |
| `--run-pure-csharp-tests` | Fast path: `dotnet test` only — no Godot binary or export needed |

---

## Requirements

| Tool | Version | Notes |
|---|---|---|
| Python | 3.8+ | Standard library only |
| Godot | 4.x (mono/.NET) | Auto-downloaded by `prepare-env` |
| .NET SDK | 9 | Auto-installed via mise by `prepare-env` |
| Java | 17 | Required only for Android export (auto-installed with `--android`) |

---

## Quick Start

### 1. First-time setup

```bash
# Download Godot binary, export templates, and dotnet 9 via mise
python3 scripts/cli_builder.py prepare-env --godot-version 4.6.2

# Activate the environment (sets PATH, GODOT_BINARY, NUGET_PACKAGES, …)
source env.sh
```

Use `--package-cache-dir` to share downloads across containers / CI runs:

```bash
python3 scripts/cli_builder.py prepare-env \
    --godot-version 4.6.2 \
    --package-cache-dir /mnt/cache/godot
```

Add `--android` to also install the Android SDK and generate a debug keystore.

### 2. Build and export

```bash
# Export for Linux arm64
python3 scripts/cli_builder.py -p linux_arm64 --project test-01

# Export for macOS (universal .app)
python3 scripts/cli_builder.py -p macos --project test-01

# Export for all platforms
python3 scripts/cli_builder.py -p all --project test-01
```

### 3. Export and run

```bash
# Build, export, and run headlessly (Linux host, matching arch)
python3 scripts/cli_builder.py -p linux_arm64 --project test-01 --run

# macOS host
python3 scripts/cli_builder.py -p macos --project test-01 --run
```

### 4. NUnit tests (export → run in-scene)

For projects that contain an `InSceneTests/` directory, `--run` starts a TCP echo server
on port 8028, launches the binary headlessly, and stops as soon as the
`##GODOT_TEST_EOM##` marker appears in stdout. The XML report is read from
`TestResults/InSceneTestResults.xml`.

```bash
python3 scripts/cli_builder.py -p linux_arm64 --project <project> --run
```

### 5. Pure C# tests (no Godot, no export)

The fastest feedback loop: just `dotnet test` on `tests/*.csproj`.

```bash
python3 scripts/cli_builder.py --project test-02 --run-pure-csharp-tests
```

### 6. Android APK

```bash
python3 scripts/cli_builder.py -p android --project test-01
```

The first run auto-installs the Android SDK under `<workspace>/.android/`.
On subsequent runs the SDK is reused.

---

## Example Projects

### `test-01/` — Hello World

Minimal Godot 4.x C# project. Demonstrates a basic export preset configuration
for macOS, Linux x86\_64, Linux arm64, and Android.

Also includes `Dockerfile.linux_arm64` showing how to run the exported binary
inside a Debian Slim container.

### `test-02/` — Pure C# NUnit tests

Shows the **pure-C# fast path**: a `Calculator` class with no Godot dependencies
is tested directly with `dotnet test`, producing a NUnit XML report.

```
test-02/
├── Calculator.cs          # pure-C# domain logic
├── HelloWorld.cs          # Godot scene script (uses Calculator)
├── tests/
│   ├── tests.csproj       # Microsoft.NET.Sdk — no Godot SDK
│   └── CalculatorTests.cs # NUnit tests (one pass, one intentional fail)
└── export_presets.cfg
```

Run:

```bash
python3 scripts/cli_builder.py --project test-02 --run-pure-csharp-tests
```

---

## CLI Reference

### `prepare-env`

```
python3 scripts/cli_builder.py prepare-env [options]

Options:
  --godot-version VERSION   Godot version to download (e.g. 4.6, 4.6.2)
  --package-cache-dir DIR   Cache downloaded files for reuse
  --android                 Also install Android SDK + debug keystore
  --android-sdk-dir DIR     Custom Android SDK directory
  --skip-mise               Skip mise / dotnet provisioning
  --skip-godot              Skip Godot binary download
  --skip-templates          Skip export template extraction
```

### Export / Test

```
python3 scripts/cli_builder.py [options]

Platform:
  -p, --platform PLATFORM   macos | linux_x86_64 | linux_arm64 | android | all
                            (not required with --run-pure-csharp-tests)

Project:
  --project DIR             Path to Godot project directory (default: cwd)
  --godot PATH              Path to Godot binary (default: auto-detect)

Export:
  --export-type TYPE        release (default) | debug
  --skip-import             Skip resource import step
  --force-import            Force resource import
  --skip-build              Skip .NET build step
  --force-build             Force .NET build
  --clean                   Remove export/ before building

Run:
  --run                     Run exported binary headlessly after export
  --run-timeout SECS        Timeout for --run (default: 60)

Test:
  --run-tests               Run tests/ via dotnet test after export
  --run-pure-csharp-tests   Run tests/ via dotnet test only (no Godot)
```

---

## Incremental Build Cache

`cli_builder.py` maintains a `.godot_build_cache/` directory inside the project:

| Step | Skipped when… |
|---|---|
| Import | `.godot/imported/` exists and no non-C# asset is newer than the import stamp |
| Build | No `.cs` / `.csproj` / `.sln` file is newer than the build stamp |

Use `--force-import` / `--force-build` to bypass, or `--skip-import` / `--skip-build`
to unconditionally skip.

Timing for each step is saved to `.godot_build_cache/metrics.json` and shown in the
summary so you can see how much time was saved on incremental runs.

---

## Directory Layout (this repo)

```
godot-cli-builder/
├── scripts/
│   └── cli_builder.py     # the tool
├── test-01/               # Hello World example
│   ├── Dockerfile.linux_arm64
│   └── …
├── test-02/               # Pure-C# NUnit example
│   ├── Calculator.cs
│   ├── tests/
│   └── …
└── README.md
```

---

## Licence

MIT
