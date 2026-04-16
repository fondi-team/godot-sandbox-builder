#!/usr/bin/env python3
"""
CLI builder for Godot 4.x C#/.NET projects.

Automates the build and export pipeline for Godot .NET projects,
supporting macOS, Linux x86_64, and Linux arm64 targets.

Subcommands
-----------
  prepare-env   Install all prerequisites (mise, dotnet, Godot binary,
                export templates) and write env.sh for shell activation.
  (default)     Export one or more platforms (backward-compatible flags).

Usage examples
--------------
  # First-time setup (fresh container):
  python3 cli_builder.py prepare-env
  source env.sh

  # Build & export:
  python3 cli_builder.py -p linux_arm64 --project test-01 --run

  # Build, export, and run NUnit tests:
  python3 cli_builder.py -p linux_arm64 --project test-02 --run-tests

  # Build, export, and run pure C# tests (platform independent):
  python3 scripts/cli_builder.py --project test-fondi --run-pure-csharp-tests

  # Build, export, and run in-scene tests (test-fondi):
  python3 cli_builder.py -p linux_arm64 --project test-fondi --run

  # Build and export Android apk (test-fondi):
  python3 cli_builder.py -p android --project test-fondi 

  # Build and export macOS ad-hoc binary (test-fondi):
  python3 cli_builder.py -p macos --project test-fondi 
"""

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Optional


# Platform preset definitions
# Maps CLI platform names to export_presets.cfg preset names and output paths.
PLATFORMS = {
    "macos": {
        "preset": "macOS",
        "output_suffix": ".app",
        "output_dir": "export/macos",
    },
    "linux_x86_64": {
        "preset": "Linux x86_64",
        "output_suffix": ".x86_64",
        "output_dir": "export/linux_x86_64",
    },
    "linux_arm64": {
        "preset": "Linux arm64",
        "output_suffix": ".arm64",
        "output_dir": "export/linux_arm64",
    },
    "android": {
        "preset": "Android",
        "output_suffix": ".apk",
        "output_dir": "export/android",
    },
}

# ─── Android SDK constants ────────────────────────────────────────────────

_ANDROID_CMDTOOLS_BUILD = "11076708"
_ANDROID_CMDTOOLS_FILENAME = f"commandlinetools-linux-{_ANDROID_CMDTOOLS_BUILD}_latest.zip"
_ANDROID_CMDTOOLS_URL = (
    f"https://dl.google.com/android/repository/{_ANDROID_CMDTOOLS_FILENAME}"
)
_ANDROID_BUILD_TOOLS_VER = "34.0.0"
_ANDROID_PLATFORM_VER = "android-34"          # directory name under sdk/platforms/
_ANDROID_PLATFORM_PKG = "platforms;android-34"  # sdkmanager package name


# ─── incremental build cache ──────────────────────────────────────────────


_CACHE_DIR_NAME = ".godot_build_cache"

# Directories and extensions to skip when scanning for resource changes.
_IMPORT_SKIP_DIRS = frozenset({
    ".godot", "export", "TestResults", _CACHE_DIR_NAME,
    ".git", ".godot_config", "bin", "obj",
})
_IMPORT_SKIP_EXTS = frozenset({".cs", ".sln", ".stamp", ".json", ".md"})

_BUILD_SKIP_DIRS = frozenset({
    "export", "TestResults", _CACHE_DIR_NAME,
    ".git", ".godot_config", "bin", "obj",
})


def _cache_dir(project_dir: Path) -> Path:
    d = project_dir / _CACHE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stamp_path(project_dir: Path, step: str) -> Path:
    return _cache_dir(project_dir) / f"{step}.stamp"


def _read_stamp_mtime(project_dir: Path, step: str) -> Optional[float]:
    p = _stamp_path(project_dir, step)
    return p.stat().st_mtime if p.exists() else None


def _write_stamp(project_dir: Path, step: str) -> None:
    _stamp_path(project_dir, step).touch()


def _load_metrics(project_dir: Path) -> dict:
    p = _cache_dir(project_dir) / "metrics.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_metrics(project_dir: Path, metrics: dict) -> None:
    p = _cache_dir(project_dir) / "metrics.json"
    p.write_text(json.dumps(metrics, indent=2))


def _needs_import(project_dir: Path) -> tuple[bool, str]:
    """Return (True, reason) if a resource re-import is required."""
    imported_dir = project_dir / ".godot" / "imported"
    if not imported_dir.exists() or not any(imported_dir.iterdir()):
        return True, "no import cache (.godot/imported/ missing or empty)"

    stamp_mtime = _read_stamp_mtime(project_dir, "import")
    if stamp_mtime is None:
        return True, "first run (no import stamp)"

    for item in project_dir.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(project_dir)
        if any(part in _IMPORT_SKIP_DIRS for part in rel.parts):
            continue
        if item.suffix.lower() in _IMPORT_SKIP_EXTS:
            continue
        if item.stat().st_mtime > stamp_mtime:
            return True, str(rel)

    return False, "assets unchanged"


def _needs_build(project_dir: Path) -> tuple[bool, str]:
    """Return (True, reason) if a dotnet build is required."""
    stamp_mtime = _read_stamp_mtime(project_dir, "build")
    if stamp_mtime is None:
        return True, "first run (no build stamp)"

    for item in project_dir.rglob("*"):
        if not item.is_file():
            continue
        if item.suffix not in (".cs", ".csproj", ".sln"):
            continue
        rel = item.relative_to(project_dir)
        if any(part in _BUILD_SKIP_DIRS for part in rel.parts):
            continue
        if item.stat().st_mtime > stamp_mtime:
            return True, str(rel)

    return False, "C# sources unchanged"


def find_godot_binary() -> Optional[Path]:
    """Locate the Godot editor binary.

    Search order:
      1. GODOT_BINARY environment variable
      2. On Linux: Godot_*_mono_linux_{arch}/ directory in the repo root (native arch)
      3. On macOS: Godot_mono.app in the repo root, then /Applications, ~/Applications,
         and versioned .app bundles matching Godot*mono*.app
      4. On Linux: any Godot_*_mono_linux_*/ directory in the repo root
      5. 'godot' on PATH
    """
    env = os.environ.get("GODOT_BINARY")
    if env:
        p = Path(env)
        if p.is_file():
            return p

    repo_root = Path(__file__).resolve().parent.parent
    host = platform.system()

    if host == "Linux":
        # Prefer native-arch Linux binary
        arch = platform.machine()
        arch_tag = "arm64" if arch in ("aarch64", "arm64") else "x86_64"
        for candidate_dir in sorted(repo_root.glob(f"Godot_*mono_linux*{arch_tag}*"), reverse=True):
            for binary in sorted(candidate_dir.glob("Godot_*linux*"), reverse=True):
                if binary.is_file() and binary.suffix not in (".zip", ".tar", ".gz", ".exe"):
                    binary.chmod(binary.stat().st_mode | 0o111)
                    return binary

    if host == "Darwin":
        # Search candidates in priority order
        app_search_dirs = [
            repo_root,
            Path.home() / "Applications",
            Path("/Applications"),
        ]
        # First try exact Godot_mono.app
        for search_dir in app_search_dirs:
            app_binary = search_dir / "Godot_mono.app" / "Contents" / "MacOS" / "Godot"
            if app_binary.is_file():
                return app_binary
        # Then try versioned and non-mono names (e.g. Godot_v4.6.2-stable_mono.app)
        for search_dir in app_search_dirs:
            for app_bundle in sorted(search_dir.glob("Godot*mono*.app"), reverse=True):
                binary = app_bundle / "Contents" / "MacOS" / "Godot"
                if binary.is_file():
                    return binary
            # Also accept non-mono Godot.app as last resort in each dir
            for app_bundle in sorted(search_dir.glob("Godot*.app"), reverse=True):
                binary = app_bundle / "Contents" / "MacOS" / "Godot"
                if binary.is_file():
                    return binary

    if host == "Linux":
        # Any Linux binary as last resort
        for candidate_dir in sorted(repo_root.glob("Godot_*mono_linux*"), reverse=True):
            for binary in sorted(candidate_dir.glob("Godot_*linux*"), reverse=True):
                if binary.is_file() and binary.suffix not in (".zip", ".tar", ".gz", ".exe"):
                    binary.chmod(binary.stat().st_mode | 0o111)
                    return binary

    # Fallback: check PATH
    which = shutil.which("godot")
    if which:
        return Path(which)

    return None


def ensure_dotnet_env() -> None:
    """Ensure DOTNET_ROOT is set and dotnet is on PATH.

    Searches common install locations used by the dotnet-install script
    and the repo-sibling layout used in this project.
    """
    if shutil.which("dotnet"):
        return

    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        repo_root.parent / "dotnet8",       # <workspace>/fondi-app/dotnet8
        repo_root.parent.parent / "dotnet8", # one level higher
        Path.home() / "dotnet",
        Path("/usr/local/dotnet"),
        Path("/usr/share/dotnet"),
    ]
    for candidate in candidates:
        dotnet = candidate / "dotnet"
        if dotnet.is_file():
            os.environ["DOTNET_ROOT"] = str(candidate)
            os.environ["PATH"] = f"{candidate}{os.pathsep}{os.environ.get('PATH', '')}"
            print(f"  dotnet found at {candidate}")
            return

    print(
        "Warning: dotnet not found on PATH or common locations. "
        "Set DOTNET_ROOT or install .NET SDK.",
        file=sys.stderr,
    )


# ─── prepare-env helpers ──────────────────────────────────────────────────


def _find_workspace(repo_root: Path) -> Path:
    """Return a writable directory with ≥10 GB free for storing large binaries.

    Walks up from repo_root; falls back to repo_root if nothing better found.
    """
    for candidate in [
        repo_root,
        repo_root.parent,
        repo_root.parent.parent,
        repo_root.parent.parent.parent,
    ]:
        try:
            if shutil.disk_usage(candidate).free < 10 * 1024 ** 3:
                continue
            probe = candidate / ".cli_builder_write_test"
            probe.touch()
            probe.unlink()
            return candidate
        except OSError:
            continue
    return repo_root


def _ensure_mise(workspace: Path) -> Path:
    """Install mise into workspace/.mise/bin if not already present."""
    mise_bin = workspace / ".mise" / "bin" / "mise"
    if mise_bin.is_file():
        print(f"  mise already installed: {mise_bin}")
        return mise_bin
    mise_bin.parent.mkdir(parents=True, exist_ok=True)
    print("  Downloading and installing mise …")
    env = os.environ.copy()
    env["MISE_INSTALL_PATH"] = str(mise_bin)
    subprocess.run(
        ["sh", "-c", "curl -fsSL https://mise.run | sh"],
        env=env, check=True,
    )
    if not mise_bin.is_file():
        raise RuntimeError(f"mise installation failed: {mise_bin} not found after install")
    print(f"  mise installed: {mise_bin}")
    return mise_bin


def _write_mise_toml(repo_root: Path, include_java: bool = False) -> None:
    """Write .mise.toml at repo root declaring required tools."""
    toml_path = repo_root / ".mise.toml"
    lines = ['dotnet = "9"']
    if include_java:
        lines.append('java = "17"')
    content = "[tools]\n" + "\n".join(lines) + "\n"
    if toml_path.exists() and toml_path.read_text() == content:
        print(f"  .mise.toml already up to date")
        return
    toml_path.write_text(content)
    print(f"  Wrote {toml_path}")


def _mise_install(mise_bin: Path, repo_root: Path, workspace: Path,
                  cache_dir: Optional[Path] = None) -> None:
    """Run 'mise install' to provision dotnet, storing data under workspace."""
    mise_data = workspace / ".mise"
    nuget_pkg = workspace / ".nuget" / "packages"
    nuget_pkg.mkdir(parents=True, exist_ok=True)

    # Keep all temp/cache I/O on workspace to avoid filling the root filesystem.
    tmp_dir = workspace / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # If a shared cache dir is provided, redirect mise's download cache and
    # NuGet package store there so they survive across fresh workspaces.
    if cache_dir is not None:
        mise_cache = cache_dir / "mise"
        nuget_pkg = cache_dir / "nuget" / "packages"
        mise_cache.mkdir(parents=True, exist_ok=True)
        nuget_pkg.mkdir(parents=True, exist_ok=True)
    else:
        mise_cache = mise_data / "cache"

    env = os.environ.copy()
    env["MISE_DATA_DIR"] = str(mise_data)
    env["MISE_CACHE_DIR"] = str(mise_cache)
    env["NUGET_PACKAGES"] = str(nuget_pkg)
    env["TMPDIR"] = str(tmp_dir)   # dotnet-install.sh respects TMPDIR
    print(f"  MISE_DATA_DIR  = {mise_data}")
    print(f"  MISE_CACHE_DIR = {mise_cache}")
    print(f"  NUGET_PACKAGES = {nuget_pkg}")
    print(f"  TMPDIR         = {tmp_dir}")

    subprocess.run(
        [str(mise_bin), "trust", "--yes", str(repo_root / ".mise.toml")],
        cwd=repo_root, env=env, check=True,
    )
    subprocess.run(
        [str(mise_bin), "install", "--yes"],
        cwd=repo_root, env=env, check=True,
    )

    # Activate dotnet in the current process
    shims = mise_data / "shims"
    os.environ["MISE_DATA_DIR"] = str(mise_data)
    os.environ["NUGET_PACKAGES"] = str(nuget_pkg)
    if (shims / "dotnet").is_file():
        os.environ["PATH"] = f"{shims}{os.pathsep}{os.environ.get('PATH', '')}"
        print(f"  dotnet available via mise shims")
    else:
        installs = mise_data / "installs" / "dotnet"
        for dotnet in sorted(installs.rglob("dotnet"), reverse=True):
            if dotnet.is_file():
                os.environ["DOTNET_ROOT"] = str(dotnet.parent)
                os.environ["PATH"] = f"{dotnet.parent}{os.pathsep}{os.environ.get('PATH', '')}"
                print(f"  DOTNET_ROOT = {dotnet.parent}")
                break


def _version_str_to_tag(version_str: str) -> str:
    """Convert a Godot version string to a GitHub release tag.

    Handles short forms ("4.6", "4.6.2") and full version.txt forms
    ("4.6.stable", "4.6.stable.mono", "4.6.2.stable.mono").
    """
    parts = version_str.split(".")
    if len(parts) == 2:
        # "4.6" → "4.6-stable"
        return f"{version_str}-stable"
    if len(parts) == 3 and parts[2][0].isdigit():
        # "4.6.2" → "4.6.2-stable"
        return f"{version_str}-stable"
    if len(parts) == 3:
        # "4.6.stable" → "4.6-stable"
        return f"{parts[0]}.{parts[1]}-{parts[2]}"
    if len(parts) == 4:
        # "4.6.stable.mono" → "4.6-stable"
        return f"{parts[0]}.{parts[1]}-{parts[2]}"
    if len(parts) >= 5:
        # "4.6.2.stable.mono" → "4.6.2-stable"
        return f"{parts[0]}.{parts[1]}.{parts[2]}-{parts[3]}"
    raise ValueError(f"Cannot parse Godot version string: {version_str!r}")


def _read_godot_template_version(repo_root: Path) -> Optional[str]:
    """Read Godot version string (e.g. '4.6.2.stable.mono') from the tpz archive."""
    tpz = repo_root / "mono_export_templates.tpz"
    if not tpz.exists():
        return None
    try:
        with zipfile.ZipFile(tpz) as z:
            return z.read("templates/version.txt").decode().strip()
    except Exception:
        return None


def _download_with_cache(url: str, filename: str, dest: Path,
                         cache_dir: Optional[Path]) -> None:
    """Download *url* to *dest*, using *cache_dir* as a local file cache.

    If *cache_dir* is given and already contains *filename*, the cached copy is
    used instead of fetching from the network.  On a fresh download the file is
    also saved into *cache_dir* so future runs can skip the download.
    """
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / filename
        if cached.is_file():
            print(f"  Using cached file: {cached}")
            shutil.copy2(cached, dest)
            return

    print(f"  Downloading {url} …")
    urllib.request.urlretrieve(url, dest)

    if cache_dir is not None:
        cached = cache_dir / filename
        if not cached.is_file():
            shutil.copy2(dest, cached)
            print(f"  Cached to: {cached}")


def _ensure_godot_binary(repo_root: Path, version_str: str,
                         cache_dir: Optional[Path] = None) -> None:
    """Download and extract the Godot mono editor binary for the current platform."""
    host = platform.system()
    arch = platform.machine()

    if host == "Linux":
        arch_tag = "arm64" if arch in ("aarch64", "arm64") else "x86_64"
        existing = [
            p for p in repo_root.glob(f"Godot_*mono_linux*{arch_tag}*/Godot_*linux*")
            if p.is_file() and p.suffix not in (".zip",)
        ]
        if existing:
            print(f"  Godot binary already present: {existing[0]}")
            return
        tag = _version_str_to_tag(version_str)
        filename = f"Godot_v{tag}_mono_linux_{arch_tag}.zip"
        url = f"https://github.com/godotengine/godot/releases/download/{tag}/{filename}"
        dest_zip = repo_root / filename
        _download_with_cache(url, filename, dest_zip, cache_dir)
        print(f"  Extracting …")
        with zipfile.ZipFile(dest_zip) as z:
            z.extractall(repo_root)
        dest_zip.unlink()
        # Make binary executable
        for p in repo_root.glob(f"Godot_*mono_linux*{arch_tag}*/Godot_*linux*"):
            if p.is_file() and p.suffix not in (".zip",):
                p.chmod(p.stat().st_mode | 0o111)
        print("  Godot binary ready.")
    elif host == "Darwin":
        app_binary = repo_root / "Godot_mono.app" / "Contents" / "MacOS" / "Godot"
        if app_binary.is_file():
            print(f"  Godot binary already present: {app_binary}")
        else:
            tag = _version_str_to_tag(version_str)
            filename = f"Godot_v{tag}_mono_macos.universal.zip"
            url = f"https://github.com/godotengine/godot/releases/download/{tag}/{filename}"
            dest_zip = repo_root / filename
            _download_with_cache(url, filename, dest_zip, cache_dir)
            print(f"  Extracting {filename} …")
            with zipfile.ZipFile(dest_zip) as z:
                z.extractall(repo_root)
            dest_zip.unlink()
            # The zip may contain a versioned .app name; rename to canonical Godot_mono.app
            for app in sorted(repo_root.glob("Godot*.app"), reverse=True):
                if app.name != "Godot_mono.app":
                    app.rename(repo_root / "Godot_mono.app")
                    break
            if app_binary.is_file():
                app_binary.chmod(app_binary.stat().st_mode | 0o111)
                print(f"  Godot binary ready: {app_binary}")
            else:
                print(f"  Warning: Godot_mono.app not found after extraction at {repo_root}",
                      file=sys.stderr)
    else:
        print(f"  Warning: Unsupported OS '{host}'. Download Godot manually.")


def _ensure_export_templates(repo_root: Path, workspace: Path,
                              version_str: Optional[str] = None,
                              cache_dir: Optional[Path] = None) -> None:
    """Extract export templates from mono_export_templates.tpz into workspace.

    If the tpz is absent but *version_str* is provided, the **mono** variant is
    downloaded from the Godot GitHub release.  The mono tpz includes C#-capable
    runtime templates required for .NET/GodotSharp projects.
    """
    tpz = repo_root / "mono_export_templates.tpz"

    if not tpz.exists():
        if version_str is None:
            print("  Warning: mono_export_templates.tpz not found — skipping.")
            return
        tag = _version_str_to_tag(version_str)
        # Use the mono export templates so exported binaries support C#.
        tpz_filename = f"Godot_v{tag}_mono_export_templates.tpz"
        url = (
            f"https://github.com/godotengine/godot/releases/download/"
            f"{tag}/{tpz_filename}"
        )
        print(f"  Downloading mono export templates from {url} …")
        print(f"  (This is ~1 GB — please be patient.)")
        _download_with_cache(url, tpz_filename, tpz, cache_dir)
        print(f"  Saved: {tpz}")

    detected = _read_godot_template_version(repo_root) or version_str or "unknown"
    ws_templates = workspace / ".godot" / "export_templates" / detected
    ws_templates.mkdir(parents=True, exist_ok=True)

    # Check for any release template to decide whether extraction is needed.
    already_extracted = (
        list(ws_templates.glob("linux_release.*"))
        or list(ws_templates.glob("macos*"))
        or list(ws_templates.glob("osx*"))
        or list(ws_templates.glob("version.txt"))
    )
    if not already_extracted:
        print(f"  Extracting export templates to {ws_templates} …")
        with zipfile.ZipFile(tpz) as z:
            for name in z.namelist():
                stripped = name[len("templates/"):] if name.startswith("templates/") else name
                if stripped:
                    (ws_templates / stripped).write_bytes(z.read(name))
        print("  Export templates extracted.")
    else:
        print(f"  Export templates already present: {ws_templates}")

    # Symlink Godot's expected template directory to ws_templates.
    # macOS: ~/Library/Application Support/Godot/export_templates/
    # Linux: ~/.local/share/godot/export_templates/
    host = platform.system()
    if host == "Darwin":
        local_base = Path.home() / "Library" / "Application Support" / "Godot" / "export_templates"
    else:
        local_base = Path.home() / ".local" / "share" / "godot" / "export_templates"

    try:
        local_base.mkdir(parents=True, exist_ok=True)
        # Create symlinks for both the bare version and the .mono variant.
        candidates = [detected]
        if not detected.endswith(".mono"):
            candidates.append(detected + ".mono")
        for ver_key in candidates:
            local_target = local_base / ver_key
            if not local_target.exists() and not local_target.is_symlink():
                local_target.symlink_to(ws_templates)
                print(f"  Symlinked {local_target} → {ws_templates}")
            else:
                print(f"  Template path already exists: {local_target}")
    except OSError as exc:
        print(f"  Warning: Could not symlink export template paths: {exc}")


# ─── Android helpers ──────────────────────────────────────────────────────


def _ensure_debug_keystore(keystore_path: Path) -> None:
    """Generate an Android debug keystore at *keystore_path* using keytool."""
    if keystore_path.exists():
        print(f"  Debug keystore exists: {keystore_path}")
        return
    keytool = shutil.which("keytool")
    if not keytool:
        print("  Warning: keytool not found — cannot generate debug keystore.", file=sys.stderr)
        return
    keystore_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Generating debug keystore at {keystore_path} …")
    subprocess.run([
        keytool, "-genkey", "-v",
        "-keystore", str(keystore_path),
        "-storepass", "android",
        "-alias", "androiddebugkey",
        "-keypass", "android",
        "-keyalg", "RSA",
        "-keysize", "2048",
        "-validity", "10000",
        "-dname", "CN=Android Debug,O=Android,C=US",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("  Debug keystore generated.")


def _patch_godot_editor_settings(settings_path: Path, patches: dict) -> None:
    """Create or update a Godot editor_settings-4.X.tres with the given key=value pairs.

    Each *value* must be a Godot-serialised literal, e.g. ``'"path/to/sdk"'`` for a
    String or ``'true'`` for a bool.  Existing keys are updated in-place; new keys are
    appended to the ``[resource]`` section.
    """
    if settings_path.exists():
        text = settings_path.read_text()
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        text = '[gd_resource type="EditorSettings" format=3]\n\n[resource]\n'

    for key, value in patches.items():
        pattern = rf'^{re.escape(key)} = .*$'
        replacement = f'{key} = {value}'
        if re.search(pattern, text, re.MULTILINE):
            text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
        else:
            if not text.endswith('\n'):
                text += '\n'
            text += f'{replacement}\n'

    settings_path.write_text(text)
    print(f"  Patched: {settings_path}")


def _patch_android_export_preset(project_dir: Path, patches: dict,
                                 force_keys: Optional[set] = None) -> None:
    """Patch key=value pairs in the [preset.N.options] section for the Android
    preset inside *project_dir*/export_presets.cfg.

    Each *value* in *patches* is a raw string (not quoted) — e.g. the
    actual path, not ``'"path"'``.  Empty-string values in the file are
    replaced; keys in *force_keys* are always overwritten regardless of the
    current value (used for environment-specific paths like keystores).
    """
    cfg_path = project_dir / "export_presets.cfg"
    if not cfg_path.exists():
        return

    force_keys = force_keys or set()
    lines = cfg_path.read_text().splitlines(keepends=True)

    # Pass 1: find preset indices whose name == "Android"
    android_preset_nums: set[str] = set()
    for line in lines:
        m = re.match(r'^\[preset\.(\d+)\]', line)
        if m:
            current_preset = m.group(1)
        if line.strip() == 'name="Android"':
            android_preset_nums.add(current_preset)

    # Pass 2: rewrite lines inside the matching [preset.N.options] sections
    in_android_options = False
    result = []
    for line in lines:
        m = re.match(r'^\[preset\.(\d+)\.options\]', line)
        if m:
            in_android_options = m.group(1) in android_preset_nums
        elif re.match(r'^\[', line):
            in_android_options = False

        if in_android_options:
            for key, value in patches.items():
                if key in force_keys:
                    # Always overwrite (environment-specific paths).
                    pattern = rf'^{re.escape(key)}=.*$'
                    if re.match(pattern, line):
                        line = f'{key}="{value}"\n'
                        break
                else:
                    # Only patch lines whose current value is empty ("").
                    pattern = rf'^({re.escape(key)}=)""\s*$'
                    if re.match(pattern, line):
                        line = f'{key}="{value}"\n'
                        break
        result.append(line)

    cfg_path.write_text("".join(result))
    print(f"  Patched export preset: {cfg_path}")


def _install_android_sdk(android_dir: Path,
                         cache_dir: Optional[Path] = None) -> Path:
    """Download Android cmdline-tools and install required SDK packages.

    Returns the SDK root directory (``android_dir/sdk``).
    Java (keytool, sdkmanager) must already be on PATH (install via mise first).
    """
    sdk_dir = android_dir / "sdk"
    cmdtools_dir = sdk_dir / "cmdline-tools" / "latest"

    if not (cmdtools_dir / "bin" / "sdkmanager").exists():
        zip_dest = android_dir / _ANDROID_CMDTOOLS_FILENAME
        _download_with_cache(_ANDROID_CMDTOOLS_URL, _ANDROID_CMDTOOLS_FILENAME,
                             zip_dest, cache_dir)

        tmp = android_dir / "_cmdtools_extract"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(zip_dest) as z:
            z.extractall(tmp)
        zip_dest.unlink()

        extracted = tmp / "cmdline-tools"
        cmdtools_dir.parent.mkdir(parents=True, exist_ok=True)
        if cmdtools_dir.exists() or cmdtools_dir.is_symlink():
            shutil.rmtree(cmdtools_dir)
        shutil.move(str(extracted), str(cmdtools_dir))
        shutil.rmtree(tmp, ignore_errors=True)
        # Ensure all scripts under bin/ are executable.
        for f in (cmdtools_dir / "bin").glob("*"):
            if f.is_file():
                f.chmod(f.stat().st_mode | 0o111)
        print(f"  Android cmdline-tools → {cmdtools_dir}")
    else:
        print(f"  Android cmdline-tools already present: {cmdtools_dir}")
        # Ensure scripts are executable even if extracted in a previous run.
        for f in (cmdtools_dir / "bin").glob("*"):
            if f.is_file():
                f.chmod(f.stat().st_mode | 0o111)

    sdkmanager = cmdtools_dir / "bin" / "sdkmanager"
    sdk_root_flag = f"--sdk_root={sdk_dir}"

    # Accept all SDK licenses non-interactively.
    print("  Accepting Android SDK licenses …")
    subprocess.run(
        [str(sdkmanager), sdk_root_flag, "--licenses"],
        input="y\n" * 20, text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # Install required packages (skip if marker directory already exists).
    packages: list[tuple[str, Path]] = [
        (f"build-tools;{_ANDROID_BUILD_TOOLS_VER}",
         sdk_dir / "build-tools" / _ANDROID_BUILD_TOOLS_VER),
        (_ANDROID_PLATFORM_PKG,
         sdk_dir / "platforms" / _ANDROID_PLATFORM_VER),
        ("platform-tools",
         sdk_dir / "platform-tools"),
    ]
    for pkg, marker in packages:
        if marker.is_dir():
            print(f"  Already installed: {pkg}")
            continue
        print(f"  Installing {pkg} …")
        subprocess.run(
            [str(sdkmanager), sdk_root_flag, pkg],
            input="y\n", text=True, check=True,
        )

    return sdk_dir


def _setup_android_for_export(project_dir: Path,
                               android_sdk_dir: Optional[Path] = None) -> None:
    """Configure environment and Godot editor settings for Android export.

    Locates (or accepts an explicit) Android SDK directory, ensures a debug
    keystore exists, then patches the project's Godot editor settings so the
    export step can find both.
    """
    # ── Locate SDK ────────────────────────────────────────────────────────
    sdk_dir = android_sdk_dir
    if sdk_dir is None:
        env_sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
        if env_sdk:
            sdk_dir = Path(env_sdk)
    if sdk_dir is None:
        repo_root = Path(__file__).resolve().parent.parent
        workspace = _find_workspace(repo_root)
        sdk_dir = workspace / ".android" / "sdk"

    if not sdk_dir.is_dir():
        # Auto-bootstrap: install Java (via mise) + Android SDK.
        _repo = Path(__file__).resolve().parent.parent
        _ws = _find_workspace(_repo)
        android_dir = sdk_dir.parent
        print(f"  Android SDK not found at {sdk_dir} — auto-installing …")
        mise_bin = _ws / ".mise" / "bin" / "mise"
        if not mise_bin.is_file():
            _found = shutil.which("mise")
            if not _found:
                print(
                    "  Error: mise not found — run 'prepare-env --android' first.",
                    file=sys.stderr,
                )
                return
            mise_bin = Path(_found)
        _write_mise_toml(_repo, include_java=True)
        _mise_install(mise_bin, _repo, _ws)
        android_dir.mkdir(parents=True, exist_ok=True)
        sdk_dir = _install_android_sdk(android_dir)
        _ensure_debug_keystore(android_dir / "debug.keystore")

    os.environ["ANDROID_SDK_ROOT"] = str(sdk_dir)
    os.environ["ANDROID_HOME"] = str(sdk_dir)

    # ── Locate / generate debug keystore ──────────────────────────────────
    keystore_path = sdk_dir.parent / "debug.keystore"
    if not keystore_path.exists():
        default_ks = Path.home() / ".android" / "debug.keystore"
        if default_ks.exists():
            keystore_path = default_ks
        else:
            _ensure_debug_keystore(keystore_path)

    # ── Patch Godot editor settings ───────────────────────────────────────
    # macOS: Godot reads from ~/Library/Application Support/Godot/ natively.
    # Linux: honour XDG_CONFIG_HOME (set to project-local dir to avoid filling /).
    if platform.system() == "Darwin":
        config_base = Path.home() / "Library" / "Application Support" / "Godot"
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME", str(project_dir / ".godot_config"))
        config_base = Path(config_home) / "godot"
    settings_files = sorted(config_base.glob("editor_settings-4.*.tres"))
    settings_path = settings_files[0] if settings_files else (config_base / "editor_settings-4.tres")

    # Locate Java SDK home (needed by Godot's Android export).
    java_sdk_path = ""
    java_home = os.environ.get("JAVA_HOME")
    if java_home and Path(java_home).is_dir():
        java_sdk_path = java_home
    else:
        # Ask java itself via -XshowSettings:property (most reliable across shims).
        java_bin = shutil.which("java")
        if java_bin:
            try:
                out = subprocess.run(
                    [java_bin, "-XshowSettings:property", "-version"],
                    capture_output=True, text=True,
                )
                for line in (out.stdout + out.stderr).splitlines():
                    if "java.home" in line:
                        java_sdk_path = line.split("=", 1)[1].strip()
                        break
            except Exception:
                pass
        # Fallback: search mise installs for java.
        if not java_sdk_path:
            repo_root_j = Path(__file__).resolve().parent.parent
            mise_data = os.environ.get("MISE_DATA_DIR", str(repo_root_j / ".mise"))
            for candidate in sorted(Path(mise_data, "installs", "java").glob("*/bin/java"),
                                    reverse=True):
                if candidate.is_file():
                    java_sdk_path = str(candidate.parent.parent)
                    break

    patches: dict = {
        "export/android/android_sdk_path": f'"{sdk_dir}"',
        "export/android/debug_keystore": f'"{keystore_path}"',
        "export/android/debug_keystore_pass": '"android"',
        "export/android/debug_user": '"androiddebugkey"',
        # Use the debug keystore for release builds too (dev / CI scenario).
        "export/android/release_keystore": f'"{keystore_path}"',
        "export/android/release_keystore_pass": '"android"',
        "export/android/release_keystore_user": '"androiddebugkey"',
    }
    if java_sdk_path:
        patches["export/android/java_sdk_path"] = f'"{java_sdk_path}"'

    _patch_godot_editor_settings(settings_path, patches)

    # Also patch export_presets.cfg — preset-level keystore values take
    # precedence over editor settings, so empty preset fields must be filled.
    preset_patches = {
        "keystore/debug": str(keystore_path),
        "keystore/debug_user": "androiddebugkey",
        "keystore/debug_password": "android",
        "keystore/release": str(keystore_path),
        "keystore/release_user": "androiddebugkey",
        "keystore/release_password": "android",
    }
    if java_sdk_path:
        preset_patches["gradle_build/java_sdk_path"] = java_sdk_path
    # Keystore paths are environment-specific; always overwrite even if non-empty.
    _keystore_force_keys = {
        "keystore/debug", "keystore/debug_user", "keystore/debug_password",
        "keystore/release", "keystore/release_user", "keystore/release_password",
    }
    _patch_android_export_preset(project_dir, preset_patches, force_keys=_keystore_force_keys)

    print(f"  Android SDK      : {sdk_dir}")
    print(f"  Debug keystore   : {keystore_path}")
    if java_sdk_path:
        print(f"  Java SDK         : {java_sdk_path}")


def _write_env_sh(repo_root: Path, workspace: Path, mise_bin: Path,
                  cache_dir: Optional[Path] = None,
                  android_sdk_dir: Optional[Path] = None) -> Path:
    """Write env.sh that activates the full environment in any sh-compatible shell."""
    mise_data = workspace / ".mise"
    nuget_pkg = (cache_dir / "nuget" / "packages") if cache_dir else (workspace / ".nuget" / "packages")
    mise_cache = (cache_dir / "mise") if cache_dir else (mise_data / "cache")
    tmp_dir = workspace / ".tmp"
    env_sh = repo_root / "env.sh"

    android_block = ""
    if android_sdk_dir and android_sdk_dir.is_dir():
        android_block = f"""
# Android SDK
export ANDROID_SDK_ROOT="{android_sdk_dir}"
export ANDROID_HOME="{android_sdk_dir}"
"""

    env_sh.write_text(f"""\
#!/usr/bin/env sh
# Generated by cli_builder.py prepare-env — source to activate the environment.
# Usage:  source env.sh   OR   . env.sh

export MISE_DATA_DIR="{mise_data}"
export MISE_CACHE_DIR="{mise_cache}"
export NUGET_PACKAGES="{nuget_pkg}"
export TMPDIR="{tmp_dir}"
export DOTNET_NOLOGO=1
export DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
export DOTNET_CLI_TELEMETRY_OPTOUT=1
eval "$("{mise_bin}" activate --shims 2>/dev/null || "{mise_bin}" activate bash 2>/dev/null || true)"

# Auto-detect Godot binary
if [ -z "$GODOT_BINARY" ]; then
  _os="$(uname -s)"
  if [ "$_os" = "Linux" ]; then
    for f in "{repo_root}"/Godot_*mono_linux*/Godot_*linux*; do
      case "$f" in *.zip|*.tar|*.gz) continue ;; esac
      [ -f "$f" ] && export GODOT_BINARY="$f" && break
    done
  elif [ "$_os" = "Darwin" ]; then
    for _app_dir in "{repo_root}" "$HOME/Applications" "/Applications"; do
      # Prefer Godot_mono.app, then any versioned mono .app
      for _app in "$_app_dir/Godot_mono.app" "$_app_dir"/Godot*mono*.app "$_app_dir"/Godot*.app; do
        _bin="$_app/Contents/MacOS/Godot"
        [ -f "$_bin" ] && export GODOT_BINARY="$_bin" && break 2
      done
    done
  fi
  unset _os _app_dir _app _bin
fi
{android_block}
echo "Environment activated."
echo "  dotnet : $(dotnet --version 2>/dev/null || echo 'not found')"
echo "  godot  : ${{GODOT_BINARY:-not found}}"
""")
    env_sh.chmod(env_sh.stat().st_mode | 0o755)
    print(f"  Wrote {env_sh}")
    return env_sh


def cmd_prepare_env(argv: list[str]) -> None:
    """prepare-env subcommand: install all prerequisites."""
    parser = argparse.ArgumentParser(
        prog="cli_builder.py prepare-env",
        description="Install prerequisites: mise, dotnet 9, Godot binary, export templates.",
    )
    parser.add_argument("--skip-mise", action="store_true",
                        help="Skip mise installation and dotnet provisioning.")
    parser.add_argument("--skip-godot", action="store_true",
                        help="Skip Godot editor binary download.")
    parser.add_argument("--skip-templates", action="store_true",
                        help="Skip export template extraction.")
    parser.add_argument(
        "--godot-version",
        default=None,
        metavar="VERSION",
        help=(
            "Godot version to download when mono_export_templates.tpz is absent. "
            "Accepts short form ('4.6', '4.6.2') or full form ('4.6.stable.mono'). "
            "Example: --godot-version 4.6"
        ),
    )
    parser.add_argument(
        "--package-cache-dir",
        default=None,
        metavar="DIR",
        help=(
            "Directory to cache downloaded packages (Godot binary zip, export templates tpz). "
            "Files are matched by filename; a cached file skips the network download. "
            "Downloaded files are also saved here for future reuse."
        ),
    )
    parser.add_argument(
        "--android",
        action="store_true",
        help=(
            "Install Android SDK (cmdline-tools, build-tools, platform-tools, android-34), "
            "generate a debug keystore, and add ANDROID_SDK_ROOT to env.sh. "
            "Requires Java 17 (automatically added to .mise.toml and installed)."
        ),
    )
    parser.add_argument(
        "--android-sdk-dir",
        default=None,
        metavar="DIR",
        help=(
            "Directory for the Android SDK installation "
            "(default: <workspace>/.android). "
            "The SDK will be placed in <DIR>/sdk/."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    workspace = _find_workspace(repo_root)
    cache_dir: Optional[Path] = Path(args.package_cache_dir).resolve() if args.package_cache_dir else None
    android_dir: Optional[Path] = (
        Path(args.android_sdk_dir).resolve() if args.android_sdk_dir
        else (workspace / ".android") if args.android
        else None
    )
    print(f"\n{'=' * 60}")
    print("  prepare-env")
    print(f"{'=' * 60}")
    print(f"  Repo root : {repo_root}")
    print(f"  Workspace : {workspace}")
    print(f"  OS/Arch   : {platform.system()}/{platform.machine()}")
    if cache_dir:
        print(f"  Cache dir : {cache_dir}")
    print()

    # Resolve Godot version: tpz takes priority; --godot-version is the fallback.
    version_str: Optional[str] = _read_godot_template_version(repo_root) or args.godot_version

    mise_bin: Optional[Path] = None

    if not args.skip_mise:
        print("[1/4] mise + dotnet" + (" + java" if args.android else ""))
        mise_bin = _ensure_mise(workspace)
        _write_mise_toml(repo_root, include_java=args.android)
        _mise_install(mise_bin, repo_root, workspace, cache_dir)
        print()

    if not args.skip_godot:
        print("[2/4] Godot editor binary")
        if version_str:
            _ensure_godot_binary(repo_root, version_str, cache_dir)
        else:
            print("  Warning: Godot version unknown — pass --godot-version X.Y or place")
            print("           mono_export_templates.tpz at the repo root.")
        print()

    if not args.skip_templates:
        print("[3/4] Export templates")
        _ensure_export_templates(repo_root, workspace, version_str, cache_dir)
        print()

    android_sdk_dir: Optional[Path] = None
    if args.android:
        step_num = 5
        print(f"[{step_num}/5] Android SDK")
        assert android_dir is not None
        android_dir.mkdir(parents=True, exist_ok=True)
        android_sdk_dir = _install_android_sdk(android_dir, cache_dir)
        _ensure_debug_keystore(android_dir / "debug.keystore")
        print()

    print(f"[{5 if args.android else 4}/{'5' if args.android else '4'}] Writing env.sh")
    if mise_bin is None:
        mise_bin = workspace / ".mise" / "bin" / "mise"
        if not mise_bin.is_file():
            mise_bin = Path(shutil.which("mise") or "mise")
    env_sh = _write_env_sh(repo_root, workspace, mise_bin, cache_dir,
                           android_sdk_dir=android_sdk_dir)
    print()

    print(f"{'=' * 60}")
    print("  Done!  Activate with:")
    print(f"    source {env_sh}")
    print(f"{'=' * 60}\n")


# ─── export helpers ───────────────────────────────────────────────────────


def resolve_project_dir(project_arg: Optional[str]) -> Path:
    """Return an absolute Path to the Godot project directory."""
    if project_arg:
        p = Path(project_arg).resolve()
    else:
        p = Path.cwd()
    if not (p / "project.godot").exists():
        print(f"Error: {p} does not contain a project.godot file.", file=sys.stderr)
        sys.exit(1)
    return p


def get_project_name(project_dir: Path) -> str:
    """Read the project name from project.godot."""
    godot_cfg = project_dir / "project.godot"
    for line in godot_cfg.read_text().splitlines():
        if line.startswith("config/name="):
            # config/name="test01"
            return line.split("=", 1)[1].strip().strip('"')
    return project_dir.name


def run(cmd: list[str], *, cwd: Path, label: str) -> None:
    """Run a subprocess, printing its output, and exit on failure."""
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"  cmd: {' '.join(cmd)}")
    print(f"{'=' * 60}\n")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"\nError: {label} failed (exit code {result.returncode}).", file=sys.stderr)
        sys.exit(result.returncode)


def step_import_resources(godot: Path, project_dir: Path) -> None:
    """Import / re-import project resources (needed on first build or after changes)."""
    run(
        [str(godot), "--headless", "--path", str(project_dir), "--import"],
        cwd=project_dir,
        label="Importing resources",
    )


def step_build_solutions(godot: Path, project_dir: Path) -> None:
    """Build the .NET solution via Godot's built-in MSBuild integration."""
    run(
        [str(godot), "--headless", "--path", str(project_dir), "--build-solutions", "--quit"],
        cwd=project_dir,
        label="Building .NET solution",
    )


def step_export(
    godot: Path,
    project_dir: Path,
    project_name: str,
    platform_key: str,
    export_type: str,
) -> Path:
    """Export the project for the given platform. Returns the output path."""
    info = PLATFORMS[platform_key]
    output_dir = project_dir / info["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)

    output_file = output_dir / f"{project_name}{info['output_suffix']}"

    # Clean previous export
    if output_file.exists():
        if output_file.is_dir():
            shutil.rmtree(output_file)
        else:
            output_file.unlink()

    flag = "--export-release" if export_type == "release" else "--export-debug"
    run(
        [str(godot), "--headless", "--path", str(project_dir), flag, info["preset"], str(output_file)],
        cwd=project_dir,
        label=f"Exporting {platform_key} ({export_type})",
    )

    if not output_file.exists():
        print(f"Error: Expected output not found: {output_file}", file=sys.stderr)
        sys.exit(1)

    return output_file


def step_verify_export(output_file: Path, platform_key: str) -> None:
    """Run basic sanity checks on the exported artifact."""
    if platform_key == "android":
        if output_file.is_file() and output_file.stat().st_size > 1024:
            size_mb = output_file.stat().st_size / (1024 * 1024)
            print(f"  ✓ APK: {output_file.name} ({size_mb:.1f} MB)")
        else:
            print(f"  Warning: APK not found or suspiciously small: {output_file}",
                  file=sys.stderr)
        return

    if platform_key == "macos":
        binary = output_file / "Contents" / "MacOS" / output_file.stem
        if not binary.exists():
            print(f"Warning: macOS binary not found at {binary}", file=sys.stderr)
            return
        # Check data dirs exist
        resources = output_file / "Contents" / "Resources"
        data_dirs = list(resources.glob("data_*"))
        if not data_dirs:
            print("Warning: No .NET data directories found in .app bundle.", file=sys.stderr)
        else:
            print(f"  ✓ Found {len(data_dirs)} .NET data dir(s): {[d.name for d in data_dirs]}")
    else:
        # Linux: check companion data dir
        data_pattern = f"data_{output_file.stem}_linuxbsd_*"
        data_dirs = list(output_file.parent.glob(data_pattern))
        if not data_dirs:
            print(f"Warning: No .NET data directory matching {data_pattern}", file=sys.stderr)
        else:
            print(f"  ✓ Found data dir: {data_dirs[0].name}")

    size_mb = sum(
        f.stat().st_size for f in (output_file.rglob("*") if output_file.is_dir() else [output_file])
        if f.is_file()
    ) / (1024 * 1024)
    print(f"  ✓ Export size: {size_mb:.1f} MB")


def _has_in_scene_tests(project_dir: Path) -> bool:
    """Return True if the project contains an InSceneTests/ directory."""
    return (project_dir / "InSceneTests").is_dir()


def start_echo_server(port: int = 8028) -> "subprocess.Popen[str]":
    """Start a simple TCP echo server on *port* in a background process.

    The server accepts connections, echoes every byte it receives, and closes
    the connection when the client shuts down the send side.  It loops until
    the process is terminated.
    """
    server_script = f"""\
import socket, sys
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', {port}))
srv.listen(8)
srv.settimeout(1.0)
while True:
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        continue
    except OSError:
        break
    try:
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                conn.sendall(data)
    except OSError:
        pass
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", server_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait until the port is accepting connections (up to 3 s).
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    print(f"  Echo server started on port {port} (pid {proc.pid})")
    return proc


def stop_echo_server(proc: "subprocess.Popen[str]", port: int = 8028) -> None:
    """Terminate the echo server process started by *start_echo_server*."""
    try:
        proc.terminate()
        proc.wait(timeout=5)
        print(f"  Echo server (pid {proc.pid}) stopped")
    except Exception as exc:
        print(f"  Warning: could not stop echo server: {exc}", file=sys.stderr)
        try:
            proc.kill()
        except Exception:
            pass


def _resolve_run_binary(output_file: Path, platform_key: str) -> Optional[Path]:
    """Return the executable to run for ``--run``, or *None* if not runnable on this host.

    Platform rules:
    - ``linux_arm64`` / ``linux_x86_64``: only on a matching Linux host arch.
    - ``macos``: only on a Darwin host; binary is inside the ``.app`` bundle at
      ``Contents/MacOS/<stem>``.
    - All other platforms: not runnable (Android, etc.).
    """
    host = platform.system()

    if platform_key in ("linux_arm64", "linux_x86_64"):
        if host != "Linux":
            print(f"  (run step skipped: host is {host}, binary is Linux)")
            return None
        arch = platform.machine()
        expected_arch = "aarch64" if platform_key == "linux_arm64" else "x86_64"
        if arch != expected_arch:
            print(f"  (run step skipped: host arch {arch} ≠ binary arch {expected_arch})")
            return None
        binary = output_file
        if not binary.is_file():
            print(f"  Warning: binary not found: {binary}", file=sys.stderr)
            return None
        binary.chmod(binary.stat().st_mode | 0o111)
        return binary

    if platform_key == "macos":
        if host != "Darwin":
            print(f"  (run step skipped for macOS: not on macOS host)")
            return None
        # Executable is inside the .app bundle: test-fondi.app/Contents/MacOS/test-fondi
        binary = output_file / "Contents" / "MacOS" / output_file.stem
        if not binary.is_file():
            print(f"  Warning: macOS binary not found at {binary}", file=sys.stderr)
            return None
        return binary

    print(f"  (run step skipped for {platform_key})")
    return None


def step_run_export(output_file: Path, platform_key: str, timeout_sec: int = 10,
                    project_dir: Optional[Path] = None) -> None:
    """Run the exported binary headlessly and print its stdout/stderr.

    Streams output line-by-line.  If the binary emits ``##GODOT_TEST_EOM##``
    the process is terminated immediately (early-exit on test completion).
    If *project_dir* contains an ``InSceneTests/`` directory an echo server is
    started on port 8028 before the binary and stopped afterwards.

    Supported: Linux (native arch), macOS (Darwin host only).
    """
    binary = _resolve_run_binary(output_file, platform_key)
    if binary is None:
        return

    in_scene = project_dir is not None and _has_in_scene_tests(project_dir)
    echo_proc: Optional["subprocess.Popen[str]"] = None

    if in_scene:
        print("\n  Detected InSceneTests/ — starting echo server on port 8028 …")
        echo_proc = start_echo_server(8028)

    eom_marker = "##GODOT_TEST_EOM##"
    print(f"\n  Running: {binary} --headless  (timeout {timeout_sec}s)")
    if in_scene:
        print(f"  (will stop early on '{eom_marker}')")

    proc = subprocess.Popen(
        [str(binary), "--headless"],
        cwd=binary.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    eom_event = threading.Event()

    def _stream() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                print(line, end="", flush=True)
                if eom_marker in line:
                    eom_event.set()
                    break
            # Drain remaining output after EOM (or on EOF).
            for line in proc.stdout:
                print(line, end="", flush=True)
        except ValueError:
            pass  # pipe closed

    stream_thread = threading.Thread(target=_stream, daemon=True)
    stream_thread.start()

    eom_found = eom_event.wait(timeout=timeout_sec)

    if eom_found:
        print(f"\n  ✓ EOM marker detected — stopping process")
    else:
        print(f"\n  (timeout {timeout_sec}s reached — stopping process)")

    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    stream_thread.join(timeout=2)

    try:
        if echo_proc is not None:
            stop_echo_server(echo_proc, 8028)
        if in_scene:
            _report_in_scene_xml(binary.parent)
    finally:
        print(f"  exit code: {proc.returncode}")



def _report_in_scene_xml(binary_dir: Path) -> None:
    """Parse and summarise the in-scene XML report if it was written."""
    xml_path = binary_dir / "TestResults" / "InSceneTestResults.xml"
    if not xml_path.exists():
        print(f"  (no XML report found at {xml_path})", file=sys.stderr)
        return
    try:
        root = ET.parse(xml_path).getroot()
        total  = root.get("total",  "?")
        passed = root.get("passed", "?")
        failed = root.get("failed", "?")
        print(f"\n  ✓ In-scene NUnit report: {xml_path}")
        print(f"    total={total}  passed={passed}  failed={failed}")
    except ET.ParseError as exc:
        print(f"  Warning: could not parse XML report: {exc}", file=sys.stderr)


def step_run_tests(project_dir: Path) -> Optional[Path]:
    """Run dotnet test on the tests/ subproject; return path to the NUnit XML report.

    Test failures (exit 1) are expected and logged but do not abort the process.
    """
    tests_dir = project_dir / "tests"
    if not tests_dir.exists():
        print(f"  No tests/ directory found in {project_dir} — skipping.")
        return None

    test_projs = list(tests_dir.glob("*.csproj"))
    if not test_projs:
        print(f"  No .csproj files found in {tests_dir} — skipping.")
        return None

    dotnet = shutil.which("dotnet")
    if not dotnet:
        print("Error: dotnet not found. Run 'prepare-env' first.", file=sys.stderr)
        sys.exit(1)

    results_dir = project_dir / "TestResults"
    results_dir.mkdir(parents=True, exist_ok=True)
    xml_path = results_dir / "TestResults.xml"

    for proj in test_projs:
        print(f"\n{'=' * 60}")
        print(f"  Running NUnit tests: {proj.name}")
        print(f"  XML report         : {xml_path}")
        print(f"{'=' * 60}\n")
        result = subprocess.run(
            [
                dotnet, "test", str(proj),
                f"--logger:nunit;LogFilePath={xml_path}",
                "--results-directory", str(results_dir),
            ],
            cwd=project_dir,
        )
        # exit 1 = test failures (expected in mixed pass/fail suites)
        if result.returncode not in (0, 1):
            print(
                f"\nError: dotnet test failed unexpectedly (exit {result.returncode}).",
                file=sys.stderr,
            )

    if xml_path.exists():
        try:
            root = ET.parse(xml_path).getroot()
            total  = root.get("total",  "?")
            passed = root.get("passed", "?")
            failed = root.get("failed", "?")
            print(f"\n  ✓ NUnit report : {xml_path}")
            print(f"    total={total}  passed={passed}  failed={failed}")
        except ET.ParseError:
            print(f"\n  ✓ NUnit report written: {xml_path}")
    return xml_path


def main() -> None:
    # Subcommand dispatch (keeps existing export flags backward-compatible).
    _SUBCOMMANDS = ("prepare-env",)
    if len(sys.argv) > 1 and sys.argv[1] in _SUBCOMMANDS:
        cmd = sys.argv.pop(1)
        if cmd == "prepare-env":
            cmd_prepare_env(sys.argv[1:])
        return

    parser = argparse.ArgumentParser(
        description="Build and export a Godot .NET/C# project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Subcommands:
  prepare-env               Install mise, dotnet, Godot binary, export templates.

Export examples:
  %(prog)s -p macos                           Export for macOS
  %(prog)s -p linux_x86_64 -p linux_arm64     Export for both Linux targets
  %(prog)s -p all                              Export all platforms
  %(prog)s -p all --export-type debug          Debug export
  %(prog)s -p macos --skip-build              Skip .NET build step
  %(prog)s -p linux_arm64 --project test-01 --run          Build & run (Linux)
  %(prog)s -p macos       --project test-01 --run          Build & run (macOS host)
  %(prog)s -p linux_arm64 --project test-02 --run-tests    Build & test

In-scene NUnit test runner (Godot process, EOM marker, echo server auto-started):
  %(prog)s -p linux_arm64 --project test-fondi --run       (Linux host)
  %(prog)s -p macos       --project test-fondi --run       (macOS host)

Pure C# test fast path (no Godot, no -p needed):
  %(prog)s --project test-fondi --run-pure-csharp-tests
        """,
    )
    parser.add_argument(
        "-p", "--platform",
        action="append",
        default=[],
        required=False,
        choices=list(PLATFORMS.keys()) + ["all"],
        help="Target platform(s). Use 'all' for every platform. Not required when --run-pure-csharp-tests is used.",
    )
    parser.add_argument(
        "--android-sdk-dir",
        default=None,
        metavar="DIR",
        help=(
            "Path to the Android SDK root (overrides ANDROID_SDK_ROOT / ANDROID_HOME). "
            "Used when -p android is specified."
        ),
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Path to the Godot project directory (default: current directory).",
    )
    parser.add_argument(
        "--godot",
        default=None,
        help="Path to the Godot editor binary (default: auto-detect).",
    )
    parser.add_argument(
        "--export-type",
        choices=["release", "debug"],
        default="release",
        help="Export type (default: release).",
    )
    parser.add_argument(
        "--skip-import",
        action="store_true",
        help="Skip the resource import step.",
    )
    parser.add_argument(
        "--force-import",
        action="store_true",
        help="Force resource import even when auto-detection says it is not needed.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip the .NET build step.",
    )
    parser.add_argument(
        "--force-build",
        action="store_true",
        help="Force dotnet build even when auto-detection says it is not needed.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing export directories before building.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the exported binary after building (Linux targets only, native arch). "
             "For projects with InSceneTests/, an echo server is started on port 8028 and "
             "the run stops as soon as the EOM marker is detected.",
    )
    parser.add_argument(
        "--run-timeout",
        type=int,
        default=60,
        help="Seconds to allow the binary to run before killing it (default: 60). "
             "The EOM marker causes early exit before the timeout is reached.",
    )
    parser.add_argument(
        "--run-tests",
        action="store_true",
        help="Run NUnit tests in tests/ after exporting and write TestResults/TestResults.xml.",
    )
    parser.add_argument(
        "--run-pure-csharp-tests",
        action="store_true",
        help=(
            "Build and run tests/tests.csproj directly with dotnet test, "
            "bypassing all Godot steps (no import, no build-solutions, no export). "
            "Platform-independent: uses the host dotnet runtime. "
            "-p/--platform is not required when this flag is used."
        ),
    )

    args = parser.parse_args()

    # Ensure .NET SDK is available on PATH before any build step
    ensure_dotnet_env()

    # ── Fast path: pure C# test run ──────────────────────────────────────
    # No Godot needed: just `dotnet test tests/` on the host runtime.
    # Platform-independent; -p is not required.
    if args.run_pure_csharp_tests:
        project_dir = resolve_project_dir(args.project)
        print(f"Project dir  : {project_dir}")
        print(f"Mode         : pure C# tests (no Godot, no export, platform-independent)")

        t_start = time.monotonic()
        xml_path = step_run_tests(project_dir)
        t_total = time.monotonic() - t_start

        print(f"\n{'=' * 60}")
        print("  Iteration Summary (pure C# tests)")
        print(f"{'=' * 60}")
        print(f"  tests : {t_total:.1f}s")
        print(f"  total : {t_total:.1f}s")
        if xml_path:
            print(f"  report: {xml_path}")
        print(f"{'=' * 60}\n")
        return

    # Suppress .NET first-run banners so Godot can parse dotnet output cleanly.
    os.environ.setdefault("DOTNET_NOLOGO", "1")
    os.environ.setdefault("DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "1")
    os.environ.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")

    # Platform is required for the full Godot build/export pipeline
    if not args.platform:
        parser.error("argument -p/--platform is required (or use --run-pure-csharp-tests for direct dotnet test)")

    # Resolve platforms
    platforms: list[str] = []
    for p in args.platform:
        if p == "all":
            platforms = list(PLATFORMS.keys())
            break
        if p not in platforms:
            platforms.append(p)

    # Locate Godot
    if args.godot:
        godot = Path(args.godot).resolve()
        if not godot.is_file():
            print(f"Error: Godot binary not found at {godot}", file=sys.stderr)
            sys.exit(1)
    else:
        godot = find_godot_binary()
        if godot is None:
            print(
                "Error: Could not find Godot binary. Set GODOT_BINARY env var or pass --godot.",
                file=sys.stderr,
            )
            sys.exit(1)

    project_dir = resolve_project_dir(args.project)
    project_name = get_project_name(project_dir)

    # On Linux (container environments), redirect Godot editor config to a
    # project-local dir so we never write to a potentially full root filesystem.
    # On macOS, Godot checks XDG_CONFIG_HOME first but also uses native macOS
    # paths for data (templates); mismatched config vs. data paths break template
    # resolution, so we leave the env var alone on macOS.
    if platform.system() == "Linux":
        godot_config_dir = project_dir / ".godot_config"
        godot_config_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("XDG_CONFIG_HOME", str(godot_config_dir))

        # Also point XDG_DATA_HOME at a project-local dir that symlinks to the
        # workspace-level export_templates directory.  This makes template
        # discovery reliable across container restarts (no dependency on
        # ~/.local/share/godot symlinks from a previous prepare-env run).
        repo_root_local = Path(__file__).resolve().parent.parent
        workspace_local = _find_workspace(repo_root_local)
        ws_tpl = workspace_local / ".godot" / "export_templates"
        if ws_tpl.is_dir():
            godot_data_dir = project_dir / ".godot_data"
            link_target = godot_data_dir / "godot" / "export_templates"
            link_target.parent.mkdir(parents=True, exist_ok=True)
            if not link_target.exists() and not link_target.is_symlink():
                link_target.symlink_to(ws_tpl)
            os.environ.setdefault("XDG_DATA_HOME", str(godot_data_dir))

    print(f"Godot binary : {godot}")
    print(f"Project dir  : {project_dir}")
    print(f"Project name : {project_name}")
    print(f"Platforms    : {', '.join(platforms)}")
    print(f"Export type  : {args.export_type}")

    # Optional: clean
    if args.clean:
        export_root = project_dir / "export"
        if export_root.exists():
            print(f"\nCleaning {export_root} ...")
            shutil.rmtree(export_root)

    t_start = time.monotonic()
    metrics = _load_metrics(project_dir)
    step_times: dict[str, float] = {}
    steps_skipped: list[str] = []

    # Step 1: Import resources
    if args.skip_import:
        steps_skipped.append("import")
        print("\n  (resource import: skipped via --skip-import)")
    else:
        needed, reason = (True, "forced") if args.force_import else _needs_import(project_dir)
        if not needed:
            steps_skipped.append("import")
            print(f"\n  ✓ Resource import: auto-skipped ({reason})")
        else:
            if not args.force_import:
                print(f"\n  Resource import needed: {reason}")
            t0 = time.monotonic()
            step_import_resources(godot, project_dir)
            step_times["import"] = time.monotonic() - t0
            _write_stamp(project_dir, "import")
            metrics["import_duration"] = step_times["import"]

    # Step 2: Build .NET solution
    if args.skip_build:
        steps_skipped.append("build")
        print("\n  (dotnet build: skipped via --skip-build)")
    else:
        needed, reason = (True, "forced") if args.force_build else _needs_build(project_dir)
        if not needed:
            steps_skipped.append("build")
            print(f"\n  ✓ Dotnet build: auto-skipped ({reason})")
        else:
            if not args.force_build:
                print(f"\n  Dotnet build needed: {reason}")
            t0 = time.monotonic()
            step_build_solutions(godot, project_dir)
            step_times["build"] = time.monotonic() - t0
            _write_stamp(project_dir, "build")
            metrics["build_duration"] = step_times["build"]

    # Step 3: Export each platform
    # Android-specific setup (SDK path, keystore, editor settings)
    if "android" in platforms:
        print("\n  Android export setup …")
        android_sdk_path = Path(args.android_sdk_dir).resolve() if args.android_sdk_dir else None
        _setup_android_for_export(project_dir, android_sdk_path)

    t0 = time.monotonic()
    results: list[tuple[str, Path]] = []
    for plat_key in platforms:
        output = step_export(godot, project_dir, project_name, plat_key, args.export_type)
        results.append((plat_key, output))
    step_times["export"] = time.monotonic() - t0
    metrics["export_duration"] = step_times["export"]

    # Step 4: Verify
    print(f"\n{'=' * 60}")
    print("  Export Summary")
    print(f"{'=' * 60}")
    for plat, output in results:
        print(f"\n[{plat}] → {output}")
        step_verify_export(output, plat)

    # Step 5 (optional): Run exported binary
    if args.run:
        print(f"\n{'=' * 60}")
        print("  Run")
        print(f"{'=' * 60}")
        for plat, output in results:
            print(f"\n[{plat}]")
            step_run_export(output, plat, timeout_sec=args.run_timeout,
                            project_dir=project_dir)

    # Step 6 (optional): NUnit tests
    if args.run_tests:
        t0 = time.monotonic()
        step_run_tests(project_dir)
        step_times["tests"] = time.monotonic() - t0
        metrics["tests_duration"] = step_times["tests"]

    t_total = time.monotonic() - t_start
    _save_metrics(project_dir, metrics)

    print(f"\n{'=' * 60}")
    print(f"  All {len(results)} export(s) completed successfully!")
    print(f"{'=' * 60}")

    # Iteration summary
    print(f"\n{'=' * 60}")
    print("  Iteration Summary")
    print(f"{'=' * 60}")
    for step, label in (("import", "import"), ("build", "build "), ("export", "export"), ("tests", "tests ")):
        if step in steps_skipped:
            hist = metrics.get(f"{step}_duration")
            saved_str = f"  (saved ~{hist:.0f}s from last run)" if hist else ""
            print(f"  {label} : SKIPPED{saved_str}")
        elif step in step_times:
            print(f"  {label} : {step_times[step]:.1f}s")
    print(f"  {'total'} : {t_total:.1f}s")

    total_saved = sum(
        metrics.get(f"{s}_duration", 0) for s in steps_skipped
        if metrics.get(f"{s}_duration")
    )
    if total_saved > 0:
        print(f"\n  ⚡ ~{total_saved:.0f}s saved vs full build (incremental run)")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
