#!/usr/bin/env python3
"""
Cross‑platform bootstrap script for embedded Python 3.14 (Windows only).
Attempts to download the native portablemc binary, falls back to pip.
Run with any Python (e.g., system 3.11) to set up and launch the launcher.
"""

import os
import sys
import ssl
import subprocess
import urllib.request
import zipfile
import tarfile
import shutil
import platform
from pathlib import Path

# --- Windows-only guard ---
if platform.system().lower() != 'windows':
    print("❌ This bootstrap script currently only supports Windows.")
    sys.exit(1)

# --- Configuration ---
EMBEDDED_DIR = Path(__file__).parent / "python"
EMBEDDED_PYTHON = EMBEDDED_DIR / "python.exe"
PYTHON_VERSION = "3.14.3"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
BASE_PACKAGES = ["flask", "flask-socketio", "psutil", "ansi2html"]
PORTABLEMC_BIN_DIR = Path(__file__).parent / "portablemc_bin"

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

# --- Helper functions ---
def ensure_embedded_python():
    if EMBEDDED_PYTHON.exists():
        print("✅ Embedded Python already exists.")
        return True
    print("📥 Downloading embedded Python...")
    try:
        urllib.request.urlretrieve(PYTHON_URL, "python-embed.zip")
    except Exception as e:
        print(f"❌ Failed to download Python: {e}")
        return False
    print("📦 Extracting...")
    with zipfile.ZipFile("python-embed.zip", "r") as zip_ref:
        zip_ref.extractall(EMBEDDED_DIR)
    os.remove("python-embed.zip")
    print("✅ Embedded Python ready.")
    return True

def fix_pth_file():
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

def download_get_pip():
    pip_script = EMBEDDED_DIR / "get-pip.py"
    if pip_script.exists():
        print("✅ get-pip.py already present.")
        return pip_script

    print("📥 Downloading get-pip.py (ignoring SSL cert for this request)...")
    try:
        # Create an unverified SSL context to bypass the certificate error
        ssl_context = ssl._create_unverified_context()
        with urllib.request.urlopen(GET_PIP_URL, context=ssl_context) as response:
            with open(pip_script, 'wb') as out_file:
                out_file.write(response.read())
        print("✅ get-pip.py downloaded successfully.")
    except Exception as e:
        print(f"❌ Failed to download get-pip.py: {e}")
        return None
    return pip_script

def run_pip_command(args, isolated=True):
    env = os.environ.copy()
    if isolated:
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONPATH"] = ""
    cmd = [str(EMBEDDED_PYTHON)] + args
    # SAFE: args are static or come from trusted BASE_PACKAGES
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ Pip command failed: {' '.join(args)}")
        print(result.stderr)
        return False
    print(result.stdout)
    return True

def install_pip():
    pip_script = download_get_pip()
    if not pip_script:
        return False
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = ""
    cmd = [str(EMBEDDED_PYTHON), str(pip_script),
           "--trusted-host=files.pythonhosted.org",
           "--trusted-host=pypi.org"]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        print("❌ Failed to install pip.")
        print(result.stderr)
        return False
    print("✅ pip installed.")
    return True

def install_base_packages():
    print("📦 Installing base packages...")
    if not run_pip_command(["-m", "pip", "install", "--upgrade", "pip"], isolated=True):
        print("⚠️ Pip upgrade failed, continuing anyway.")
    for pkg in BASE_PACKAGES:
        print(f"   Installing {pkg}...")
        if not run_pip_command(["-m", "pip", "install", pkg], isolated=True):
            print(f"❌ Failed to install {pkg}.")
            return False
    return True

def download_portablemc_binary():
    url = get_portablemc_url()
    if not url:
        return False
    print(f"📥 Downloading portablemc from {url}...")
    archive_path = Path(__file__).parent / "portablemc_download"
    try:
        urllib.request.urlretrieve(url, archive_path)
    except Exception as e:
        print(f"❌ Failed to download: {e}")
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
    # Move all extracted files to the root of PORTABLEMC_BIN_DIR
    for item in PORTABLEMC_BIN_DIR.iterdir():
        if item.is_dir():
            for sub in item.iterdir():
                sub.rename(PORTABLEMC_BIN_DIR / sub.name)
            item.rmdir()
    print(f"✅ portablemc binary extracted to {PORTABLEMC_BIN_DIR}")
    return True

def test_portablemc():
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
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                print("✅ portablemc binary works.")
                return "binary"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            print("⏱️ portablemc binary check timed out or not found, trying module.")

    # Try module
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = ""
    cmd = [str(EMBEDDED_PYTHON), "-m", "portablemc", "--help"]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print("✅ portablemc module works.")
            return "module"
    except subprocess.TimeoutExpired:
        print("⏱️ portablemc module check timed out, assuming not available.")
    return None

def ensure_portablemc():
    """Make portablemc available – try binary, fallback to pip. Returns method string or None."""
    method = test_portablemc()
    if method:
        return method
    if download_portablemc_binary():
        method = test_portablemc()
        if method:
            return method
        print("⚠️ Binary download failed, falling back to pip.")
    print("📦 Installing portablemc via pip...")
    if run_pip_command(["-m", "pip", "install", "portablemc"], isolated=True):
        method = test_portablemc()
        if method:
            return method
    return None

def launch_launcher(method):
    launcher_script = Path(__file__).parent / "portablemc.py"
    if not launcher_script.exists():
        print("❌ portablemc.py not found in the same directory.")
        return False

    env = os.environ.copy()
    paths = [str(EMBEDDED_DIR), str(EMBEDDED_DIR / "Scripts"), str(PORTABLEMC_BIN_DIR)]
    env["PATH"] = os.pathsep.join(paths) + os.pathsep + env.get("PATH", "")
    env["__compat_layer"] = "runasinvoker"
    env["PYTHONHOME"] = str(EMBEDDED_DIR)
    env["CLICOLOR_FORCE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = ""
    env["PORTABLEMC_METHOD"] = method

    cmd = [str(EMBEDDED_PYTHON), str(launcher_script)]
    print(f"🚀 Launching: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, env=env, check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Launcher exited with error: {e}")
        return False
    except KeyboardInterrupt:
        print("⏹️ Interrupted by user.")
    return True

def main():
    print("=== Embedded Python Bootstrap (Windows only) ===")
    if not ensure_embedded_python():
        sys.exit(1)
    if not fix_pth_file():
        sys.exit(1)
    # Check pip
    env_check = os.environ.copy()
    env_check["PYTHONNOUSERSITE"] = "1"
    env_check["PYTHONPATH"] = ""
    pip_check = subprocess.run([str(EMBEDDED_PYTHON), "-m", "pip", "--version"],
                               env=env_check, capture_output=True, text=True)
    if pip_check.returncode != 0:
        print("📦 pip not found, installing...")
        if not install_pip():
            sys.exit(1)
    else:
        print(f"✅ pip already installed: {pip_check.stdout.strip()}")
    if not install_base_packages():
        sys.exit(1)
    method = ensure_portablemc()
    if not method:
        sys.exit(1)
    print("✅ Setup complete. Launching portablemc.py...")
    launch_launcher(method)

if __name__ == "__main__":
    main()
