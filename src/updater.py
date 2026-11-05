"""Standard-library startup bootstrapper, GitHub self-updater, and rtl_433 binary manager for SkyScry.

IMPORTANT: This module MUST use ONLY Python standard-library imports so it can execute
before `pip install -r requirements.txt` on a brand-new machine with zero third-party packages.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src import __version__

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
LAST_DEP_CHECK_FILE = DATA_DIR / ".last_dep_check"
RTL433_VERSION_FILE = PROJECT_ROOT / ".rtl_433_version"
REPO_URL = os.environ.get("SKYSCRY_REPO_URL", "https://github.com/Cave404/SkyScry.git")


def _checkpoint_sqlite_wal() -> None:
    """Safely flush and truncate SQLite WAL journal before any git pull or process re-exec."""
    db_file = DATA_DIR / "skyscry.db"
    if not db_file.is_file():
        return
    try:
        conn = sqlite3.connect(str(db_file), timeout=5.0)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            conn.close()
    except Exception:
        pass

RTL433_RELEASES_API = "https://api.github.com/repos/merbanan/rtl_433/releases/latest"
DEP_CHECK_INTERVAL_SECONDS = 86400.0  # 24 hours

REQUIRED_WINDOWS_FILES: tuple[str, ...] = (
    "rtl_433-rtlsdr.exe",
    "rtlsdr.dll",
    "libusb-1.0.dll",
)

# Mapping from pip package name in requirements.txt to importable module name
PACKAGE_IMPORT_MAP: dict[str, str] = {
    "pyyaml": "yaml",
    "flask": "flask",
    "waitress": "waitress",
    "paho-mqtt": "paho",
}


def _log(msg: str) -> None:
    """Print a clean, timestamped bootstrap log message to stdout."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | INFO    | [UPDATER] {msg}", flush=True)


def _warn(msg: str) -> None:
    """Print a clean, timestamped bootstrap warning to stdout."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | WARNING | [UPDATER] {msg}", flush=True)


def _ensure_git_safe_directory(git_bin: str) -> None:
    """Configure global safe.directory so Docker container root ownership never triggers dubious ownership."""
    try:
        subprocess.run(
            [git_bin, "config", "--global", "--add", "safe.directory", "*"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except Exception:
        pass


def ensure_git_repo_initialized() -> bool:
    """Ensure `.git` exists in `PROJECT_ROOT`, initializing it in-place from GitHub if running in a Docker image without `.git`."""
    git_bin = shutil.which("git")
    if not git_bin:
        return False

    _ensure_git_safe_directory(git_bin)
    git_dir = PROJECT_ROOT / ".git"
    if git_dir.exists():
        return True

    _log(f"No .git metadata found in {PROJECT_ROOT}; initializing in-place Git tracking from {REPO_URL}...")
    try:
        init_res = subprocess.run(
            [git_bin, "init", "-b", "main"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if init_res.returncode != 0:
            subprocess.run(
                [git_bin, "init"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )

        subprocess.run(
            [git_bin, "remote", "add", "origin", REPO_URL],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
        subprocess.run(
            [git_bin, "remote", "set-url", "origin", REPO_URL],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )

        fetch_res = subprocess.run(
            [git_bin, "fetch", "origin", "main", "--depth=1"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=12.0,
            check=False,
        )
        if fetch_res.returncode == 0:
            subprocess.run(
                [git_bin, "reset", "--mixed", "origin/main"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=6.0,
                check=False,
            )
            subprocess.run(
                [git_bin, "branch", "-M", "main"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=4.0,
                check=False,
            )
            _log("In-place Git repository initialized and synchronized with origin/main.")
            return True
    except Exception as exc:
        _warn(f"In-place Git initialization skipped: {exc}")
    return git_dir.exists()


def get_git_commit_info() -> dict[str, Optional[str]]:
    """Return current local short Git commit hash and branch using standard-library subprocess."""
    git_dir = PROJECT_ROOT / ".git"
    git_bin = shutil.which("git")
    if not git_bin:
        return {"commit_short": None, "branch": None, "remote_commit_short": None}

    _ensure_git_safe_directory(git_bin)
    if not git_dir.exists():
        ensure_git_repo_initialized()
    if not git_dir.exists():
        return {"commit_short": None, "branch": None, "remote_commit_short": None}

    commit_short: Optional[str] = None
    branch: Optional[str] = None
    remote_short: Optional[str] = None
    try:
        res_head = subprocess.run(
            [git_bin, "rev-parse", "--short", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if res_head.returncode == 0:
            commit_short = res_head.stdout.strip()

        res_branch = subprocess.run(
            [git_bin, "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if res_branch.returncode == 0:
            branch = res_branch.stdout.strip()

        res_remote = subprocess.run(
            [git_bin, "rev-parse", "--short", "origin/main"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if res_remote.returncode == 0:
            remote_short = res_remote.stdout.strip()
    except Exception:
        pass

    return {
        "commit_short": commit_short,
        "branch": branch,
        "remote_commit_short": remote_short,
    }


def check_and_apply_git_update() -> dict[str, Any]:
    """Fetch `origin/main` with an 8s timeout and pull if local HEAD is behind upstream."""
    git_bin = shutil.which("git")
    if not git_bin:
        return {"updated": False, "reason": "git_unavailable"}

    if not ensure_git_repo_initialized():
        return {"updated": False, "reason": "git_init_failed"}

    try:
        fetch_res = subprocess.run(
            [git_bin, "fetch", "origin", "main"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=8.0,
            check=False,
        )
        if fetch_res.returncode != 0:
            return {"updated": False, "reason": "fetch_failed"}

        local_head = subprocess.run(
            [git_bin, "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        ).stdout.strip()

        remote_head = subprocess.run(
            [git_bin, "rev-parse", "origin/main"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        ).stdout.strip()

        if not remote_head:
            return {"updated": False, "reason": "no_remote_head"}

        if local_head == remote_head:
            return {"updated": False, "local_head": local_head[:7], "remote_head": remote_head[:7]}

        # Check if local is behind origin/main (or if HEAD was freshly initialized)
        behind_res = subprocess.run(
            [git_bin, "rev-list", "--count", "HEAD..origin/main"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
        behind_count = int(behind_res.stdout.strip() or "0") if behind_res.returncode == 0 else 1
        if behind_count <= 0 and local_head:
            return {"updated": False, "local_head": local_head[:7], "remote_head": remote_head[:7]}

        _log(
            f"New SkyScry build detected on GitHub ({behind_count} commit(s) ahead). Pulling latest commit..."
        )
        pull_res = subprocess.run(
            [git_bin, "pull", "--rebase", "--autostash", "origin", "main"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=20.0,
            check=False,
        )
        if pull_res.returncode != 0:
            # Fallback for shallow/in-place initialized Docker working tree (never touches untracked /app/data or /app/config)
            pull_res = subprocess.run(
                [git_bin, "reset", "--hard", "origin/main"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
        if pull_res.returncode == 0:
            _log(f"Successfully updated repository to {remote_head[:7]}.")
            return {
                "updated": True,
                "previous_head": local_head[:7] if local_head else "init",
                "new_head": remote_head[:7],
                "commits_pulled": behind_count,
            }
        _warn(f"git update returned non-zero ({pull_res.returncode}): {pull_res.stderr.strip()[:160]}")
        return {"updated": False, "reason": "pull_failed"}
    except subprocess.TimeoutExpired:
        _warn("GitHub update check timed out after 8s; continuing with local build.")
        return {"updated": False, "reason": "timeout"}
    except Exception as exc:
        _warn(f"GitHub update check skipped: {exc}")
        return {"updated": False, "reason": str(exc)}


def _parse_required_packages() -> list[str]:
    """Extract package names from `requirements.txt`."""
    if not REQUIREMENTS_FILE.is_file():
        return []
    pkgs: list[str] = []
    for raw_line in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)", line)
        if match:
            pkgs.append(match.group(1).lower())
    return pkgs


def are_dependencies_missing() -> bool:
    """Return True if any package in `requirements.txt` is not currently installed."""
    for pkg in _parse_required_packages():
        mod_name = PACKAGE_IMPORT_MAP.get(pkg, pkg.replace("-", "_"))
        if importlib.util.find_spec(mod_name) is None:
            try:
                importlib.metadata.version(pkg)
            except importlib.metadata.PackageNotFoundError:
                return True
    return False


def _is_dep_upgrade_due(force: bool = False) -> bool:
    """Return True if `.last_dep_check` is missing or older than 24 hours."""
    if force or not LAST_DEP_CHECK_FILE.is_file():
        return True
    try:
        last_ts = float(LAST_DEP_CHECK_FILE.read_text(encoding="utf-8").strip())
        return (time.time() - last_ts) >= DEP_CHECK_INTERVAL_SECONDS
    except Exception:
        return True


def _write_dep_check_timestamp() -> None:
    """Record current epoch timestamp to `data/.last_dep_check`."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LAST_DEP_CHECK_FILE.write_text(f"{time.time():.0f}\n", encoding="utf-8")
    except Exception:
        pass


def verify_and_upgrade_dependencies(force: bool = False) -> dict[str, Any]:
    """Install any missing Python packages and upgrade `requirements.txt` every 24h or on Git update."""
    missing = are_dependencies_missing()
    due = _is_dep_upgrade_due(force=force)
    if not missing and not due:
        return {"upgraded": False, "reason": "up_to_date"}

    if missing:
        _log("Missing Python dependencies detected. Installing packages from requirements.txt...")
    else:
        _log("Verifying and upgrading Python dependencies from requirements.txt...")

    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
            cwd=str(PROJECT_ROOT),
            timeout=60.0,
            check=False,
        )
        res = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "-r",
                str(REQUIREMENTS_FILE),
                "--quiet",
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120.0,
            check=False,
        )
        if res.returncode == 0:
            _write_dep_check_timestamp()
            return {"upgraded": True, "missing_installed": missing}
        _warn(f"pip install returned code {res.returncode}: {res.stderr.strip()[:160]}")
    except Exception as exc:
        _warn(f"Dependency upgrade check skipped: {exc}")

    return {"upgraded": False, "reason": "pip_error"}


def get_rtl433_version_info() -> dict[str, Any]:
    """Return local `rtl_433` release tag and binary presence status."""
    tag: Optional[str] = None
    if RTL433_VERSION_FILE.is_file():
        try:
            tag = RTL433_VERSION_FILE.read_text(encoding="utf-8").strip() or None
        except Exception:
            pass

    if os.name == "nt":
        binaries_present = all(
            (PROJECT_ROOT / fname).is_file() or (PROJECT_ROOT / "bin" / fname).is_file()
            for fname in REQUIRED_WINDOWS_FILES
        )
        if not tag and binaries_present:
            tag = "local-msvc-x64"
    else:
        binaries_present = shutil.which("rtl_433") is not None or (PROJECT_ROOT / "bin" / "rtl_433").is_file()
        if not tag and binaries_present:
            tag = "system-rtl_433"

    return {
        "rtl433_version": tag,
        "binaries_present": binaries_present,
    }


def ensure_rtl433_windows_binaries(force_check: bool = False) -> dict[str, Any]:
    """Verify `rtl_433-rtlsdr.exe` and companion `.dll` files exist on Windows and update from GitHub Releases."""
    if os.name != "nt":
        return {"updated": False, "reason": "non_windows"}

    missing_files = [
        fname
        for fname in REQUIRED_WINDOWS_FILES
        if not (PROJECT_ROOT / fname).is_file() and not (PROJECT_ROOT / "bin" / fname).is_file()
    ]

    stored_tag = None
    if RTL433_VERSION_FILE.is_file():
        try:
            stored_tag = RTL433_VERSION_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    # Avoid hitting GitHub Releases API on every restart unless files are missing,
    # `.rtl_433_version` is absent, or `force_check` is requested.
    if not missing_files and stored_tag and not force_check:
        return {"updated": False, "tag": stored_tag, "binaries_present": True}

    req = urllib.request.Request(
        RTL433_RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"SkyScry/{__version__} (https://github.com/Cave404/SkyScry)",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            release_data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        if missing_files:
            _warn(f"Could not querymerbanan/rtl_433 GitHub release ({exc}); missing: {missing_files}")
        elif not stored_tag:
            # All local binaries already exist in workspace; stamp marker file so we know they are present
            try:
                RTL433_VERSION_FILE.write_text("local-bundle\n", encoding="utf-8")
            except Exception:
                pass
        return {"updated": False, "reason": "github_api_unreachable", "binaries_present": not missing_files}

    latest_tag = str(release_data.get("tag_name") or "").strip()
    assets = release_data.get("assets") or []

    # If all required files exist and stored_tag matches latest_tag, no download needed
    if not missing_files and stored_tag and latest_tag and stored_tag == latest_tag:
        return {"updated": False, "tag": latest_tag, "binaries_present": True}

    # If all required files already exist locally and this is just the initial marker creation,
    # record the tag without overwriting working local binaries unless missing_files or force_check is True.
    if not missing_files and not stored_tag and not force_check and latest_tag:
        try:
            RTL433_VERSION_FILE.write_text(f"{latest_tag}\n", encoding="utf-8")
        except Exception:
            pass
        return {"updated": False, "tag": latest_tag, "binaries_present": True}

    # Locate the 64-bit Windows MSVC zip asset
    download_url: Optional[str] = None
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith(".zip") and ("win-msvc-x64" in name or "win-x64" in name or "win64" in name):
            download_url = asset.get("browser_download_url")
            break

    if not download_url:
        return {"updated": False, "reason": "no_win64_asset_found", "tag": latest_tag}

    _log(f"Downloading rtl_433 Windows x64 binaries (release '{latest_tag}')...")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "rtl_433_win64.zip"
            dl_req = urllib.request.Request(
                download_url,
                headers={"User-Agent": f"SkyScry/{__version__}"},
            )
            with urllib.request.urlopen(dl_req, timeout=30.0) as dl_resp:
                zip_path.write_bytes(dl_resp.read())

            extracted_files: list[str] = []
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    base_name = Path(member.filename).name
                    lower_name = base_name.lower()
                    if lower_name.endswith(".exe") or lower_name.endswith(".dll"):
                        target_file = PROJECT_ROOT / base_name
                        with zf.open(member, "r") as src_fh:
                            target_file.write_bytes(src_fh.read())
                        extracted_files.append(base_name)

            # If archive provides `rtl_433.exe` but not `rtl_433-rtlsdr.exe`, copy to `rtl_433-rtlsdr.exe` for consistency
            if (PROJECT_ROOT / "rtl_433.exe").is_file() and not (PROJECT_ROOT / "rtl_433-rtlsdr.exe").is_file():
                shutil.copy2(PROJECT_ROOT / "rtl_433.exe", PROJECT_ROOT / "rtl_433-rtlsdr.exe")

            if latest_tag:
                RTL433_VERSION_FILE.write_text(f"{latest_tag}\n", encoding="utf-8")

            _log(
                f"rtl_433 binaries and DLLs updated to release {latest_tag} ({len(extracted_files)} files extracted)."
            )
            return {
                "updated": True,
                "tag": latest_tag,
                "extracted_count": len(extracted_files),
                "binaries_present": True,
            }
    except Exception as exc:
        _warn(f"Failed to download/extract rtl_433 release {latest_tag}: {exc}")
        return {"updated": False, "reason": str(exc), "binaries_present": not missing_files}


def get_full_update_status() -> dict[str, Any]:
    """Compile version, Git commit hash, and `rtl_433` release status for the WebUI header."""
    git_info = get_git_commit_info()
    rtl_info = get_rtl433_version_info()
    last_dep_iso: Optional[str] = None
    if LAST_DEP_CHECK_FILE.is_file():
        try:
            ts = float(LAST_DEP_CHECK_FILE.read_text(encoding="utf-8").strip())
            last_dep_iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass

    return {
        "version": __version__,
        "git_commit": git_info["commit_short"],
        "git_branch": git_info["branch"],
        "git_remote_commit": git_info["remote_commit_short"],
        "rtl433_version": rtl_info["rtl433_version"],
        "rtl433_binaries_present": rtl_info["binaries_present"],
        "last_dependency_check": last_dep_iso,
    }


def trigger_manual_update() -> dict[str, Any]:
    """Run an on-demand Git + pip + rtl_433 update check from the WebUI (`POST /api/trigger-update`)."""
    git_res = check_and_apply_git_update()
    dep_res = verify_and_upgrade_dependencies(force= bool(git_res.get("updated")))
    rtl_res = ensure_rtl433_windows_binaries(force_check=True)
    status = get_full_update_status()
    return {
        "status": "ok",
        "git": git_res,
        "dependencies": dep_res,
        "rtl433": rtl_res,
        "current": status,
    }


def run_startup_bootstrap(argv: Optional[list[str]] = None) -> None:
    """Execute the pre-flight startup sequence before third-party imports in `run.py`."""
    args = argv if argv is not None else sys.argv[1:]

    # Fast-exit flags that should never run network bootstrap
    if any(flag in args for flag in ("-h", "--help", "-V", "--version", "--no-update", "--skip-bootstrap")):
        return

    if os.environ.get("SKYSCRY_NO_UPDATE") == "1":
        return

    git_res = check_and_apply_git_update()
    git_updated = bool(git_res.get("updated"))

    dep_res = verify_and_upgrade_dependencies(force=git_updated)
    deps_installed = bool(dep_res.get("missing_installed"))

    ensure_rtl433_windows_binaries(force_check=False)

    # Re-exec the Python process if Git pulled new source code or missing packages were just installed
    if (git_updated or deps_installed) and "--skip-bootstrap" not in sys.argv:
        _log("Restarting SkyScry process to load updated code/dependencies...")
        _checkpoint_sqlite_wal()
        os.environ["SKYSCRY_RESTARTED"] = "1"
        new_argv = [sys.executable, *sys.argv, "--skip-bootstrap"]
        os.execv(sys.executable, new_argv)

