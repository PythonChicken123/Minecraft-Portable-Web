#!/usr/bin/env python3
r"""
Cross-platform bootstrap script for embedded Python 3.14 (Windows only).
All files are stored in %LOCALAPPDATA%\PortableMC.
Run with any Python (e.g., system 3.11) to set up and launch the launcher.

Launch modes
============
1  Web launcher      – pmc_web.py Flask server, browser UI, subprocess game
2  MSBuild launcher  – Launcher.targets (Microsoft-signed binaries)
3  CLI launcher      – portablemc CLI in terminal, subprocess game
4  In-Process        – Memory-resident JVM via JvmBootGlue + JPype (no java.exe)
d  Debug menu
q  Quit
"""

import datetime
import json
import os
import platform
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import traceback
import urllib.request
import winreg
import zipfile
from pathlib import Path

# --- Windows-only guard ---
if platform.system().lower() != "windows":
    print("❌ This bootstrap script currently only supports Windows.")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════════════
# § 1  CONFIGURATION — original + in-process additions
# ═══════════════════════════════════════════════════════════════════════════════

ROOT_DIR = Path(__file__).parent
PROJECT_LOGS_DIR = ROOT_DIR / "logs"
PROJECT_LOGS_DIR.mkdir(parents=True, exist_ok=True)

APPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
LOCAL_BASE_DIR = APPDATA / "PortableMC"
BASE_DIR = LOCAL_BASE_DIR
EMBEDDED_DIR = BASE_DIR / "python"
EMBEDDED_PYTHON = EMBEDDED_DIR / "python.exe"
PORTABLEMC_VERSION = "5.0.2"
PORTABLEMC_RELEASE_BASE = (
    f"https://github.com/mindstorm38/portablemc/releases/download/v{PORTABLEMC_VERSION}"
)
PYTHON_VERSION = "3.14.3"
PYTHON_VERSIONS = ["3.15", "3.14", "3.13", "3.12", "3.11"]
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
BASE_PACKAGES = ["flask", "flask-socketio", "psutil", "ansi2html", "certifi", "vswhere"]
PORTABLEMC_BIN_DIR = BASE_DIR / "portablemc_bin"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
ALLOW_INSECURE_SSL = (
    os.environ.get("ALLOW_INSECURE_SSL", "").strip().lower() in TRUTHY_ENV_VALUES
)

# Default game settings
DEFAULT_USERNAME = "CubeUniform840"
DEFAULT_SERVER_IP = "play.echoruins.com"
DEFAULT_JVM_OPTS = "-Xmx3G -Xms3G"

# Paths
SCRIPTS_DIR = ROOT_DIR / "scripts"
_SCRIPTS_POS = str(SCRIPTS_DIR.resolve())
if _SCRIPTS_POS not in sys.path:
    sys.path.insert(0, _SCRIPTS_POS)
SCRIPTS_DIR.mkdir(exist_ok=True)

try:
    import local_portablemc as _local_portablemc

    _local_portablemc.prepend_vendor_portablemc(scripts_dir=SCRIPTS_DIR)
except ImportError:
    pass

MSBUILD_PATH = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\MSBuild.exe"

# Launcher files inside the Scripts folder
TARGETS_FILE = SCRIPTS_DIR / "Launcher.targets"
LAUNCHER_VBS = SCRIPTS_DIR / "Launcher.vbs"
LAUNCHER_PS1 = SCRIPTS_DIR / "Launcher.ps1"
PORTABLEMC_PY = SCRIPTS_DIR / "pmc_web.py"
CONFIG_PATH = ROOT_DIR / "launcher_config.json"
LAUNCHER_STATE = {}
ACTIVE_TRUSTED_DIR = None

# ── In-Process launcher constants ────────────────────────────────────────────
# Absolute path to the JDK bin directory (contains jvm.dll in bin/server/)
JDK_BIN = Path(
    r"C:\Users\wave6\Downloads"
    r"\OpenJDK25U-jdk_x64_windows_hotspot_25.0.3_9"
    r"\jdk-25.0.3+9\bin"
)

# portablemc Mojang runtime (often JRE 21) — canonical source for manual-mapped
# JVM in option 5 (HotSpot quirks with newer standalone JDK blobs are avoided).
MEMORY_JVM_BIN_REL = Path("jvm") / "java-runtime-epsilon" / "bin"

# Minecraft version string forwarded to portablemc's installer
INPROCESS_VERSION = "fabric:"

# Python packages that must be importable for in-process mode.
# pythonmemorymodule ships as source inside scripts/ — only jpype1 needs pip.
INPROCESS_PIP_DEPS = ["jpype1"]

# ── Glue module paths ─────────────────────────────────────────────────────────
# Both scripts live in SCRIPTS_DIR and import each other by name.
JVM_BOOT_GLUE_PY = SCRIPTS_DIR / "jvm_boot_glue.py"
JVM_MEMORY_LOADER_PY = SCRIPTS_DIR / "jvm_memory_loader.py"

# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "schema_version": 1,
    "success": False,
    "last_error": "",
    "last_run_mode": "",
    "last_known_working_directory": str(ROOT_DIR),
    "last_base_dir": str(BASE_DIR),
    "trusted_dir": "",
    "trusted_dir_candidates": [],
    "trusted_dir_probes": {},
    "config_sync_mode": "copy",
    "installer_backend": "",
    "managed_links": {},
    "allowlist_trusted_dirs": [],
    "include_sensitive_env_details": False,
}


# ═══════════════════════════════════════════════════════════════════════════════
# § 2  CONFIG HELPERS  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def _safe_json_dump(path, payload):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        return True
    except Exception:
        return False


def _load_json_file(path):
    try:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _get_builtin_trusted_candidates():
    user_profile = Path(os.environ.get("USERPROFILE", Path.home()))
    local_app = Path(os.environ.get("LOCALAPPDATA", user_profile / "AppData/Local"))
    roaming_app = Path(os.environ.get("APPDATA", user_profile / "AppData/Roaming"))
    temp_dir = Path(os.environ.get("TEMP", local_app / "Temp"))
    program_data = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    public_profile = Path(os.environ.get("PUBLIC", r"C:\Users\Public"))
    return [
        local_app / "PortableMC",
        roaming_app / "PortableMC",
        user_profile / ".portablemc",
        user_profile / "Documents" / "PortableMC",
        public_profile / "Documents" / "PortableMC",
        program_data / "PortableMC",
        temp_dir / "PortableMC",
    ]


def _probe_directory_access(path):
    result = {
        "exists": False,
        "create_ok": False,
        "write_ok": False,
        "read_ok": False,
        "execute_ok": False,
        "error": "",
    }
    try:
        path.mkdir(parents=True, exist_ok=True)
        result["exists"] = path.exists()
        result["create_ok"] = True
    except Exception as exc:
        result["error"] = f"mkdir failed: {exc}"
        return result

    probe_file = path / ".pmc_probe"
    try:
        with open(probe_file, "w", encoding="utf-8") as f:
            f.write("probe")
        result["write_ok"] = True
    except Exception as exc:
        result["error"] = f"write failed: {exc}"
        return result

    try:
        _ = probe_file.read_text(encoding="utf-8")
        result["read_ok"] = True
    except Exception as exc:
        result["error"] = f"read failed: {exc}"

    try:
        proc = subprocess.run(
            ["cmd", "/c", "cd"], cwd=path, capture_output=True, text=True, timeout=3
        )  # nosec
        result["execute_ok"] = proc.returncode == 0
    except Exception as exc:
        result["error"] = f"execute probe failed: {exc}"
    finally:
        try:
            probe_file.unlink(missing_ok=True)
        except Exception:
            pass

    return result


def _resolve_trusted_dir(config):
    allowlist = config.get("allowlist_trusted_dirs", [])
    candidates = _get_builtin_trusted_candidates()
    for item in allowlist:
        try:
            p = Path(item)
            if p not in candidates:
                candidates.insert(0, p)
        except Exception:
            continue

    probes = {}
    selected = None
    for candidate in candidates:
        key = str(candidate)
        try:
            probe = _probe_directory_access(candidate)
            probes[key] = probe
            if selected is None and all(
                probe.get(k) for k in ("create_ok", "write_ok", "read_ok", "execute_ok")
            ):
                selected = candidate
        except Exception as exc:
            probes[key] = {"error": f"{type(exc).__name__}: {exc}"}

    if selected is None:
        selected = BASE_DIR
    return selected, candidates, probes


def _sync_config_to_trusted(config_path, trusted_dir):
    trusted_cfg = trusted_dir / "launcher_config.json"
    sync_mode = "copy"
    try:
        trusted_cfg.parent.mkdir(parents=True, exist_ok=True)
        if trusted_cfg.exists() or trusted_cfg.is_symlink():
            try:
                if trusted_cfg.is_dir():
                    shutil.rmtree(trusted_cfg, ignore_errors=True)
                else:
                    trusted_cfg.unlink(missing_ok=True)
            except Exception:
                pass
        os.symlink(str(config_path), str(trusted_cfg))
        sync_mode = "symlink"
    except Exception:
        try:
            shutil.copy2(config_path, trusted_cfg)
            sync_mode = "copy"
        except Exception:
            sync_mode = "disabled"
    return trusted_cfg, sync_mode


def _set_runtime_base_dir(new_base_dir):
    global BASE_DIR, EMBEDDED_DIR, EMBEDDED_PYTHON, PORTABLEMC_BIN_DIR
    BASE_DIR = Path(new_base_dir)
    EMBEDDED_DIR = BASE_DIR / "python"
    EMBEDDED_PYTHON = EMBEDDED_DIR / "python.exe"
    PORTABLEMC_BIN_DIR = BASE_DIR / "portablemc_bin"


def update_launcher_state(**updates):
    LAUNCHER_STATE.update(updates)
    _safe_json_dump(CONFIG_PATH, LAUNCHER_STATE)


def initialize_runtime_configuration():
    global LAUNCHER_STATE, ACTIVE_TRUSTED_DIR
    loaded = _load_json_file(CONFIG_PATH)
    merged = {**DEFAULT_CONFIG, **loaded}

    selected_dir, candidate_list, probes = _resolve_trusted_dir(merged)
    ACTIVE_TRUSTED_DIR = selected_dir
    _set_runtime_base_dir(selected_dir)

    merged["trusted_dir"] = str(selected_dir)
    merged["trusted_dir_candidates"] = [str(x) for x in candidate_list]
    merged["trusted_dir_probes"] = probes
    merged["last_base_dir"] = str(BASE_DIR)
    merged["last_known_working_directory"] = str(ROOT_DIR)
    merged["include_sensitive_env_details"] = False
    LAUNCHER_STATE = merged

    _safe_json_dump(CONFIG_PATH, LAUNCHER_STATE)

    trusted_cfg, sync_mode = _sync_config_to_trusted(CONFIG_PATH, ACTIVE_TRUSTED_DIR)
    LAUNCHER_STATE["trusted_config_path"] = str(trusted_cfg)
    LAUNCHER_STATE["config_sync_mode"] = sync_mode
    _safe_json_dump(CONFIG_PATH, LAUNCHER_STATE)


def get_safe_local_cwd(preferred=None):
    """Return local cwd to avoid UNC path execution issues."""
    candidate = Path(preferred) if preferred else BASE_DIR
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    text = str(candidate)
    if text.startswith("\\\\"):
        fallback = BASE_DIR
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback
    return candidate


# ═══════════════════════════════════════════════════════════════════════════════
# § 3  IN-PROCESS BOOTSTRAP HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _ensure_scripts_on_path() -> None:
    """
    Prepend SCRIPTS_DIR to sys.path so that jvm_boot_glue, jvm_memory_loader,
    and the bundled pythonmemorymodule package are all importable in the
    current Python process.

    Safe to call multiple times — idempotent.
    """
    scripts_str = str(SCRIPTS_DIR)
    if scripts_str not in sys.path:
        sys.path.insert(0, scripts_str)


def _check_importable(module_name: str) -> bool:
    """Return True if *module_name* can be imported in the current process."""
    import importlib.util

    try:
        return importlib.util.find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def _bootstrap_inprocess_deps() -> bool:
    """
    Ensure every Python dependency needed by jvm_boot_glue is importable
    in the CURRENT Python interpreter (whichever is running main.py).

    pythonmemorymodule ships as source inside scripts/ and becomes importable
    after _ensure_scripts_on_path().  Only jpype1 requires a pip install.

    Returns True on success, False if any dep cannot be satisfied.
    """
    _ensure_scripts_on_path()

    # pythonmemorymodule — source present in scripts/; verify it loads.
    if not _check_importable("pythonmemorymodule"):
        print(
            "❌ pythonmemorymodule not found under scripts/. "
            "Ensure scripts/pythonmemorymodule/__init__.py exists."
        )
        return False
    print("✅ pythonmemorymodule: found in scripts/")

    # jpype1 — requires a compiled C extension; install via pip if missing.
    if _check_importable("jpype"):
        print("✅ jpype: already importable")
    else:
        print("📦 jpype not found — installing jpype1 via pip...")
        env = os.environ.copy()
        env["PYTHONNOUSERSITE"] = "1"
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "jpype1",
            "--quiet",
            "--no-warn-script-location",
        ]
        try:
            result = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=120
            )  # nosec
            if result.returncode != 0:
                print(f"❌ pip install jpype1 failed:\n{result.stderr.strip()}")
                return False
            print("✅ jpype1 installed")
        except Exception as exc:
            print(f"❌ Could not install jpype1: {exc}")
            return False

        # Re-check after install
        if not _check_importable("jpype"):
            print(
                "❌ jpype still not importable after install. "
                "Check that pip installed into the active interpreter."
            )
            return False

    # Verify glue scripts exist on disk
    for script in (JVM_BOOT_GLUE_PY, JVM_MEMORY_LOADER_PY):
        if not script.exists():
            print(f"❌ Required glue script not found: {script}")
            return False
    print("✅ jvm_boot_glue.py and jvm_memory_loader.py: found")

    return True


def _candidate_portablemc_jvm_roots() -> list[Path]:
    """Search roots that may contain ``jvm/java-runtime-*`` Mojang installs."""
    seen: dict[str, None] = {}
    roots: list[Path] = []

    def _add(candidate: Path | None):
        if candidate is None:
            return
        try:
            rp = candidate.resolve()
        except OSError:
            rp = Path(candidate)
        key = str(rp).casefold()
        if key in seen:
            return
        seen[key] = None
        roots.append(rp)

    _pmc = os.environ.get("PMC_MAIN_DIR", "").strip()
    if _pmc:
        try:
            _add(Path(_pmc))
        except OSError:
            pass
    _add(BASE_DIR)
    _add(LOCAL_BASE_DIR)
    _add(ACTIVE_TRUSTED_DIR)
    explicit = Path(
        r"C:\Users\wave6\AppData\Local\PortableMC"
    ).resolve()
    _add(explicit)
    return roots


def resolve_effective_launcher_jdk_bin(*, memory_resident: bool) -> Path:
    """
    Return ``bin/`` owning the JVM image.

    • Option 4 (OS-loaded): pinned OpenJDK 25 (`JDK_BIN`).
    • Option 5 (memory): portablemc's Mojang epsilon JRE if present, otherwise
      fall back to `JDK_BIN`.

    Canonical files: ``bin/server/jvm.dll`` or ``bin/jvm.dll``.
    """
    openjdk_bin = JDK_BIN.resolve()
    if not memory_resident:
        return openjdk_bin

    eps_bin: Path | None = None
    for root in _candidate_portablemc_jvm_roots():
        candidate = (root / MEMORY_JVM_BIN_REL).resolve()
        if not candidate.is_dir():
            continue
        sj = candidate / "server" / "jvm.dll"
        fj = candidate / "jvm.dll"
        if sj.is_file() or fj.is_file():
            eps_bin = candidate
            break

    return eps_bin if eps_bin else openjdk_bin


def _verify_jdk_at(jdk_bin: Path | None = None) -> bool:
    """
    Confirm ``jdk_bin`` exists and ``jvm.dll`` is reachable (server/ or bin/).
    Prints a diagnostic message and returns False on failure.
    """
    base = JDK_BIN if jdk_bin is None else Path(jdk_bin)
    sj = base / "server" / "jvm.dll"
    fj = base / "jvm.dll"
    resolved = sj if sj.is_file() else (fj if fj.is_file() else None)
    if not base.exists():
        print(f"❌ JDK bin not found: {base}")
        return False
    if resolved is None:
        print(f"❌ jvm.dll not found under {base} (checked server/jvm.dll, jvm.dll)")
        return False
    print(f"✅ JDK  : {base}")
    print(f"✅ jvm  : {resolved}")
    return True


def _verify_jdk() -> bool:
    """Verify the default pinned OpenJDK 25 bin path (shortcut for diagnostics)."""
    return _verify_jdk_at(JDK_BIN)


# ═══════════════════════════════════════════════════════════════════════════════
# § 4  DIAGNOSTICS / PORTABLEMC HELPERS  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def collect_portablemc_diagnostics(python_exe, env=None):
    """Collect portablemc install diagnostics for dumps."""
    diagnostics = {}
    base_env = os.environ.copy()
    if env:
        base_env.update(env)
    commands = {
        "pip_show": [str(python_exe), "-m", "pip", "show", "portablemc"],
        "import_probe": [
            str(python_exe),
            "-c",
            "import portablemc,sys; print(portablemc.__file__); print(sys.executable)",
        ],
        "site_probe": [str(python_exe), "-m", "site"],
        "sys_path": [
            str(python_exe),
            "-c",
            "import sys,site,json; print(json.dumps({'sys_path':sys.path,'usersite':getattr(site,'getusersitepackages',lambda:None)()}, default=str))",
        ],
        "uvx_help": ["uvx", "portablemc", "--help"],
    }
    for name, cmd in commands.items():
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                env=base_env,
                cwd=get_safe_local_cwd(),
            )  # nosec
            diagnostics[name] = {
                "cmd": cmd,
                "returncode": res.returncode,
                "stdout": (res.stdout or "").strip(),
                "stderr": (res.stderr or "").strip(),
            }
        except Exception as exc:
            diagnostics[name] = {"cmd": cmd, "error": f"{type(exc).__name__}: {exc}"}
    return diagnostics


# ═══════════════════════════════════════════════════════════════════════════════
# § 5  OS / ARCH DETECTION  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM = platform.system().lower()
MACHINE = platform.machine().lower()

ARCH_MAP = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "i686": "i686",
    "i386": "i686",
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "armv7l": "arm-gnueabihf",
    "arm": "arm-gnueabihf",
}

OS_MAP = {
    "windows": "windows",
    "linux": "linux",
    "darwin": "macos",
}


def get_portablemc_url():
    """Return the download URL for the native portablemc binary, or None."""
    os_name = OS_MAP.get(SYSTEM)
    if not os_name:
        print(f"⚠️ Unsupported OS: {SYSTEM}")
        return None
    arch = ARCH_MAP.get(MACHINE, "x86_64")
    if os_name == "macos":
        arch = "aarch64" if arch == "aarch64" else "x86_64"
    if os_name == "linux" and arch not in ("arm-gnueabihf",):
        arch += "-gnu"
    base = f"{PORTABLEMC_RELEASE_BASE}/"
    if os_name == "windows":
        ext = "zip"
        filename = f"portablemc-{PORTABLEMC_VERSION}-{os_name}-{arch}-msvc.{ext}"
    else:
        ext = "tar.gz"
        filename = f"portablemc-{PORTABLEMC_VERSION}-{os_name}-{arch}.{ext}"
    return base + filename


# ═══════════════════════════════════════════════════════════════════════════════
# § 6  DATA / JUNCTION HELPERS  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def prepare_user_data():
    """Move static folder and game files to BASE_DIR if not already present."""
    base_dir = BASE_DIR
    root_dir = ROOT_DIR

    src_static = root_dir / "static"
    dst_static = base_dir / "static"
    if src_static.exists() and src_static.is_dir() and not dst_static.exists():
        try:
            shutil.copytree(src_static, dst_static)
            print("✅ Static folder moved to %LOCALAPPDATA%\\PortableMC\\static")
        except Exception as e:
            print(f"⚠️ Failed to copy static folder: {e}")
    elif dst_static.exists():
        print("ℹ️ Static folder already exists in %LOCALAPPDATA%\\PortableMC")

    for filename in ["servers.dat", "options.txt"]:
        src = root_dir / filename
        dst = base_dir / filename
        if src.exists() and src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
                print(f"✅ {filename} moved to %LOCALAPPDATA%\\PortableMC")
            except Exception as e:
                print(f"⚠️ Failed to copy {filename}: {e}")
        elif dst.exists():
            print(f"ℹ️ {filename} already exists in %LOCALAPPDATA%\\PortableMC")


def is_junction(path):
    """Return True if path is a junction (reparse point)."""
    try:
        attrs = os.lstat(str(path))
        return (attrs.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) != 0
    except OSError:
        return False


def create_junction(source, target):
    """Create a junction from source to target."""
    source_path = Path(source).resolve()
    target_path = Path(target).resolve()

    if source_path == target_path:
        print(
            f"⚠️ Source and target are the same ({source_path}); skipping junction creation."
        )
        try:
            target_path.mkdir(parents=True, exist_ok=True)
        except OSError as mkdir_exc:
            print(f"Could not create fallback directory {target_path}: {mkdir_exc}")
        return False

    source_path.mkdir(parents=True, exist_ok=True)

    if target_path.exists():
        if is_junction(target_path):
            os.rmdir(str(target_path))
            print(f"Removed existing junction: {target_path}")
        elif target_path.is_dir():
            shutil.copytree(str(target_path), str(source_path), dirs_exist_ok=True)
            shutil.rmtree(str(target_path), ignore_errors=True)
            print(f"Merged and removed existing directory: {target_path}")
        else:
            if not source_path.exists():
                shutil.copy2(str(target_path), str(source_path))
            target_path.unlink()

    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target_path), str(source_path)],
            check=True,
            capture_output=True,
            text=True,
        )  # nosec
        print(f"✅ Junction created: {target_path} -> {source_path}")
        links = LAUNCHER_STATE.get("managed_links", {})
        links[str(target_path)] = str(source_path)
        update_launcher_state(managed_links=links)
        return True
    except subprocess.CalledProcessError as e:
        print(
            f"⚠️ Could not create junction (falling back to regular directory): {e.stderr}"
        )
        target_path.mkdir(parents=True, exist_ok=True)
        return False


def ensure_junctions():
    r"""Ensure project-managed folders link into the active runtime base directory."""
    runtime_dirs = []
    for candidate in (LOCAL_BASE_DIR, BASE_DIR, ACTIVE_TRUSTED_DIR):
        if not candidate:
            continue
        candidate = Path(candidate)
        if not any(
            existing.resolve() == candidate.resolve() for existing in runtime_dirs
        ):
            runtime_dirs.append(candidate)

    for base_dir in runtime_dirs:
        base_dir.mkdir(parents=True, exist_ok=True)
        create_junction(ROOT_DIR / "mods", base_dir / "mods")
        create_junction(ROOT_DIR / "resourcepacks", base_dir / "resourcepacks")
        create_junction(ROOT_DIR / "config", base_dir / "config")


# ═══════════════════════════════════════════════════════════════════════════════
# § 7  DEBUG / HARNESS  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def _run_harness_probe(name, cmd, cwd=None, timeout=8):
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )  # nosec
        return {
            "name": name,
            "command": cmd,
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
        }
    except Exception as exc:
        return {
            "name": name,
            "command": cmd,
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_restricted_env_test_harness():
    """Run non-invasive restricted-environment compatibility probes."""
    print("\n=== Restricted Environment Test Harness ===\n")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    harness_log = PROJECT_LOGS_DIR / f"restricted_env_harness_{stamp}.log"
    probes = []

    probes.append(_run_harness_probe("whoami", ["whoami"]))
    probes.append(_run_harness_probe("where_msbuild", ["where", "MSBuild.exe"]))
    probes.append(_run_harness_probe("where_powershell", ["where", "powershell.exe"]))
    probes.append(_run_harness_probe("where_cscript", ["where", "cscript.exe"]))
    probes.append(_run_harness_probe("where_csc", ["where", "csc.exe"]))
    probes.append(_run_harness_probe("where_py", ["where", "py"]))
    probes.append(_run_harness_probe("where_python", ["where", "python"]))
    probes.append(
        _run_harness_probe("msbuild_version", ["cmd", "/c", "MSBuild.exe -version"])
    )
    probes.append(
        _run_harness_probe(
            "powershell_version",
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$PSVersionTable.PSVersion.ToString()",
            ],
        )
    )
    probes.append(_run_harness_probe("cscript_help", ["cscript", "//?"]))
    probes.append(_run_harness_probe("csc_help", ["csc", "/help"]))
    probes.append(_run_harness_probe("systeminfo", ["systeminfo"]))

    trusted_dir = ACTIVE_TRUSTED_DIR or BASE_DIR
    probes.append(
        _run_harness_probe("trusted_dir_probe", ["cmd", "/c", "cd"], cwd=trusted_dir)
    )

    lines = []
    lines.append("=" * 80)
    lines.append("RESTRICTED ENVIRONMENT TEST HARNESS")
    lines.append("=" * 80)
    lines.append(f"timestamp: {datetime.datetime.now().isoformat()}")
    lines.append(f"root_dir: {ROOT_DIR}")
    lines.append(f"base_dir: {BASE_DIR}")
    lines.append(f"active_trusted_dir: {ACTIVE_TRUSTED_DIR}")
    lines.append(f"config_path: {CONFIG_PATH}")
    lines.append("")
    lines.append("[effective_env]")
    lines.append(
        f"PORTABLEMC_VERSION={os.environ.get('PORTABLEMC_VERSION', PORTABLEMC_VERSION)}"
    )
    lines.append(f"ALLOW_INSECURE_SSL={os.environ.get('ALLOW_INSECURE_SSL', '')}")
    lines.append(f"LAUNCHER_VERBOSE={os.environ.get('LAUNCHER_VERBOSE', '')}")
    lines.append("")
    lines.append("[trusted_dir_probes]")
    lines.append(
        json.dumps(
            LAUNCHER_STATE.get("trusted_dir_probes", {}), indent=2, sort_keys=True
        )
    )
    lines.append("")
    lines.append("[command_probes]")
    for probe in probes:
        lines.append("-" * 60)
        lines.append(json.dumps(probe, indent=2))
    lines.append("-" * 60)

    try:
        PROJECT_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(harness_log, "w", encoding="utf-8", errors="replace") as f:
            f.write("\n".join(lines))
        if ACTIVE_TRUSTED_DIR:
            try:
                mirror_logs = ACTIVE_TRUSTED_DIR / "logs"
                mirror_logs.mkdir(parents=True, exist_ok=True)
                shutil.copy2(harness_log, mirror_logs / harness_log.name)
            except Exception:
                pass
        print(f"✅ Harness complete. Log: {harness_log}")
        update_launcher_state(last_harness_log=str(harness_log))
        return True
    except Exception as exc:
        print(f"❌ Harness failed to write log: {exc}")
        return False


def remove_managed_link(target):
    target_path = Path(target)
    try:
        if not target_path.exists() and not target_path.is_symlink():
            return True
        if target_path.is_symlink() or is_junction(target_path):
            os.rmdir(str(target_path)) if target_path.is_dir() else target_path.unlink(
                missing_ok=True
            )
        elif target_path.is_dir():
            shutil.rmtree(target_path, ignore_errors=True)
        else:
            target_path.unlink(missing_ok=True)
        return True
    except Exception as exc:
        print(f"⚠️ Failed to remove {target_path}: {exc}")
        return False


def run_debug_menu():
    while True:
        print("\n" + "=" * 60)
        print("      Debug Launcher – Choose a Option")
        print("=" * 60)
        print("1) Remove mods link")
        print("2) Remove resourcepacks link")
        print("3) Remove config link")
        print("4) Remove all managed links")
        print("5) Clear trusted mirror cache/logs")
        print("6) Print effective config")
        print("7) Run trusted-dir probes only")
        print("8) Advanced restricted-env test harness")
        print("9) Toggle sensitive env fields in dumps")
        print("10) In-process dependency check")
        print("b) Back")
        choice = input("Select debug option: ").strip().lower()

        if choice == "1":
            remove_managed_link(BASE_DIR / "mods")
        elif choice == "2":
            remove_managed_link(BASE_DIR / "resourcepacks")
        elif choice == "3":
            remove_managed_link(BASE_DIR / "config")
        elif choice == "4":
            if (
                input("Confirm remove all managed links? (yes/no): ").strip().lower()
                == "yes"
            ):
                links = list(LAUNCHER_STATE.get("managed_links", {}).keys())
                for link in links:
                    remove_managed_link(link)
                update_launcher_state(managed_links={})
                print("Managed links removed.")
        elif choice == "__disabled_old_3":
            if (
                input("Confirm remove all managed links? (yes/no): ").strip().lower()
                == "yes"
            ):
                links = list(LAUNCHER_STATE.get("managed_links", {}).keys())
                for link in links:
                    remove_managed_link(link)
                update_launcher_state(managed_links={})
                print("✅ Managed links removed.")
        elif choice == "5":
            if ACTIVE_TRUSTED_DIR:
                for name in ("logs", "portablemc_bin", "python"):
                    p = ACTIVE_TRUSTED_DIR / name
                    if p.exists():
                        try:
                            shutil.rmtree(p, ignore_errors=True)
                        except Exception as exc:
                            print(f"⚠️ Could not remove {p}: {exc}")
                print("✅ Trusted cache/logs cleanup attempted.")
            else:
                print("ℹ️ No trusted directory selected.")
        elif choice == "6":
            print(json.dumps(LAUNCHER_STATE, indent=2, sort_keys=True))
        elif choice == "7":
            selected, candidates, probes = _resolve_trusted_dir(LAUNCHER_STATE)
            print(f"Selected trusted dir: {selected}")
            print(json.dumps({str(k): v for k, v in probes.items()}, indent=2))
        elif choice == "8":
            run_restricted_env_test_harness()
        elif choice == "9":
            current = bool(LAUNCHER_STATE.get("include_sensitive_env_details", False))
            update_launcher_state(include_sensitive_env_details=not current)
            print(f"Sensitive env fields in dumps: {not current}")
        elif choice == "10":
            # In-process dependency diagnostic
            print("\n--- In-Process Dependency Check ---")
            _ensure_scripts_on_path()
            for mod in ["pythonmemorymodule", "jpype", "jpype.imports"]:
                ok = _check_importable(mod)
                print(f"  {'✅' if ok else '❌'} {mod}")
            mem_bin = resolve_effective_launcher_jdk_bin(memory_resident=True)
            mem_jvm = mem_bin / "server" / "jvm.dll"
            if not mem_jvm.is_file():
                mem_jvm = mem_bin / "jvm.dll"
            os_jvm = JDK_BIN / "server" / "jvm.dll"
            if not os_jvm.is_file():
                os_jvm = JDK_BIN / "jvm.dll"
            print(f"  {'✅' if os_jvm.exists() else '❌'} option 4 JVM @ {os_jvm}")
            print(
                f"  {'✅' if mem_jvm.exists() else '❌'} option 5 JVM @ {mem_jvm} (bin {mem_bin})"
            )
            print(f"  {'✅' if JVM_BOOT_GLUE_PY.exists() else '❌'} jvm_boot_glue.py")
            print(
                f"  {'✅' if JVM_MEMORY_LOADER_PY.exists() else '❌'} jvm_memory_loader.py"
            )
        elif choice in ("b", "q"):
            return
        else:
            print("Invalid debug choice.")


# ═══════════════════════════════════════════════════════════════════════════════
# § 8  DOWNLOAD / SSL HELPERS  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def get_ssl_context():
    """Return an unverified SSL context if ALLOW_INSECURE_SSL is True, else None."""
    if ALLOW_INSECURE_SSL:
        print(
            "⚠️ WARNING: SSL certificate verification is disabled (ALLOW_INSECURE_SSL=true)."
        )
        return ssl._create_unverified_context()
    return None


def download_file(url, dest_path):
    """Download a file with optional insecure fallback."""
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(url, context=ctx) as response:
            with open(dest_path, "wb") as f:
                f.write(response.read())
        return True
    except Exception as e:
        print(f"⚠️ First download attempt failed: {e}")
        if ALLOW_INSECURE_SSL:
            print("📥 Retrying with SSL verification disabled...")
            try:
                context = get_ssl_context()
                with urllib.request.urlopen(url, context=context) as response:
                    with open(dest_path, "wb") as f:
                        f.write(response.read())
                return True
            except Exception as e2:
                print(f"❌ Download failed even with unverified SSL: {e2}")
                return False
        else:
            print("❌ Download failed and insecure SSL is disabled.")
            print(
                "⚠️ If you are in a restricted network, set ALLOW_INSECURE_SSL=true and try again."
            )
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# § 9  MSBUILD CANDIDATE DETECTION  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def _add_msbuild_candidate(candidates, path, priority):
    path = os.fspath(path)
    if os.path.isfile(path):
        candidates.append((path, priority))


def _add_msbuild_candidates_from_vs_install(candidates, install_path, priority):
    if not install_path:
        return
    root = Path(install_path)
    for relative in (
        Path("MSBuild") / "Current" / "Bin" / "MSBuild.exe",
        Path("MSBuild") / "15.0" / "Bin" / "MSBuild.exe",
    ):
        _add_msbuild_candidate(candidates, root / relative, priority)


def _add_msbuild_candidates_from_python_vswhere(candidates):
    try:
        import vswhere  # type: ignore[import]
    except ImportError:
        return

    query_sets = (
        {
            "products": "*",
            "requires": "Microsoft.Component.MSBuild",
            "prop": "installationPath",
        },
        {"products": "*", "prop": "installationPath"},
    )
    for kwargs in query_sets:
        try:
            for entry in vswhere.find(legacy=True, **kwargs):
                install_path = (
                    entry.get("installationPath") if isinstance(entry, dict) else entry
                )
                _add_msbuild_candidates_from_vs_install(candidates, install_path, 110)
        except Exception:
            continue

    try:
        _add_msbuild_candidates_from_vs_install(
            candidates, vswhere.get_latest_path(products="*"), 109
        )
    except Exception:
        pass


def find_msbuild_candidates():
    """Return a list of candidate MSBuild.exe paths sorted by priority (highest first)."""
    candidates = []

    _add_msbuild_candidates_from_python_vswhere(candidates)

    hardcoded = [
        (
            r"C:\Program Files\Microsoft Visual Studio\2026\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
            99,
        ),
        (
            r"C:\Program Files\Microsoft Visual Studio\2026\Professional\MSBuild\Current\Bin\MSBuild.exe",
            99,
        ),
        (
            r"C:\Program Files\Microsoft Visual Studio\2026\Community\MSBuild\Current\Bin\MSBuild.exe",
            99,
        ),
        (
            r"C:\Program Files\Microsoft Visual Studio\2026\BuildTools\MSBuild\Current\Bin\MSBuild.exe",
            99,
        ),
        (
            r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
            90,
        ),
        (
            r"C:\Program Files\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe",
            90,
        ),
        (
            r"C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
            90,
        ),
        (
            r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe",
            90,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
            89,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe",
            89,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
            89,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe",
            89,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
            80,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Professional\MSBuild\Current\Bin\MSBuild.exe",
            80,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Community\MSBuild\Current\Bin\MSBuild.exe",
            80,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\MSBuild.exe",
            80,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2017\Enterprise\MSBuild\15.0\Bin\MSBuild.exe",
            70,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2017\Professional\MSBuild\15.0\Bin\MSBuild.exe",
            70,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2017\Community\MSBuild\15.0\Bin\MSBuild.exe",
            70,
        ),
        (
            r"C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\MSBuild\15.0\Bin\MSBuild.exe",
            70,
        ),
        (r"C:\Program Files (x86)\MSBuild\14.0\Bin\MSBuild.exe", 60),
        (r"C:\Program Files (x86)\MSBuild\12.0\Bin\MSBuild.exe", 50),
        (r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\MSBuild.exe", 40),
        (r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\MSBuild.exe", 30),
    ]

    for path, prio in hardcoded:
        _add_msbuild_candidate(candidates, path, prio)

    unique = {}
    for path, prio in candidates:
        if path not in unique or prio > unique[path]:
            unique[path] = prio

    sorted_candidates = sorted(unique.items(), key=lambda x: x[1], reverse=True)
    return [path for path, _ in sorted_candidates]


# ═══════════════════════════════════════════════════════════════════════════════
# § 10  EMBEDDED PYTHON SETUP  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def ensure_embedded_python():
    """Download and extract embedded Python to BASE_DIR if missing."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if EMBEDDED_PYTHON.exists():
        print("✅ Embedded Python already exists.")
        return True

    print("📥 Downloading embedded Python...")
    zip_path = BASE_DIR / "python-embed.zip"
    if not download_file(PYTHON_URL, zip_path):
        return False

    print("📦 Extracting...")
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(EMBEDDED_DIR)
    zip_path.unlink()
    print("✅ Embedded Python ready.")
    return True


def fix_pth_file():
    """Enable site-packages in embedded Python's ._pth file."""
    pth_files = list(EMBEDDED_DIR.glob("*._pth"))
    if not pth_files:
        print("❌ No ._pth file found; cannot enable site-packages.")
        return False
    pth = pth_files[0]
    with open(pth, "r") as f:
        content = f.read()
    if "#import site" in content:
        content = content.replace("#import site", "import site")
        with open(pth, "w") as f:
            f.write(content)
        print("✅ Enabled site-packages in ._pth file.")
    else:
        print("ℹ️ site-packages already enabled.")
    return True


def test_embedded_python():
    """Test if the embedded Python executable can be run."""
    try:
        result = subprocess.run(
            [str(EMBEDDED_PYTHON), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )  # nosec
        if result.returncode == 0:
            print(f"✅ Embedded Python runs: {result.stdout.strip()}")
            return True
        else:
            print(f"❌ Embedded Python test failed: {result.stderr}")
            return False
    except OSError as e:
        print(f"❌ Cannot run embedded Python (blocked by group policy?): {e}")
        return False
    except Exception as e:
        print(f"❌ Embedded Python test error: {e}")
        return False


def setup_embedded_python():
    """Ensure embedded Python is downloaded, pth fixed, pip installed."""
    if not ensure_embedded_python():
        return False
    if not fix_pth_file():
        print("⚠️ Failed to fix .pth file for embedded Python.")
        return False
    if not test_embedded_python():
        return False

    env_check = os.environ.copy()
    env_check["PYTHONNOUSERSITE"] = "1"
    env_check["PYTHONPATH"] = ""
    pip_check = subprocess.run(
        [str(EMBEDDED_PYTHON), "-m", "pip", "--version"],
        env=env_check,
        capture_output=True,
        text=True,
    )  # nosec
    if pip_check.returncode != 0:
        print("📦 pip not found, installing...")
        if not install_pip(EMBEDDED_PYTHON):
            return False
    else:
        print(f"✅ pip already installed: {pip_check.stdout.strip()}")
    return True


def install_portablemc_via_embedded():
    """Install portablemc in embedded Python and return method."""
    print("📦 Installing portablemc (uv first, pip fallback)...")
    if not install_packages_with_fallback(
        ["portablemc"], python_exe=EMBEDDED_PYTHON, isolated=True
    ):
        print("❌ Failed to install portablemc.")
        return None
    return test_portablemc(EMBEDDED_PYTHON)


def download_get_pip():
    """Download get-pip.py into the embedded Python directory."""
    pip_script = EMBEDDED_DIR / "get-pip.py"
    if pip_script.exists():
        print("✅ get-pip.py already present.")
        return pip_script

    print("📥 Downloading get-pip.py (ignoring SSL cert for this request)...")
    try:
        context = get_ssl_context()
        with urllib.request.urlopen(GET_PIP_URL, context=context) as response:
            with open(pip_script, "wb") as out_file:
                out_file.write(response.read())
        print("✅ get-pip.py downloaded successfully.")
    except Exception as e:
        print(f"❌ Failed to download get-pip.py: {e}")
        return None
    return pip_script


def run_pip_command(args, isolated=True, python_exe=None):
    """Run a pip command with the given Python executable."""
    if python_exe is None:
        python_exe = EMBEDDED_PYTHON
    env = os.environ.copy()
    if isolated:
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONPATH"] = ""
    cmd = [str(python_exe)] + args
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)  # nosec
    if result.returncode != 0:
        print(f"❌ Pip command failed: {' '.join(args)}")
        print(result.stderr)
        return False
    print(result.stdout)
    return True


def run_uv_install(packages, python_exe=None, user=False):
    """Try package install using uv. Returns True on success."""
    if python_exe is None:
        python_exe = EMBEDDED_PYTHON
    cmd = ["uv", "pip", "install", "--python", str(python_exe)]
    if user:
        cmd.append("--user")
    cmd.extend(packages)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)  # nosec
    except Exception as exc:
        print(f"⚠️ uv unavailable or blocked: {exc}")
        return False
    if result.returncode != 0:
        print(f"⚠️ uv install failed ({result.returncode}); falling back to pip.")
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
        return False
    if result.stdout:
        print(result.stdout.strip())
    return True


def install_packages_with_fallback(
    packages, python_exe=None, isolated=True, user=False
):
    """Install packages with uv first, then pip fallback."""
    if python_exe is None:
        python_exe = EMBEDDED_PYTHON
    if run_uv_install(packages, python_exe=python_exe, user=user):
        update_launcher_state(installer_backend="uv")
        return True

    pip_args = ["-m", "pip", "install"]
    if user:
        pip_args.append("--user")
    pip_args.extend(packages)
    ok = run_pip_command(pip_args, isolated=isolated, python_exe=python_exe)
    if ok:
        update_launcher_state(installer_backend="pip")
    return ok


def run_portablemc_with_uvx_fallback(
    portablemc_args, env=None, cwd=None, python_exe=None
):
    """Run portablemc with uvx first, python module fallback."""
    uvx_cmd = ["uvx", "portablemc"] + portablemc_args
    print(f"🚀 Launching (uvx): {' '.join(uvx_cmd)}")
    try:
        uvx_code, uvx_output = run_command_live(uvx_cmd, env=env, cwd=cwd)
        if uvx_code == 0:
            return uvx_code, uvx_output, uvx_cmd
        print(f"⚠️ uvx failed with code {uvx_code}, falling back to python module.")
    except Exception as exc:
        print(f"⚠️ uvx blocked/unavailable: {exc}. Falling back to python module.")

    if python_exe is None:
        python_exe = EMBEDDED_PYTHON
    py_cmd = [str(python_exe), "-m", "portablemc"] + portablemc_args
    print(f"🚀 Launching (python fallback): {' '.join(py_cmd)}")
    py_code, py_output = run_command_live(py_cmd, env=env, cwd=cwd)
    return py_code, py_output, py_cmd


def install_pip(python_exe=None):
    """Install pip into the given Python environment."""
    if python_exe is None:
        python_exe = EMBEDDED_PYTHON
    pip_script = download_get_pip()
    if not pip_script:
        return False

    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = ""
    cmd = [
        str(python_exe),
        str(pip_script),
        "--trusted-host=files.pythonhosted.org",
        "--trusted-host=pypi.org",
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)  # nosec
    if result.returncode != 0:
        print("❌ Failed to install pip.")
        print(result.stderr)
        return False
    print("✅ pip installed.")
    return True


def install_base_packages(python_exe=None):
    """Install the base packages (flask, etc.) into the given Python."""
    print("📦 Installing base packages...")
    if python_exe is None:
        python_exe = EMBEDDED_PYTHON
    if not install_packages_with_fallback(
        ["--upgrade", "pip"], isolated=True, python_exe=python_exe
    ):
        print("⚠️ Pip upgrade failed, continuing anyway.")
    for pkg in BASE_PACKAGES:
        print(f"   Installing {pkg}...")
        if not install_packages_with_fallback(
            [pkg], isolated=True, python_exe=python_exe
        ):
            print(f"❌ Failed to install {pkg}.")
            return False
    return True


def get_certifi_path(python_exe=None):
    """Return the path to certifi's CA bundle, or None if certifi not installed."""
    if python_exe is None:
        python_exe = EMBEDDED_PYTHON
    try:
        result = subprocess.run(
            [str(python_exe), "-c", "import certifi; print(certifi.where())"],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONNOUSERSITE": "1"},
        )  # nosec
        path = result.stdout.strip()
        if path and Path(path).exists():
            return path
    except Exception:
        pass
    return None


def download_portablemc_binary():
    """Download and extract the native portablemc binary into BASE_DIR."""
    url = get_portablemc_url()
    if not url:
        return False
    print(f"📥 Downloading portablemc from {url}...")
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = BASE_DIR / "portablemc_download"
    if not download_file(url, archive_path):
        return False

    PORTABLEMC_BIN_DIR.mkdir(exist_ok=True)
    try:
        if url.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as zip_ref:
                zip_ref.extractall(PORTABLEMC_BIN_DIR)
        else:
            with tarfile.open(archive_path, "r:gz") as tar_ref:
                tar_ref.extractall(PORTABLEMC_BIN_DIR)
    except Exception as e:
        print(f"❌ Failed to extract archive: {e}")
        return False
    finally:
        archive_path.unlink(missing_ok=True)
    for item in PORTABLEMC_BIN_DIR.iterdir():
        if item.is_dir():
            for sub in item.iterdir():
                sub.rename(PORTABLEMC_BIN_DIR / sub.name)
            item.rmdir()
    print(f"✅ portablemc binary extracted to {PORTABLEMC_BIN_DIR}")
    return True


def test_portablemc(python_exe=None):
    """Check if portablemc is available (binary, uvx, or module)."""
    exe_name = "portablemc.exe" if SYSTEM == "windows" else "portablemc"
    binary_path = PORTABLEMC_BIN_DIR / exe_name
    if binary_path.exists():
        if SYSTEM != "windows":
            binary_path.chmod(binary_path.stat().st_mode | 0o111)
        env = os.environ.copy()
        env["PATH"] = str(PORTABLEMC_BIN_DIR) + os.pathsep + env.get("PATH", "")
        try:
            result = subprocess.run(
                [str(binary_path), "--help"],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )  # nosec
            if result.returncode == 0:
                print("✅ portablemc binary works.")
                return "binary"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            print("⏱️ portablemc binary check timed out or not found, trying module.")

    try:
        result = subprocess.run(
            ["uvx", "portablemc", "--help"], capture_output=True, text=True, timeout=8
        )  # nosec
        if result.returncode == 0:
            print("✅ portablemc via uvx works.")
            return "uvx"
    except Exception:
        pass

    if python_exe is None:
        python_exe = EMBEDDED_PYTHON
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = ""
    cmd = [str(python_exe), "-m", "portablemc", "--help"]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=5)  # nosec
        if result.returncode == 0:
            print("✅ portablemc module works.")
            return "module"
    except subprocess.TimeoutExpired:
        print("⏱️ portablemc module check timed out, assuming not available.")
    return None


def ensure_portablemc(python_exe=None):
    """Make portablemc available – try binary, fallback to pip. Returns method string or None."""
    method = test_portablemc(python_exe)
    if method:
        return method
    if download_portablemc_binary():
        method = test_portablemc(python_exe)
        if method:
            return method
        print("⚠️ Binary download failed, falling back to pip.")
    print("📦 Installing portablemc (uv first, pip fallback)...")
    if install_packages_with_fallback(
        ["portablemc"], isolated=True, python_exe=python_exe
    ):
        method = test_portablemc(python_exe)
        if method:
            return method
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# § 11  DIAGNOSTICS / FAILURE DUMPS  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def collect_system_details():
    """Collect diagnostic details. Individual probes can fail safely."""
    include_sensitive = bool(LAUNCHER_STATE.get("include_sensitive_env_details", False))
    sensitive_keys = {"USERNAME", "USERDOMAIN", "COMPUTERNAME"}
    details = {
        "timestamp": datetime.datetime.now().isoformat(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
        "root_dir": str(ROOT_DIR),
        "base_dir": str(BASE_DIR),
        "trusted_dir": str(ACTIVE_TRUSTED_DIR) if ACTIVE_TRUSTED_DIR else "",
        "project_logs_dir": str(PROJECT_LOGS_DIR),
        "config_path": str(CONFIG_PATH),
        "env_subset": {
            k: (
                os.environ.get(k, "")
                if include_sensitive or k not in sensitive_keys
                else "<redacted>"
            )
            for k in [
                "USERNAME",
                "USERDOMAIN",
                "COMPUTERNAME",
                "PROCESSOR_ARCHITECTURE",
                "LOCALAPPDATA",
                "APPDATA",
                "TEMP",
                "TMP",
                "COMSPEC",
                "PSModulePath",
            ]
        },
    }

    probes = {
        "whoami": ["whoami"],
        "ver": ["cmd", "/c", "ver"],
        "where_python": ["where", "python"],
        "where_py": ["where", "py"],
        "where_msbuild": ["where", "MSBuild.exe"],
        "where_powershell": ["where", "powershell.exe"],
    }
    details["probes"] = {}
    for name, cmd in probes.items():
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)  # nosec
            details["probes"][name] = {
                "returncode": res.returncode,
                "stdout": (res.stdout or "").strip(),
                "stderr": (res.stderr or "").strip(),
            }
        except Exception as exc:
            details["probes"][name] = {"error": f"{type(exc).__name__}: {exc}"}
    return details


def write_failure_dump(
    kind, message, command=None, output=None, returncode=None, exc=None, extra=None
):
    """Write a resilient failure dump log and return its path (or None)."""
    try:
        logs_dir = PROJECT_LOGS_DIR
        logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dump_path = logs_dir / f"{kind}_failure_{stamp}.log"
        lines = []
        lines.append("=" * 80)
        lines.append(f"{kind.upper()} FAILURE DUMP")
        lines.append("=" * 80)
        lines.append(f"Message: {message}")
        if command:
            lines.append(f"Command: {' '.join(command)}")
        if returncode is not None:
            lines.append(f"Return code: {returncode}")
        lines.append("")

        if extra:
            lines.append("[Extra]")
            for key, value in extra.items():
                lines.append(f"{key}: {value}")
            lines.append("")

        lines.append("[System Details]")
        details = collect_system_details()
        for key, value in details.items():
            lines.append(f"{key}: {value}")
        lines.append("")

        if output:
            lines.append("[Process Output]")
            lines.append(output)
            lines.append("")

        lines.append("[Stack Trace]")
        if exc is not None:
            lines.append(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            )
        else:
            lines.append("(no Python exception captured)")

        with open(dump_path, "w", encoding="utf-8", errors="replace") as f:
            f.write("\n".join(lines))
        if ACTIVE_TRUSTED_DIR:
            try:
                trusted_logs = ACTIVE_TRUSTED_DIR / "logs"
                trusted_logs.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dump_path, trusted_logs / dump_path.name)
            except Exception:
                pass
        update_launcher_state(success=False, last_error=message)
        return dump_path
    except Exception as dump_exc:
        print(f"⚠️ Failed to write diagnostic dump: {dump_exc}")
        return None


def run_command_live(cmd, env=None, cwd=None, timeout=1800):
    """Run a command, stream output live, and capture it for diagnostics."""
    import threading

    output_lines = []
    process = subprocess.Popen(  # nosec
        cmd,
        env=env,
        cwd=get_safe_local_cwd(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def _drain():
        try:
            for line in process.stdout:
                print(line, end="")
                output_lines.append(line)
        except Exception:
            pass

    drain_thread = threading.Thread(target=_drain, daemon=True)
    drain_thread.start()
    try:
        drain_thread.join(timeout=timeout)
        if drain_thread.is_alive():
            process.kill()
            drain_thread.join()
            raise TimeoutError(
                f"Command timed out after {timeout}s: {' '.join(str(c) for c in cmd)}"
            )
        returncode = process.wait()
        return returncode, "".join(output_lines)
    except KeyboardInterrupt:
        try:
            process.terminate()
        except Exception:
            pass
        raise


# ═══════════════════════════════════════════════════════════════════════════════
# § 12  SYSTEM PYTHON DETECTION  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def get_system_python():
    """Find a system Python 3.x executable, preferring 3.11 or higher."""
    candidates = []
    seen = set()

    def add_candidate(p):
        p = Path(p).resolve()
        if p.exists() and p not in seen and not str(p).startswith(str(BASE_DIR)):
            seen.add(p)
            candidates.append(p)

    current = Path(sys.executable)
    if current != EMBEDDED_PYTHON and not str(current).startswith(str(BASE_DIR)):
        add_candidate(current)

    wl = shutil.which("py.exe") or shutil.which("py")
    if wl:
        add_candidate(wl)

    for dir in os.environ.get("PATH", "").split(os.pathsep):
        add_candidate(Path(dir) / "py.exe")
        add_candidate(Path(dir) / "python.exe")
        add_candidate(Path(dir) / "python3.exe")

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"Software\Python\PythonCore")
        except FileNotFoundError:
            continue
        i = 0
        while True:
            try:
                ver = winreg.EnumKey(key, i)
                if ver.startswith("3."):
                    install_path = winreg.QueryValue(key, f"{ver}\\InstallPath")
                    add_candidate(Path(install_path) / "python.exe")
                i += 1
            except OSError:
                break
        winreg.CloseKey(key)

    for ver in PYTHON_VERSIONS:
        num = ver.replace(".", "")
        for base in (
            r"C:\Python{}",
            r"C:\Program Files\Python{}",
            r"C:\Program Files (x86)\Python{}",
        ):
            add_candidate(base.format(num) + "\\python.exe")
        user_dir = (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
            / "Programs"
            / f"Python{num}"
        )
        add_candidate(user_dir / "python.exe")
    add_candidate(r"C:\Windows\Sysnative\python.exe")
    add_candidate(r"C:\Windows\System32\python.exe")

    valid = []
    for p in candidates:
        try:
            result = subprocess.run(
                [str(p), "--version"], capture_output=True, text=True, timeout=2
            )  # nosec
            combined = (result.stdout + result.stderr).strip()
            if result.returncode == 0 and "Python 3" in combined:
                match = re.search(r"\d+(?:\.\d+)+", combined)
                if match:
                    version_str = match.group()
                    valid.append((version_str, p))
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
            continue

    if not valid:
        return None

    valid.sort(key=lambda x: tuple(map(int, x[0].split("."))), reverse=True)
    best = valid[0][1]
    print(f"Selected system Python: {best}")
    return best


# ═══════════════════════════════════════════════════════════════════════════════
# § 13  LAUNCHER DISPATCH — launch_launcher()
#        Updated to propagate in-process env vars to pmc_web.py
# ═══════════════════════════════════════════════════════════════════════════════


def launch_launcher(method, python_exe=None, extra_env=None):
    launcher_script = PORTABLEMC_PY
    if not launcher_script.exists():
        print("❌ pmc_web.py not found in scripts/.")
        return False

    if python_exe is None:
        python_exe = EMBEDDED_PYTHON

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    paths = [str(EMBEDDED_DIR), str(EMBEDDED_DIR / "Scripts"), str(PORTABLEMC_BIN_DIR)]
    env["PATH"] = os.pathsep.join(paths) + os.pathsep + env.get("PATH", "")
    env["__COMPAT_LAYER"] = "RUNASINVOKER"
    env["LAUNCHER_ROOT"] = str(ROOT_DIR)

    if python_exe == EMBEDDED_PYTHON:
        env["PYTHONHOME"] = str(EMBEDDED_DIR)
        env["PYTHONNOUSERSITE"] = "1"

    env["CLICOLOR_FORCE"] = "1"
    env["PYTHONPATH"] = ""
    env["PORTABLEMC_METHOD"] = method

    # ── In-process env vars forwarded to pmc_web.py ────────────────────────
    # pmc_web.py can inspect LAUNCHER_INPROCESS to decide whether to use
    # the in-process launch path (jvm_boot_glue) instead of a portablemc CLI
    # subprocess.  JDK_BIN_PATH and PMC_SCRIPTS_DIR let it locate the glue.
    env["JDK_BIN_PATH"] = str(JDK_BIN)
    env["PMC_SCRIPTS_DIR"] = str(SCRIPTS_DIR)
    env["PMC_MAIN_DIR"] = str(BASE_DIR)
    # LAUNCHER_INPROCESS is deliberately NOT set here — web/cli modes keep
    # the existing subprocess model.  run_inprocess_launcher() sets it directly.

    cert_path = get_certifi_path(python_exe)
    if cert_path:
        env["SSL_CERT_FILE"] = cert_path
        env["REQUESTS_CA_BUNDLE"] = cert_path

    cmd = [str(python_exe), str(launcher_script)]
    print(f"🚀 Launching: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, env=env, check=True, cwd=get_safe_local_cwd(BASE_DIR))  # nosec
    except subprocess.CalledProcessError as e:
        print(f"❌ Launcher exited with error: {e}")
        return False
    except KeyboardInterrupt:
        print("⏹️ Interrupted by user.")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# § 14  WEB LAUNCHER  (unchanged logic, extra env vars added via launch_launcher)
# ═══════════════════════════════════════════════════════════════════════════════


def run_web_launcher():
    """Attempt to use embedded Python; if blocked, fall back to system Python."""
    print("\n=== Bootstrapping environment for web launcher ===\n")
    prepare_user_data()
    if setup_embedded_python():
        print("✅ Embedded Python is usable.")
        if not install_base_packages(EMBEDDED_PYTHON):
            return False
        method = ensure_portablemc(EMBEDDED_PYTHON)
        if not method:
            return False
        print("✅ Setup complete. Launching pmc_web.py with embedded Python...")
        return launch_launcher(method, EMBEDDED_PYTHON)

    print("\n⚠️ Embedded Python not usable. Trying system Python...")
    sys_python = get_system_python()
    if not sys_python:
        print("❌ No system Python found. Cannot proceed.")
        return False

    user_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    launcher_python_dir = user_appdata / "PythonLauncher"
    launcher_python_dir.mkdir(exist_ok=True)

    env = os.environ.copy()
    env["PYTHONUSERBASE"] = str(launcher_python_dir)
    env["PYTHONNOUSERSITE"] = "0"
    env["PYTHONPATH"] = ""
    print("📦 Installing required packages with system Python...")
    for pkg in BASE_PACKAGES + ["portablemc"]:
        print(f"   Installing {pkg}...")
        if not install_packages_with_fallback(
            [pkg], python_exe=sys_python, isolated=False, user=True
        ):
            print(f"❌ Failed to install {pkg}.")
            return False
        print(
            f"   ✅ {pkg} installed via {LAUNCHER_STATE.get('installer_backend', 'pip')}."
        )

    method = test_portablemc(sys_python)
    if not method:
        diag = collect_portablemc_diagnostics(sys_python, env=env)
        dump = write_failure_dump(
            "web",
            "portablemc unavailable after system Python install.",
            extra={"mode": "web", "diagnostics": diag},
        )
        if dump:
            print(f"📝 Failure dump written: {dump}")
        print("❌ portablemc not available even after installation.")
        return False

    print("✅ Setup complete. Launching pmc_web.py with system Python...")
    extra_env = {
        "PYTHONUSERBASE": str(launcher_python_dir),
        "PYTHONNOUSERSITE": "0",
        "PATH": os.environ["PATH"],
    }
    return launch_launcher(method, sys_python, extra_env)


# ═══════════════════════════════════════════════════════════════════════════════
# § 15  MSBUILD LAUNCHER  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def run_msbuild_launcher():
    print("\n=== Launching via MSBuild ===\n")
    prepare_user_data()

    candidates = find_msbuild_candidates()
    if not candidates:
        print("❌ No MSBuild.exe found on the system.")
        return False

    for msbuild_path in candidates:
        print(f"Trying MSBuild at: {msbuild_path}")
        env = os.environ.copy()
        env["__COMPAT_LAYER"] = "RUNASINVOKER"
        env["PORTABLEMC_VERSION"] = PORTABLEMC_VERSION
        env["PORTABLEMC_RELEASE_BASE"] = PORTABLEMC_RELEASE_BASE
        env["LAUNCHER_VERBOSE"] = env.get("LAUNCHER_VERBOSE", "0")
        if ALLOW_INSECURE_SSL:
            env["ALLOW_INSECURE_SSL"] = "true"

        cmd = [
            msbuild_path,
            str(TARGETS_FILE),
            f"/p:Username={DEFAULT_USERNAME}",
            f"/p:ServerIp={DEFAULT_SERVER_IP}",
            f'/p:JvmOpts="{DEFAULT_JVM_OPTS}"',
            "/p:UsePowerShell=true",
            "/p:UseCsc=false",
            "/p:UseVbs=false",
        ]
        print(f"Executing: {' '.join(cmd)}")
        try:
            result_code, output = run_command_live(cmd, env=env)
            if result_code != 0 and "blocked by group policy" in (output or "").lower():
                print(
                    "⚠️ PowerShell blocked. Retrying this MSBuild candidate with VBS fallback."
                )
                cmd_vbs = [
                    msbuild_path,
                    str(TARGETS_FILE),
                    f"/p:Username={DEFAULT_USERNAME}",
                    f"/p:ServerIp={DEFAULT_SERVER_IP}",
                    f'/p:JvmOpts="{DEFAULT_JVM_OPTS}"',
                    "/p:UsePowerShell=false",
                    "/p:UseCsc=false",
                    "/p:UseVbs=true",
                ]
                vbs_code, vbs_output = run_command_live(cmd_vbs, env=env)
                if vbs_code == 0:
                    print("✅ MSBuild succeeded via VBS fallback.")
                    return True
                output = (
                    (output or "") + "\n\n[VBS retry output]\n" + (vbs_output or "")
                )
                result_code = vbs_code
            if result_code == 0:
                print("✅ MSBuild succeeded.")
                return True
            else:
                dump = write_failure_dump(
                    kind="msbuild",
                    message=f"MSBuild candidate failed: {msbuild_path}",
                    command=cmd,
                    output=output,
                    returncode=result_code,
                    extra={"candidate": msbuild_path},
                )
                if dump:
                    print(f"📝 Failure dump written: {dump}")
                print(
                    f"⚠️ MSBuild at {msbuild_path} exited with code {result_code}. Trying next candidate."
                )
        except KeyboardInterrupt:
            raise
        except Exception as e:
            dump = write_failure_dump(
                kind="msbuild",
                message=f"Exception while running MSBuild candidate: {msbuild_path}",
                command=cmd,
                exc=e,
                extra={"candidate": msbuild_path},
            )
            if dump:
                print(f"📝 Failure dump written: {dump}")
            print(
                f"⚠️ Failed to execute MSBuild at {msbuild_path}: {e}. Trying next candidate."
            )

    print("❌ All MSBuild candidates failed.")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# § 16  CLI LAUNCHER  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def run_cli_launcher():
    """Launch portablemc in CLI mode using the embedded Python (or system Python fallback)."""
    print("\n=== Bootstrapping environment for CLI launcher ===\n")
    prepare_user_data()
    failure_context = {
        "mode": "cli",
        "server": DEFAULT_SERVER_IP,
        "username": DEFAULT_USERNAME,
    }

    if setup_embedded_python():
        method = install_portablemc_via_embedded()
        if not method:
            dump = write_failure_dump(
                "cli",
                "Embedded Python portablemc install failed.",
                extra=failure_context,
            )
            if dump:
                print(f"📝 Failure dump written: {dump}")
            return False

        print("📦 Installing certifi for SSL support...")
        if not install_packages_with_fallback(
            ["certifi"], isolated=True, python_exe=EMBEDDED_PYTHON
        ):
            print("⚠️ Failed to install certifi; SSL errors may occur.")
        else:
            print("✅ certifi installed.")

        cert_path = get_certifi_path(EMBEDDED_PYTHON)
        ensure_junctions()

        jvm_arg_tokens = [arg for arg in DEFAULT_JVM_OPTS.split() if arg.strip()]
        portablemc_args = [
            "--main-dir",
            ".",
            "--output",
            "human-color",
            "start",
            "--server",
            DEFAULT_SERVER_IP,
            "fabric:",
            "-u",
            DEFAULT_USERNAME,
        ]
        for token in reversed(jvm_arg_tokens):
            portablemc_args.insert(7, f"--jvm-arg={token}")

        env = os.environ.copy()
        env["__COMPAT_LAYER"] = "RUNASINVOKER"
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONPATH"] = ""
        env["LAUNCHER_ROOT"] = str(ROOT_DIR)
        if cert_path:
            env["SSL_CERT_FILE"] = cert_path
            env["REQUESTS_CA_BUNDLE"] = cert_path

        used_cmd = []
        try:
            result_code, output, used_cmd = run_portablemc_with_uvx_fallback(
                portablemc_args, env=env, cwd=BASE_DIR, python_exe=EMBEDDED_PYTHON
            )
            if result_code != 0:
                diag = collect_portablemc_diagnostics(EMBEDDED_PYTHON, env=env)
                dump = write_failure_dump(
                    "cli",
                    "Embedded Python CLI launch failed.",
                    command=used_cmd,
                    output=output,
                    returncode=result_code,
                    extra={**failure_context, "diagnostics": diag},
                )
                if dump:
                    print(f"📝 Failure dump written: {dump}")
                return False
            return True
        except KeyboardInterrupt:
            raise
        except Exception as e:
            diag = collect_portablemc_diagnostics(EMBEDDED_PYTHON, env=env)
            dump = write_failure_dump(
                "cli",
                "Exception during embedded CLI launch.",
                command=used_cmd,
                exc=e,
                extra={**failure_context, "diagnostics": diag},
            )
            if dump:
                print(f"📝 Failure dump written: {dump}")
            print(f"❌ CLI launcher exited with error: {e}")
            return False

    print("\n⚠️ Embedded Python not usable. Trying system Python...")
    sys_python = get_system_python()
    if not sys_python:
        dump = write_failure_dump(
            "cli", "System Python not found for CLI fallback.", extra=failure_context
        )
        if dump:
            print(f"📝 Failure dump written: {dump}")
        print("❌ No system Python found. Cannot proceed.")
        return False

    user_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    launcher_python_dir = user_appdata / "PythonLauncher"
    launcher_python_dir.mkdir(exist_ok=True)

    env = os.environ.copy()
    env["PYTHONUSERBASE"] = str(launcher_python_dir)
    env["PYTHONNOUSERSITE"] = "0"
    env["PYTHONPATH"] = ""

    print("📦 Installing portablemc with system Python...")
    if not install_packages_with_fallback(
        ["portablemc", "certifi"], python_exe=sys_python, isolated=False, user=True
    ):
        dump = write_failure_dump(
            "cli",
            "package install failed in system Python fallback (uv and pip).",
            command=[
                str(sys_python),
                "-m",
                "pip/uv",
                "install",
                "--user",
                "portablemc",
                "certifi",
            ],
            extra=failure_context,
        )
        if dump:
            print(f"📝 Failure dump written: {dump}")
        print("❌ Failed to install portablemc/certifi.")
        return False

    method = test_portablemc(sys_python)
    if not method:
        diag = collect_portablemc_diagnostics(sys_python, env=env)
        dump = write_failure_dump(
            "cli",
            "portablemc unavailable after system Python install.",
            extra={**failure_context, "diagnostics": diag},
        )
        if dump:
            print(f"📝 Failure dump written: {dump}")
        print("❌ portablemc not available after installation.")
        return False

    cert_path = get_certifi_path(sys_python)
    ensure_junctions()

    jvm_arg_tokens = [arg for arg in DEFAULT_JVM_OPTS.split() if arg.strip()]
    portablemc_args = [
        "--main-dir",
        ".",
        "--output",
        "human-color",
        "start",
        "--server",
        DEFAULT_SERVER_IP,
        "fabric:",
        "-u",
        DEFAULT_USERNAME,
    ]
    for token in reversed(jvm_arg_tokens):
        portablemc_args.insert(7, f"--jvm-arg={token}")
    env["__COMPAT_LAYER"] = "RUNASINVOKER"
    env["LAUNCHER_ROOT"] = str(ROOT_DIR)
    if cert_path:
        env["SSL_CERT_FILE"] = cert_path
        env["REQUESTS_CA_BUNDLE"] = cert_path

    used_cmd = []
    try:
        result_code, output, used_cmd = run_portablemc_with_uvx_fallback(
            portablemc_args, env=env, cwd=BASE_DIR, python_exe=sys_python
        )
        if result_code != 0:
            diag = collect_portablemc_diagnostics(sys_python, env=env)
            dump = write_failure_dump(
                "cli",
                "System Python CLI launch failed.",
                command=used_cmd,
                output=output,
                returncode=result_code,
                extra={**failure_context, "diagnostics": diag},
            )
            if dump:
                print(f"📝 Failure dump written: {dump}")
            return False
        return True
    except KeyboardInterrupt:
        raise
    except Exception as e:
        diag = collect_portablemc_diagnostics(sys_python, env=env)
        dump = write_failure_dump(
            "cli",
            "Exception during system Python CLI launch.",
            command=used_cmd,
            exc=e,
            extra={**failure_context, "diagnostics": diag},
        )
        if dump:
            print(f"📝 Failure dump written: {dump}")
        print(f"❌ CLI launcher exited with error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# § 17  IN-PROCESS LAUNCHER  ← NEW
#        Loads jvm.dll into RAM via PythonMemoryModule, boots the JVM through
#        JPype's IAT-hooked startJVM(), and runs the Minecraft main class
#        entirely in the current Python process.  No java.exe is spawned.
# ═══════════════════════════════════════════════════════════════════════════════


def run_inprocess_launcher(memory_resident: bool = False):
    """
    Memory-resident JVM launcher — the entire game runs inside this process.

    Boot sequence
    -------------
    1. Validate JDK path and glue scripts exist on disk.
    2. Add scripts/ to sys.path so jvm_boot_glue and jvm_memory_loader are
       importable, and so pythonmemorymodule (bundled in scripts/) is too.
    3. Ensure jpype1 is installed (pip install if absent).
    4. Prepare game data directories and junctions.
    5. Import launch_minecraft from jvm_boot_glue and call it.
       Inside that call:
         a) JvmMemoryLoader maps verify.dll → jli.dll → java.dll → jvm.dll
            into VirtualAlloc'd buffers and resolves JNI_CreateJavaVM.
         b) JvmBootGlue patches _jpype.pyd's IAT (LoadLibraryW +
            GetProcAddress) to intercept the JVM load.
         c) jpype.startJVM() is called — C++ gets the fake HINSTANCE,
            calls our JNI_CreateJavaVM trampoline → JVM boots from RAM.
         d) IAT is restored; JPype is fully operational.
         e) jpype.JClass(main_class).main(game_args[:]) is called.
            Minecraft runs. This call blocks until the game exits.

    No .exe files are spawned at any point in this path.
    """
    print("\n" + "=" * 62)
    env_memory_resident = (
        os.environ.get("LAUNCHER_MEMORY_JVM", "").strip().lower() in TRUTHY_ENV_VALUES
    )
    memory_resident = bool(memory_resident or env_memory_resident)
    mode_name = "inprocess_memory" if memory_resident else "inprocess_jpype"
    effective_jdk_bin = resolve_effective_launcher_jdk_bin(memory_resident=memory_resident)

    def _enable_inprocess_crash_log() -> None:
        """
        Enable Python faulthandler early so native crashes during manual-mapped
        DLL bootstrap are captured (even before jvm_boot_glue imports).
        The file is truncated each run because HotSpot uses handled AVs for
        null-pointer checks; we only care about fatal unhandled crashes.
        """
        try:
            import faulthandler
            import time

            d = ROOT_DIR / "logs"
            d.mkdir(parents=True, exist_ok=True)
            log_path = d / "python_native_crash.log"
            f = log_path.open("w", encoding="utf-8")
            f.write(
                f"=== main.py in-process crash capture {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
            )
            f.flush()
            faulthandler.enable(file=f, all_threads=True)
        except Exception:
            pass

    _enable_inprocess_crash_log()
    print("   In-Process Launcher  (JPype JVM, no java.exe)")
    print("=" * 62 + "\n")

    prepare_user_data()
    ensure_junctions()

    # ── 1. Verify JDK and glue scripts ────────────────────────────────────
    print("[ 1/4 ] Verifying JDK and glue scripts…")
    if memory_resident and effective_jdk_bin.resolve() == JDK_BIN.resolve():
        print(
            "ℹ️  java-runtime-epsilon not detected — falling back to OpenJDK25 for manual map."
        )
    if not _verify_jdk_at(effective_jdk_bin):
        dump = write_failure_dump(
            "inprocess",
            f"JDK not found or jvm.dll missing at {effective_jdk_bin}",
            extra={"jdk_bin": str(effective_jdk_bin), "mode": mode_name},
        )
        if dump:
            print(f"📝 Failure dump: {dump}")
        return False

    for script in (JVM_BOOT_GLUE_PY, JVM_MEMORY_LOADER_PY):
        if not script.exists():
            msg = f"Required script missing: {script}"
            print(f"❌ {msg}")
            write_failure_dump("inprocess", msg, extra={"mode": mode_name})
            return False
    print("✅ All glue scripts present.\n")

    # ── 2 & 3. Dependencies ────────────────────────────────────────────────
    print("[ 2/4 ] Bootstrapping in-process dependencies…")
    if not _bootstrap_inprocess_deps():
        dump = write_failure_dump(
            "inprocess",
            "Failed to satisfy in-process dependencies (jpype1 / pythonmemorymodule).",
            extra={"mode": mode_name},
        )
        if dump:
            print(f"📝 Failure dump: {dump}")
        return False
    print()

    # ── 4. Import glue ────────────────────────────────────────────────────
    print("[ 3/4 ] Importing jvm_boot_glue…")
    try:
        from jvm_boot_glue import launch_minecraft  # type: ignore[import]
    except ImportError as exc:
        msg = f"Cannot import jvm_boot_glue: {exc}"
        print(f"❌ {msg}")
        write_failure_dump("inprocess", msg, exc=exc, extra={"mode": mode_name})
        return False
    print("✅ jvm_boot_glue imported.\n")

    # ── 5. Launch ──────────────────────────────────────────────────────────
    print("[ 4/4 ] Booting JVM and launching Minecraft…")
    print(f"        version  : {INPROCESS_VERSION}")
    print(f"        username : {DEFAULT_USERNAME}")
    print(f"        server   : {DEFAULT_SERVER_IP}")
    print(f"        jdk_bin  : {effective_jdk_bin}")
    print(f"        main_dir : {BASE_DIR}")
    print(f"        loader   : {'memory-resident' if memory_resident else 'os-loader'}")
    print()

    # Build extra JVM flags: DEFAULT_JVM_OPTS tokens + auto-join server flag.
    # portablemc will supply -Djava.library.path and -cp via its Game struct;
    # we only add user-facing heap / GC flags here.
    jvm_flags = [tok for tok in DEFAULT_JVM_OPTS.split() if tok.strip()]

    # Auto-join the configured server at startup.
    # Fabric / vanilla 1.6+ support --server and --port as game args; we pass
    # them through PortableMCGameAdapter.resolve() → game_args automatically
    # when portablemc includes them.  As an extra safety net we append them
    # here as well; duplicates are harmless.
    extra_game_args: list[str] = []
    if DEFAULT_SERVER_IP:
        extra_game_args += ["--server", DEFAULT_SERVER_IP]

    try:
        launch_minecraft(
            jdk_bin=effective_jdk_bin,
            main_dir=BASE_DIR,
            version=INPROCESS_VERSION,
            username=DEFAULT_USERNAME,
            access_token="0",  # offline-mode token
            extra_jvm_flags=jvm_flags,
            extra_game_args=extra_game_args,
            debug=bool(
                os.environ.get("LAUNCHER_VERBOSE", "").strip().lower()
                in TRUTHY_ENV_VALUES
            ),
            memory_resident=memory_resident,
        )
        print("\n✅ Minecraft exited normally.")
        return True

    except KeyboardInterrupt:
        print("\n[HALT] In-process launcher interrupted by user.")
        return True  # not a failure

    except Exception as exc:
        msg = f"In-process launcher raised: {type(exc).__name__}: {exc}"
        print(f"\n❌ {msg}")
        dump = write_failure_dump(
            "inprocess",
            msg,
            exc=exc,
            extra={
                "mode": mode_name,
                "jdk_bin": str(effective_jdk_bin),
                "version": INPROCESS_VERSION,
                "username": DEFAULT_USERNAME,
                "memory_resident": memory_resident,
            },
        )
        if dump:
            print(f"📝 Failure dump: {dump}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# § 18  MAIN  — updated menu (option 4 = in-process, d = debug)
# ═══════════════════════════════════════════════════════════════════════════════


def _read_main_menu_choice() -> str:
    """
    Read the main launcher menu.

    Direct keys still work (1/2/3/4/5/d/q). On Windows consoles, Up/Down cycles
    through the same choices and Enter accepts the highlighted item.
    """
    choices = ["1", "2", "3", "4", "5", "d", "q"]
    labels = {
        "1": "Web launcher",
        "2": "MSBuild launcher",
        "3": "CLI launcher",
        "4": "In-Process JPype",
        "5": "In-Process JPype + memory JVM",
        "d": "Debug menu",
        "q": "Quit",
    }

    def supports_msvcrt_key_stream() -> bool:
        if os.name != "nt" or not sys.stdin.isatty():
            return False
        # IDLE and similar shells expose Python file-like stdin, not the
        # low-level console key stream that msvcrt.getwch() needs.
        stdin_module = type(sys.stdin).__module__.lower()
        if "idlelib" in stdin_module or "_pyrepl" in stdin_module:
            return False
        try:
            import ctypes
            import msvcrt

            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            mode = ctypes.c_uint()
            return bool(
                ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            )
        except (AttributeError, ImportError, OSError, ValueError):
            return False

    if not supports_msvcrt_key_stream():
        return input("\nEnter choice: ").strip().lower()

    import msvcrt

    selected = 0
    print()

    def redraw() -> None:
        print(
            f"\rEnter choice: {choices[selected]} ({labels[choices[selected]]})"
            + " " * 24,
            end="",
            flush=True,
        )

    redraw()
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            print()
            return choices[selected]
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            if code == "H":
                selected = (selected - 1) % len(choices)
                redraw()
            elif code == "P":
                selected = (selected + 1) % len(choices)
                redraw()
            continue

        key = ch.lower()
        if key in choices:
            print(f"\rEnter choice: {key} ({labels[key]})" + " " * 24)
            return key


def main():
    initialize_runtime_configuration()

    print("\n" + "=" * 62)
    print("         Minecraft Launcher  –  Choose a Method")
    print("=" * 62)
    print("  1)  Web launcher      – browser UI (Flask + pmc_web.py)")
    print("  2)  MSBuild launcher  – Launcher.targets (signed binaries)")
    print("  3)  CLI launcher      – portablemc in terminal")
    print("  4)  In-Process        - JPype + OS-loaded JVM, no java.exe")
    print("  5)  In-Process        - JPype + memory JVM, no java.exe")
    print("  d)  Debug menu")
    print("  q)  Quit")
    print("=" * 62)
    choice = _read_main_menu_choice()

    if choice == "1":
        update_launcher_state(last_run_mode="web")
        success = run_web_launcher()

    elif choice == "2":
        update_launcher_state(last_run_mode="msbuild")
        success = run_msbuild_launcher()

    elif choice == "3":
        update_launcher_state(last_run_mode="cli")
        success = run_cli_launcher()

    elif choice == "4":
        update_launcher_state(last_run_mode="inprocess_jpype")
        os.environ.pop("LAUNCHER_MEMORY_JVM", None)
        success = run_inprocess_launcher(memory_resident=False)

    elif choice == "5":
        update_launcher_state(last_run_mode="inprocess_memory")
        os.environ["LAUNCHER_MEMORY_JVM"] = "1"
        success = run_inprocess_launcher(memory_resident=True)

    elif choice == "d":
        run_debug_menu()
        success = True

    elif choice == "q":
        print("Exiting.")
        sys.exit(0)

    else:
        print("Invalid choice. Exiting.")
        sys.exit(1)

    update_launcher_state(
        success=bool(success),
        last_error="" if success else "launcher returned failure",
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[HALT] Keyboard interrupted.")
        sys.exit(130)
