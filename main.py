#!/usr/bin/env python3
r"""
Cross‑platform bootstrap script for embedded Python 3.14 (Windows only).
All files are stored in %LOCALAPPDATA%\PortableMC.
Run with any Python (e.g., system 3.11) to set up and launch the launcher.
"""

import os
import sys
import subprocess
import stat
import urllib.request
import zipfile
import tarfile
import shutil
import platform
import ssl
import winreg
from pathlib import Path

# --- Windows-only guard ---
if platform.system().lower() != 'windows':
    print("❌ This bootstrap script currently only supports Windows.")
    sys.exit(1)

# --- Configuration ---
# Use AppData for all downloads and runtime files
APPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
BASE_DIR = APPDATA / "PortableMC"
EMBEDDED_DIR = BASE_DIR / "python"
EMBEDDED_PYTHON = EMBEDDED_DIR / "python.exe"
PYTHON_VERSION = "3.14.3"
PYTHON_VERSIONS = ["3.15", "3.14", "3.13", "3.12", "3.11"]
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
BASE_PACKAGES = ["flask", "flask-socketio", "psutil", "ansi2html", "certifi"]   
PORTABLEMC_BIN_DIR = BASE_DIR / "portablemc_bin"
ALLOW_INSECURE_SSL = os.environ.get("ALLOW_INSECURE_SSL", "").lower() in ("1", "true", "yes")

# Default game settings
DEFAULT_USERNAME = "CubeUniform840"
DEFAULT_SERVER_IP = "77.103.184.72"
DEFAULT_JVM_OPTS = "-Xmx3G -Xms3G"

# Paths
ROOT_DIR = Path(__file__).parent
SCRIPTS_DIR = ROOT_DIR / "Scripts"
SCRIPTS_DIR.mkdir(exist_ok=True)
MSBUILD_PATH = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\MSBuild.exe"

# Launcher files inside the Scripts folder
TARGETS_FILE = SCRIPTS_DIR / "Launcher.targets"
LAUNCHER_VBS = SCRIPTS_DIR / "Launcher.vbs"
LAUNCHER_PS1 = SCRIPTS_DIR / "Launcher.ps1"
PORTABLEMC_PY = SCRIPTS_DIR / "portablemc.py"

# --- OS and architecture detection (for portablemc binary) ---
SYSTEM = platform.system().lower()
MACHINE = platform.machine().lower()

ARCH_MAP = {
    'x86_64': 'x86_64',
    'amd64': 'x86_64',
    'i686': 'i686',
    'i386': 'i686',
    'aarch64': 'aarch64',
    'arm64': 'aarch64',
    'armv7l': 'arm-gnueabihf',
    'arm': 'arm-gnueabihf',
}

OS_MAP = {
    'windows': 'windows',
    'linux': 'linux',
    'darwin': 'macos',
}

def get_portablemc_url():
    """Return the download URL for the native portablemc binary, or None."""
    os_name = OS_MAP.get(SYSTEM)
    if not os_name:
        print(f"⚠️ Unsupported OS: {SYSTEM}")
        return None
    arch = ARCH_MAP.get(MACHINE, 'x86_64')
    if os_name == 'macos':
        arch = 'aarch64' if arch == 'aarch64' else 'x86_64'
    if os_name == 'linux' and arch not in ('arm-gnueabihf',):
        arch += '-gnu'
    base = "https://github.com/mindstorm38/portablemc/releases/download/v5.0.2/"
    if os_name == 'windows':
        ext = "zip"
        filename = f"portablemc-5.0.2-{os_name}-{arch}-msvc.{ext}"
    else:
        ext = "tar.gz"
        filename = f"portablemc-5.0.2-{os_name}-{arch}.{ext}"
    return base + filename

# --- Data functions ---
def prepare_user_data():
    """Move static folder and game files to BASE_DIR if not already present."""
    base_dir = BASE_DIR
    root_dir = ROOT_DIR

    # Static folder
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

    # Game files
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

# --- Junction functions ---
def is_junction(path):
    """Return True if path is a junction (reparse point)."""
    try:
        attrs = os.lstat(str(path))
        return (attrs.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) != 0
    except OSError:
        return False

def create_junction(source, target):
    """
    Create a junction from source to target.
    Safely removes any existing target before creating the junction.
    Returns True if a junction was created, False otherwise (fallback to regular directory).
    """
    source_path = Path(source).resolve()
    target_path = Path(target).resolve()

    # Prevent self‑junction
    if source_path == target_path:
        print(f"⚠️ Source and target are the same ({source_path}); skipping junction creation.")
        # Still ensure the directory exists (as a regular folder)
        target_path.mkdir(parents=True, exist_ok=True)
        return False

    # Ensure source exists
    source_path.mkdir(parents=True, exist_ok=True)

    # Remove existing target if it exists
    if target_path.exists():
        if is_junction(target_path):
            os.rmdir(str(target_path))  # removes only the junction
            print(f"Removed existing junction: {target_path}")
        elif target_path.is_dir():
            shutil.rmtree(str(target_path), ignore_errors=True)
            print(f"Removed existing directory: {target_path}")
        else:
            target_path.unlink()

    # Try to create junction
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target_path), str(source_path)],
            check=True, capture_output=True, text=True
        )  # nosec
        print(f"✅ Junction created: {target_path} -> {source_path}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Could not create junction (falling back to regular directory): {e.stderr}")
        target_path.mkdir(parents=True, exist_ok=True)
        return False

def ensure_junctions():
    r"""Ensure mods and resourcepacks directories exist in %LOCALAPPDATA%\PortableMC."""
    # Compute AppData path directly to avoid any global variable issues
    appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    base_dir = appdata / "PortableMC"
    base_dir.mkdir(parents=True, exist_ok=True)

    create_junction(ROOT_DIR / "mods", base_dir / "mods")
    create_junction(ROOT_DIR / "resourcepacks", base_dir / "resourcepacks")

# --- Download functions ---
def get_ssl_context():
    """Return an unverified SSL context if ALLOW_INSECURE_SSL is True, else None."""
    if ALLOW_INSECURE_SSL:
        print("⚠️ WARNING: SSL certificate verification is disabled (ALLOW_INSECURE_SSL=true).")
        return ssl._create_unverified_context()
    return None

def download_file(url, dest_path):
    """Download a file with optional insecure fallback."""
    try:
        urllib.request.urlretrieve(url, dest_path)
        return True
    except Exception as e:
        print(f"⚠️ First download attempt failed: {e}")
        if ALLOW_INSECURE_SSL:
            print("📥 Retrying with SSL verification disabled...")
            try:
                context = get_ssl_context()
                with urllib.request.urlopen(url, context=context) as response:
                    with open(dest_path, 'wb') as f:
                        f.write(response.read())
                return True
            except Exception as e2:
                print(f"❌ Download failed even with unverified SSL: {e2}")
                return False
        else:
            print("❌ Download failed and insecure SSL is disabled.")
            return False

# --- Helper functions ---
def find_msbuild_candidates():
    """Return a list of candidate MSBuild.exe paths sorted by priority (highest first)."""
    candidates = []

    # 1. vswhere (most reliable, finds the latest Visual Studio)
    vswhere = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
    if os.path.isfile(vswhere):
        try:
            result = subprocess.run([vswhere, "-latest", "-products", "*", "-find", "MSBuild\\**\\Bin\\MSBuild.exe"],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line and os.path.isfile(line):
                        candidates.append((line, 100))  # highest priority
        except Exception:
            pass

    # 2. Hard‑coded candidates with descending priorities
    hardcoded = [
        # Visual Studio 2026 (v18.0)
        (r"C:\Program Files\Microsoft Visual Studio\2026\Enterprise\MSBuild\Current\Bin\MSBuild.exe", 99),
        (r"C:\Program Files\Microsoft Visual Studio\2026\Professional\MSBuild\Current\Bin\MSBuild.exe", 99),
        (r"C:\Program Files\Microsoft Visual Studio\2026\Community\MSBuild\Current\Bin\MSBuild.exe", 99),
        (r"C:\Program Files\Microsoft Visual Studio\2026\BuildTools\MSBuild\Current\Bin\MSBuild.exe", 99),

        # Visual Studio 2022 (v17.0)
        (r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe", 90),
        (r"C:\Program Files\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe", 90),
        (r"C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe", 90),
        (r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe", 90),

        # Visual Studio 2019 (v16.0)
        (r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Enterprise\MSBuild\Current\Bin\MSBuild.exe", 80),
        (r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Professional\MSBuild\Current\Bin\MSBuild.exe", 80),
        (r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Community\MSBuild\Current\Bin\MSBuild.exe", 80),
        (r"C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\MSBuild.exe", 80),

        # Visual Studio 2017 (v15.0)
        (r"C:\Program Files (x86)\Microsoft Visual Studio\2017\Enterprise\MSBuild\15.0\Bin\MSBuild.exe", 70),
        (r"C:\Program Files (x86)\Microsoft Visual Studio\2017\Professional\MSBuild\15.0\Bin\MSBuild.exe", 70),
        (r"C:\Program Files (x86)\Microsoft Visual Studio\2017\Community\MSBuild\15.0\Bin\MSBuild.exe", 70),
        (r"C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\MSBuild\15.0\Bin\MSBuild.exe", 70),

        # Standalone Build Tools (v14.0, v12.0)
        (r"C:\Program Files (x86)\MSBuild\14.0\Bin\MSBuild.exe", 60),
        (r"C:\Program Files (x86)\MSBuild\12.0\Bin\MSBuild.exe", 50),

        # .NET Framework (64‑bit preferred, then 32‑bit)
        (r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\MSBuild.exe", 40),
        (r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\MSBuild.exe", 30),
    ]

    for path, prio in hardcoded:
        if os.path.isfile(path):
            candidates.append((path, prio))

    # Remove duplicates (keep highest priority for each path)
    unique = {}
    for path, prio in candidates:
        if path not in unique or prio > unique[path]:
            unique[path] = prio

    # Sort by priority descending and return only paths
    sorted_candidates = sorted(unique.items(), key=lambda x: x[1], reverse=True)
    return [path for path, _ in sorted_candidates]

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
    if "import site" in content and "#import site" in content:
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
        result = subprocess.run([str(EMBEDDED_PYTHON), "--version"],
                                capture_output=True, text=True, timeout=5) # nosec
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

    # Ensure pip is available
    env_check = os.environ.copy()
    env_check["PYTHONNOUSERSITE"] = "1"
    env_check["PYTHONPATH"] = ""
    pip_check = subprocess.run(
        [str(EMBEDDED_PYTHON), "-m", "pip", "--version"],
        env=env_check, capture_output=True, text=True
    ) # nosec
    if pip_check.returncode != 0:
        print("📦 pip not found, installing...")
        if not install_pip(EMBEDDED_PYTHON):
            return False
    else:
        print(f"✅ pip already installed: {pip_check.stdout.strip()}")
    return True

def install_portablemc_via_embedded():
    """Install portablemc in embedded Python and return method."""
    print("📦 Installing portablemc via pip...")
    if not run_pip_command(["-m", "pip", "install", "portablemc"], isolated=True, python_exe=EMBEDDED_PYTHON):
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
        context = get_ssl_context()  # from earlier (secure or insecure)
        with urllib.request.urlopen(GET_PIP_URL, context=context) as response:
            with open(pip_script, 'wb') as out_file:
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
    result = subprocess.run(cmd, env=env, capture_output=True, text=True) # nosec
    if result.returncode != 0:
        print(f"❌ Pip command failed: {' '.join(args)}")
        print(result.stderr)
        return False
    print(result.stdout)
    return True

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
    cmd = [str(python_exe), str(pip_script),
           "--trusted-host=files.pythonhosted.org",
           "--trusted-host=pypi.org"]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True) # nosec
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
    if not run_pip_command(["-m", "pip", "install", "--upgrade", "pip"], isolated=True, python_exe=python_exe):
        print("⚠️ Pip upgrade failed, continuing anyway.")
    for pkg in BASE_PACKAGES:
        print(f"   Installing {pkg}...")
        if not run_pip_command(["-m", "pip", "install", pkg], isolated=True, python_exe=python_exe):
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
            capture_output=True, text=True, check=True,
            env={"PYTHONNOUSERSITE": "1"}
        ) # nosec
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
    # Flatten subdirectories
    for item in PORTABLEMC_BIN_DIR.iterdir():
        if item.is_dir():
            for sub in item.iterdir():
                sub.rename(PORTABLEMC_BIN_DIR / sub.name)
            item.rmdir()
    print(f"✅ portablemc binary extracted to {PORTABLEMC_BIN_DIR}")
    return True

def test_portablemc(python_exe=None):
    """Check if portablemc is available (binary or module). Returns 'binary' or 'module' or None."""
    # Try binary first
    exe_name = "portablemc.exe" if SYSTEM == "windows" else "portablemc"
    binary_path = PORTABLEMC_BIN_DIR / exe_name
    if binary_path.exists():
        if SYSTEM != "windows":
            binary_path.chmod(binary_path.stat().st_mode | 0o111)
        env = os.environ.copy()
        env["PATH"] = str(PORTABLEMC_BIN_DIR) + os.pathsep + env.get("PATH", "")
        try:
            result = subprocess.run([str(binary_path), "--help"], env=env,
                                    capture_output=True, text=True, timeout=5) # nosec
            if result.returncode == 0:
                print("✅ portablemc binary works.")
                return "binary"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            print("⏱️ portablemc binary check timed out or not found, trying module.")

    # Try module
    if python_exe is None:
        python_exe = EMBEDDED_PYTHON
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = ""
    cmd = [str(python_exe), "-m", "portablemc", "--help"]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=5) # nosec
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
    print("📦 Installing portablemc via pip...")
    if run_pip_command(["-m", "pip", "install", "portablemc"], isolated=True, python_exe=python_exe):
        method = test_portablemc(python_exe)
        if method:
            return method
    return None

# --- System Python detection ---
def get_system_python():
    """Find a system Python 3.x executable, preferring 3.11 or higher.
       Returns the path to a usable Python interpreter, or None.
       Priority order: current interpreter, PATH, registry, common install paths.
    """
    candidates = []
    seen = set()

    def add_candidate(p):
        p = Path(p).resolve()
        if p.exists() and p not in seen:
            seen.add(p)
            candidates.append(p)

    # 1. Current interpreter (if it's not the embedded one)
    current = Path(sys.executable)
    if current != EMBEDDED_PYTHON:
        add_candidate(current)

    # 2. Search PATH (most likely to be user's preferred Python)
    for dir in os.environ.get("PATH", "").split(os.pathsep):
        add_candidate(Path(dir) / "python.exe")
        add_candidate(Path(dir) / "python3.exe")

    # 3. Registry (both HKCU and HKLM)
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

    # 4. Common install locations (static paths)
    for ver in PYTHON_VERSIONS:
        num = ver.replace('.', '')
        # System-wide installations
        for base in (r"C:\Python{}", r"C:\Program Files\Python{}", r"C:\Program Files (x86)\Python{}"):
            add_candidate(base.format(num) + "\\python.exe")
        # User installations
        user_dir = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Programs" / f"Python{num}"
        add_candidate(user_dir / "python.exe")
    # Sysnative and System32 (often contain a 64-bit Python from WOW64)
    add_candidate(r"C:\Windows\Sysnative\python.exe")
    add_candidate(r"C:\Windows\System32\python.exe")

    # Now verify each candidate and extract version
    valid = []
    for p in candidates:
        try:
            result = subprocess.run([str(p), "--version"], capture_output=True, text=True, timeout=2)  # nosec
            combined = (result.stdout + result.stderr).strip()
            if result.returncode == 0 and "Python 3" in combined:
                # Extract version string (e.g., "3.11.5" from "Python 3.11.5")
                parts = combined.split()
                version_str = parts[1] if len(parts) >= 2 else "unknown"
                valid.append((version_str, p))
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
            continue

    if not valid:
        return None

    # Sort by version descending (higher version first)
    valid.sort(key=lambda x: tuple(map(int, x[0].split('.'))), reverse=True)
    best = valid[0][1]
    print(f"Selected system Python: {best}")
    return best

# --- Launcher functions ---
def launch_launcher(method, python_exe=None, extra_env=None):
    launcher_script = PORTABLEMC_PY
    if not launcher_script.exists():
        print("❌ portablemc.py not found in the same directory.")
        return False

    if python_exe is None:
        python_exe = EMBEDDED_PYTHON

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    # Ensure paths for binaries (portablemc binary may be in PORTABLEMC_BIN_DIR)
    paths = [str(EMBEDDED_DIR), str(EMBEDDED_DIR / "Scripts"), str(PORTABLEMC_BIN_DIR)]
    env["PATH"] = os.pathsep.join(paths) + os.pathsep + env.get("PATH", "")
    env["__COMPAT_LAYER"] = "RUNASINVOKER"
    env["LAUNCHER_ROOT"] = str(ROOT_DIR)

    # For embedded Python, set PYTHONHOME; for system Python, do not force isolation
    if python_exe == EMBEDDED_PYTHON:
        env["PYTHONHOME"] = str(EMBEDDED_DIR)
        env["PYTHONNOUSERSITE"] = "1"
    # else: extra_env already contains PYTHONUSERBASE and PYTHONNOUSERSITE="0"

    env["CLICOLOR_FORCE"] = "1"
    env["PYTHONPATH"] = ""
    env["PORTABLEMC_METHOD"] = method

    cert_path = get_certifi_path(python_exe)
    if cert_path:
        env["SSL_CERT_FILE"] = cert_path
        env["REQUESTS_CA_BUNDLE"] = cert_path

    cmd = [str(python_exe), str(launcher_script)]
    print(f"🚀 Launching: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, env=env, check=True)  # nosec
    except subprocess.CalledProcessError as e:
        print(f"❌ Launcher exited with error: {e}")
        return False
    except KeyboardInterrupt:
        print("⏹️ Interrupted by user.")
    return True

def run_web_launcher():
    """Attempt to use embedded Python; if blocked, fall back to system Python."""
    print("\n=== Bootstrapping environment for web launcher ===\n")
    # Move static folders and game files before launch
    prepare_user_data()
    # Try embedded Python
    if setup_embedded_python():
        print("✅ Embedded Python is usable.")
        if not install_base_packages(EMBEDDED_PYTHON):
            return False
        method = ensure_portablemc(EMBEDDED_PYTHON)
        if not method:
            return False
        print("✅ Setup complete. Launching portablemc.py with embedded Python...")
        return launch_launcher(method, EMBEDDED_PYTHON)

    # Embedded Python failed; fall back to system Python
    print("\n⚠️ Embedded Python not usable. Trying system Python...")
    sys_python = get_system_python()
    if not sys_python:
        print("❌ No system Python found. Cannot proceed.")
        return False

    # Create a user-specific directory for Python packages (to avoid admin rights)
    user_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    launcher_python_dir = user_appdata / "PythonLauncher"
    launcher_python_dir.mkdir(exist_ok=True)

    # Set up environment to use this directory for site-packages
    env = os.environ.copy()
    env["PYTHONUSERBASE"] = str(launcher_python_dir)
    env["PYTHONNOUSERSITE"] = "0"
    env["PYTHONPATH"] = ""
    # Install packages with system Python
    print("📦 Installing required packages with system Python...")
    for pkg in BASE_PACKAGES + ["portablemc"]:
        print(f"   Installing {pkg}...")
        cmd = [str(sys_python), "-m", "pip", "install", "--user", pkg]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True) # nosec
        if result.returncode != 0:
            print(f"❌ Failed to install {pkg}: {result.stderr}")
            return False
        print(f"   ✅ {pkg} installed.")

    # Test portablemc with system Python
    method = test_portablemc(sys_python)
    if not method:
        print("❌ portablemc not available even after installation.")
        return False

    print("✅ Setup complete. Launching portablemc.py with system Python...")
    # Launch portablemc.py with system Python, using the same environment
    extra_env = {
        "PYTHONUSERBASE": str(launcher_python_dir),
        "PYTHONNOUSERSITE": "0",
        "PATH": os.environ["PATH"]  # keep the original PATH
    }
    return launch_launcher(method, sys_python, extra_env)

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
        if ALLOW_INSECURE_SSL:
            env["ALLOW_INSECURE_SSL"] = "true"

        cmd = [
            msbuild_path,
            str(TARGETS_FILE),
            f"/p:Username={DEFAULT_USERNAME}",
            f"/p:ServerIp={DEFAULT_SERVER_IP}",
            f"/p:JvmOpts={DEFAULT_JVM_OPTS}"
        ]
        print(f"Executing: {' '.join(cmd)}")
        try:
            # Run without capturing output; let MSBuild print directly
            result = subprocess.run(cmd, env=env)  # nosec
            if result.returncode == 0:
                print("✅ MSBuild succeeded.")
                return True
            else:
                print(f"⚠️ MSBuild at {msbuild_path} exited with code {result.returncode}. Trying next candidate.")
        except Exception as e:
            print(f"⚠️ Failed to execute MSBuild at {msbuild_path}: {e}. Trying next candidate.")

    print("❌ All MSBuild candidates failed.")
    return False

def run_cli_launcher():
    """Launch portablemc in CLI mode using the embedded Python (or system Python fallback)."""
    print("\n=== Bootstrapping environment for CLI launcher ===\n")
    prepare_user_data()

    # Try embedded Python first
    if setup_embedded_python():
        method = install_portablemc_via_embedded()
        if not method:
            return False
        ensure_junctions()

        # Build CLI arguments (module syntax)
        cmd = [
            str(EMBEDDED_PYTHON), "-m", "portablemc",
            "--main-dir", ".",
            "--output", "human-color",
            "start",
            "--server", DEFAULT_SERVER_IP,
            "--jvm-args", DEFAULT_JVM_OPTS,
            "fabric:",
            "-u", DEFAULT_USERNAME
        ]
        env = os.environ.copy()
        env["__COMPAT_LAYER"] = "RUNASINVOKER"
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONPATH"] = ""
        env["LAUNCHER_ROOT"] = str(ROOT_DIR)
        print(f"🚀 Launching: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, env=env, cwd=BASE_DIR, check=True)  # nosec
            return True
        except subprocess.CalledProcessError as e:
            print(f"❌ CLI launcher exited with error: {e}")
            return False
        except KeyboardInterrupt:
            print("⏹️ Interrupted by user.")
            return True

    # Fallback to system Python
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

    # Install portablemc with system Python
    print("📦 Installing portablemc with system Python...")
    cmd = [str(sys_python), "-m", "pip", "install", "--user", "portablemc"]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)  # nosec
    if result.returncode != 0:
        print(f"❌ Failed to install portablemc: {result.stderr}")
        return False

    method = test_portablemc(sys_python)
    if not method:
        print("❌ portablemc not available after installation.")
        return False

    ensure_junctions()

    cmd = [
        str(sys_python), "-m", "portablemc",
        "--main-dir", ".",
        "--output", "human-color",
        "start",
        "--server", DEFAULT_SERVER_IP,
        "--jvm-args", DEFAULT_JVM_OPTS,
        "fabric:",
        "-u", DEFAULT_USERNAME
    ]
    env["__COMPAT_LAYER"] = "RUNASINVOKER"
    env["LAUNCHER_ROOT"] = str(ROOT_DIR)
    print(f"🚀 Launching: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, env=env, cwd=BASE_DIR, check=True)  # nosec
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ CLI launcher exited with error: {e}")
        return False
    except KeyboardInterrupt:
        print("⏹️ Interrupted by user.")
        return True

def main():
    # Display menu
    print("\n" + "=" * 60)
    print("      Minecraft Launcher – Choose a Method")
    print("=" * 60)
    print("1) Web launcher (portablemc.py) – requires setup, runs in browser")
    print("2) MSBuild launcher – uses Launcher.targets (Microsoft‑signed binaries)")
    print("3) CLI launcher – runs portablemc directly in terminal")
    print("q) Quit")
    choice = input("\nEnter choice (1/2/3/q): ").strip()

    if choice == "1":
        success = run_web_launcher()
    elif choice == "2":
        success = run_msbuild_launcher()
    elif choice == "3":
        success = run_cli_launcher()
    elif choice.lower() == "q":
        print("Exiting.")
        sys.exit(0)
    else:
        print("Invalid choice. Exiting.")
        sys.exit(1)

    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()