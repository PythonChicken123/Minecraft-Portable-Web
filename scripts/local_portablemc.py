"""
local_portablemc.py
═══════════════════
Ensure the vendored portablemc (scripts/portablemc/portablemc-py/python/) is
always used instead of any pip-installed copy.

Boot sequence
─────────────
1. Find the pre-built wheel in  scripts/portablemc/target/wheels/*.whl
2. If  portablemc/_portablemc.cp*-win_amd64.pyd  is missing from the
   vendored Python tree, extract it from the wheel automatically (one-time).
3. Prepend the vendored tree to sys.path and strip every other portablemc
   path so pip-installed copies can never shadow the local version.
4. Optionally load the _portablemc.pyd binary in-memory using pythonmemorymodule
   for environments with strict .exe execution policies.

Call ``bootstrap()`` (or ``prepend_vendor_portablemc()``) from every entry
point before any ``import portablemc`` statement.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import sys
import types
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Sequence

log = logging.getLogger(__name__)

# ── Canonical paths ────────────────────────────────────────────────────────

_SCRIPTS_DIR = Path(__file__).resolve().parent
_VENDOR_ROOT = _SCRIPTS_DIR / "portablemc" / "portablemc-py" / "python"
_WHEEL_SEARCH = _SCRIPTS_DIR / "portablemc" / "target" / "wheels"
_PMC_PKG_DIR = _VENDOR_ROOT / "portablemc"

# Track whether the native extension has been loaded in-memory
_PYD_LOADED_INMEMORY: bool = False
_PYD_MEMORY_MODULE: object | None = None


# ── Wheel extraction ───────────────────────────────────────────────────────

def _find_wheel() -> Path | None:
    """Return the first portablemc wheel found in the target/wheels dir."""
    if not _WHEEL_SEARCH.is_dir():
        return None
    # Try different wheel naming patterns (maturin uses portablemc_py-*)
    patterns = ["portablemc_py-*.whl", "portablemc-*.whl"]
    for pattern in patterns:
        for whl in sorted(_WHEEL_SEARCH.glob(pattern), reverse=True):
            return whl  # Return most recent
    return None


def _pyd_present() -> bool:
    """True when the native extension already lives in the vendored tree."""
    if not _PMC_PKG_DIR.is_dir():
        return False
    # Check for both naming conventions (.pyd for Windows, .so for Linux/Mac)
    patterns = ["_portablemc*.pyd", "_portablemc*.so", "portablemc_py*.pyd", "portablemc_py*.so"]
    for pattern in patterns:
        if any(_PMC_PKG_DIR.glob(pattern)):
            return True
    return False


def _extract_pyd_from_wheel(wheel: Path) -> bool:
    """
    Extract every file from the wheel's package directory into the
    vendored Python tree.  Skips dist-info and __pycache__ entries.

    The wheel may contain either:
    - portablemc/ (if built with package name portablemc)
    - portablemc_py/ (if built with maturin default naming)
    
    Files are renamed appropriately to create a portablemc/ package.

    Returns True on success.
    """
    try:
        _PMC_PKG_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(wheel, "r") as zf:
            extracted = 0
            # Detect the package prefix in the wheel
            prefixes = ["portablemc/", "portablemc_py/"]
            found_prefix = None
            for name in zf.namelist():
                for prefix in prefixes:
                    if name.startswith(prefix):
                        found_prefix = prefix
                        break
                if found_prefix:
                    break
            
            if not found_prefix:
                log.warning("No portablemc package found in wheel %s", wheel.name)
                return False
            
            for name in zf.namelist():
                if not name.startswith(found_prefix):
                    continue
                if "__pycache__" in name or name.endswith("/"):
                    continue
                if ".dist-info" in name:
                    continue
                
                # Strip the leading prefix; write relative to _PMC_PKG_DIR
                rel = name[len(found_prefix):]
                if not rel:
                    continue
                
                # Rename portablemc_py to _portablemc in filenames
                rel = rel.replace("portablemc_py", "_portablemc")
                
                dest = _PMC_PKG_DIR / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                data = zf.read(name)
                dest.write_bytes(data)
                extracted += 1
                log.debug("Extracted %s → %s", name, dest)
        
        log.info("Extracted %d files from %s into %s", extracted, wheel.name, _PMC_PKG_DIR)
        return extracted > 0
    except Exception as exc:
        log.warning("Failed to extract wheel %s: %s", wheel, exc)
        return False


def _ensure_pyd() -> bool:
    """Make sure _portablemc.pyd is present; extract from wheel if needed."""
    if _pyd_present():
        return True
    wheel = _find_wheel()
    if wheel is None:
        log.warning(
            "No portablemc wheel found in %s — _portablemc extension unavailable",
            _WHEEL_SEARCH,
        )
        return False
    log.info("Extracting native extension from %s …", wheel.name)
    return _extract_pyd_from_wheel(wheel)


# ── sys.path management ────────────────────────────────────────────────────

def _is_foreign_portablemc(path: str) -> bool:
    """
    True when *path* looks like it belongs to a pip-installed portablemc but
    is NOT our vendored tree.
    """
    p = Path(path)
    if not (p / "portablemc").is_dir():
        return False
    # Our own vendored root — keep it
    try:
        if p.resolve() == _VENDOR_ROOT.resolve():
            return False
    except OSError:
        pass
    # site-packages or dist-packages → it's pip-installed, remove it
    lp = path.lower().replace("\\", "/")
    return "site-packages" in lp or "dist-packages" in lp


def _scrub_pip_portablemc() -> None:
    """Remove pip-installed portablemc directories from sys.path."""
    before = sys.path[:]
    sys.path[:] = [p for p in sys.path if not _is_foreign_portablemc(p)]
    removed = [p for p in before if p not in sys.path]
    for p in removed:
        log.info("Removed pip portablemc from sys.path: %s", p)


def _prepend_vendor() -> bool:
    """Prepend the vendored portablemc tree to sys.path if it exists."""
    if not _VENDOR_ROOT.is_dir():
        log.warning("Vendored portablemc tree not found: %s", _VENDOR_ROOT)
        return False
    prefix = str(_VENDOR_ROOT)
    if prefix not in sys.path:
        sys.path.insert(0, prefix)
        log.info("Prepended vendored portablemc: %s", prefix)
    else:
        # Ensure it is actually first (before any site-packages remnant)
        sys.path.remove(prefix)
        sys.path.insert(0, prefix)
    return True


# ── Public API ─────────────────────────────────────────────────────────────

def bootstrap(*, scripts_dir: Path | None = None) -> bool:
    """
    Full setup: extract .pyd if needed, scrub pip portablemc, prepend vendor.

    Call this once at the top of every entry-point before importing portablemc.
    Returns True when the vendored copy is ready (native extension available).
    """
    # Allow callers to pass an explicit scripts_dir (for embedded-Python paths
    # where __file__ might resolve differently).
    global _SCRIPTS_DIR, _VENDOR_ROOT, _WHEEL_SEARCH, _PMC_PKG_DIR
    if scripts_dir is not None:
        sd = Path(scripts_dir).resolve()
        _SCRIPTS_DIR = sd
        _VENDOR_ROOT = sd / "portablemc" / "portablemc-py" / "python"
        _WHEEL_SEARCH = sd / "portablemc" / "target" / "wheels"
        _PMC_PKG_DIR = _VENDOR_ROOT / "portablemc"

    ok = _ensure_pyd()
    _scrub_pip_portablemc()
    _prepend_vendor()

    if not ok:
        log.warning(
            "portablemc native extension (_portablemc.pyd) could not be set up; "
            "only pure-Python fallback (portablemc 4.x CLI) will be available."
        )
    return ok


# Backwards-compatible alias used by older call sites
def prepend_vendor_portablemc(
    *,
    scripts_dir: Path | None = None,
    project_root: Path | None = None,
) -> Path | None:
    """
    Legacy shim — calls ``bootstrap()`` and returns the vendored root path.

    Prefer calling ``bootstrap()`` directly in new code.
    """
    sd = scripts_dir
    if sd is None and project_root is not None:
        sd = Path(project_root).resolve() / "scripts"
    bootstrap(scripts_dir=sd)
    return _VENDOR_ROOT if _VENDOR_ROOT.is_dir() else None


# ══════════════════════════════════════════════════════════════════════════════
# § IN-MEMORY PYD LOADING
# ══════════════════════════════════════════════════════════════════════════════
#
# For environments with strict .exe/.dll execution policies, load the
# _portablemc.cp314-win_amd64.pyd binary directly into memory using
# pythonmemorymodule.  This bypasses normal LoadLibrary and allows the
# extension to be imported without triggering execution policy blocks.
#
# Usage:
#   from local_portablemc import bootstrap_inmemory
#   bootstrap_inmemory()  # Must be called BEFORE any portablemc import
#   import portablemc.mojang  # Now works even with strict policies


def _find_pyd_file() -> Path | None:
    """
    Locate the _portablemc.cp*-win_amd64.pyd file in the vendored tree.
    Returns None if not found.
    """
    if not _PMC_PKG_DIR.is_dir():
        return None
    # Match any Python version's pyd file
    for pyd in _PMC_PKG_DIR.glob("_portablemc*.pyd"):
        return pyd
    return None


def _load_pyd_inmemory(pyd_path: Path) -> types.ModuleType | None:
    """
    Load a .pyd file into memory using pythonmemorymodule.

    Returns the loaded module object, or None on failure.
    This function:
    1. Reads the .pyd binary into memory
    2. Uses pythonmemorymodule.MemoryModule to map it
    3. Creates a module object and initializes it via PyInit_*
    """
    global _PYD_LOADED_INMEMORY, _PYD_MEMORY_MODULE

    if _PYD_LOADED_INMEMORY and _PYD_MEMORY_MODULE is not None:
        log.debug("_portablemc pyd already loaded in-memory")
        return _PYD_MEMORY_MODULE

    try:
        # Ensure pythonmemorymodule is available
        scripts_str = str(_SCRIPTS_DIR)
        if scripts_str not in sys.path:
            sys.path.insert(0, scripts_str)

        from pythonmemorymodule import MemoryModule

        log.info("Loading _portablemc.pyd in-memory from: %s", pyd_path)
        pyd_data = pyd_path.read_bytes()

        # Create a MemoryModule from the pyd bytes
        mem_mod = MemoryModule(data=pyd_data, debug=False)

        # Get the PyInit function address
        # The function name is PyInit_<module_name> for Python 3 extensions
        init_func_name = b"PyInit__portablemc"
        init_func_addr = mem_mod.get_proc_addr("PyInit__portablemc")

        if not init_func_addr:
            log.error("Could not find PyInit__portablemc in pyd")
            return None

        # Create a ctypes function type for PyInit
        import ctypes

        PyInit_func = ctypes.PYFUNCTYPE(ctypes.py_object)
        init_func = PyInit_func(ctypes.cast(init_func_addr, ctypes.c_void_p).value)

        # Call the init function to get the module object
        module = init_func()

        if module is None:
            log.error("PyInit__portablemc returned None")
            return None

        # Register the module in sys.modules
        sys.modules["_portablemc"] = module
        sys.modules["portablemc._portablemc"] = module

        _PYD_LOADED_INMEMORY = True
        _PYD_MEMORY_MODULE = module
        log.info("Successfully loaded _portablemc.pyd in-memory")
        return module

    except ImportError as exc:
        log.warning(
            "pythonmemorymodule not available for in-memory loading: %s", exc
        )
        return None
    except Exception as exc:
        log.error("Failed to load _portablemc.pyd in-memory: %s", exc)
        return None


def _create_portablemc_package() -> bool:
    """
    Create the portablemc package structure in sys.modules if not present.
    This ensures submodules like portablemc.mojang can be imported.
    """
    if "portablemc" not in sys.modules:
        # Create the portablemc package module
        pkg = types.ModuleType("portablemc")
        pkg.__path__ = [str(_PMC_PKG_DIR)]
        pkg.__file__ = str(_PMC_PKG_DIR / "__init__.py")
        pkg.__package__ = "portablemc"
        sys.modules["portablemc"] = pkg
        log.debug("Created portablemc package in sys.modules")
    return True


def _setup_portablemc_submodule_stubs() -> None:
    """
    Create stub modules for portablemc submodules that forward to the
    native extension's submodules.

    portablemc 5.x exposes:
      - portablemc.mojang
      - portablemc.fabric
      - portablemc.forge
      - portablemc.msa

    These are implemented as Rust code compiled into _portablemc.pyd
    and exposed via PyO3.
    """
    if "_portablemc" not in sys.modules:
        log.warning("Cannot create submodule stubs: _portablemc not loaded")
        return

    native_mod = sys.modules["_portablemc"]
    pkg = sys.modules.get("portablemc")

    if pkg is None:
        _create_portablemc_package()
        pkg = sys.modules["portablemc"]

    # List of submodules exposed by the native extension
    submodules = ["mojang", "fabric", "forge", "msa", "base"]

    for submod_name in submodules:
        full_name = f"portablemc.{submod_name}"
        if full_name in sys.modules:
            continue

        # Try to get the submodule from the native extension
        submod = getattr(native_mod, submod_name, None)
        if submod is not None:
            sys.modules[full_name] = submod
            setattr(pkg, submod_name, submod)
            log.debug("Registered submodule: %s", full_name)
        else:
            log.debug("Submodule %s not found in _portablemc", submod_name)


def bootstrap_inmemory(*, scripts_dir: Path | None = None) -> bool:
    """
    Full in-memory bootstrap: load _portablemc.pyd via pythonmemorymodule.

    This bypasses normal LoadLibrary calls and allows the extension to work
    in environments with strict .exe/.dll execution policies.

    Call this ONCE at the top of every entry-point before any portablemc import.
    Returns True when the extension is ready.

    Usage:
        from local_portablemc import bootstrap_inmemory
        if bootstrap_inmemory():
            import portablemc.mojang
            # ... use portablemc 5.x API
    """
    global _SCRIPTS_DIR, _VENDOR_ROOT, _WHEEL_SEARCH, _PMC_PKG_DIR

    # Update paths if scripts_dir is provided
    if scripts_dir is not None:
        sd = Path(scripts_dir).resolve()
        _SCRIPTS_DIR = sd
        _VENDOR_ROOT = sd / "portablemc" / "portablemc-py" / "python"
        _WHEEL_SEARCH = sd / "portablemc" / "target" / "wheels"
        _PMC_PKG_DIR = _VENDOR_ROOT / "portablemc"

    # Ensure scripts dir is on path for pythonmemorymodule
    scripts_str = str(_SCRIPTS_DIR)
    if scripts_str not in sys.path:
        sys.path.insert(0, scripts_str)

    # Step 1: Extract pyd from wheel if needed
    _ensure_pyd()

    # Step 2: Scrub pip-installed portablemc to avoid conflicts
    _scrub_pip_portablemc()

    # Step 3: Prepend vendor root for pure-Python fallback modules
    _prepend_vendor()

    # Step 4: Find and load the pyd in-memory
    pyd_path = _find_pyd_file()
    if pyd_path is None:
        log.warning("_portablemc.pyd not found in vendored tree")
        return False

    module = _load_pyd_inmemory(pyd_path)
    if module is None:
        return False

    # Step 5: Create package structure and submodule stubs
    _create_portablemc_package()
    _setup_portablemc_submodule_stubs()

    log.info("In-memory portablemc 5.x bootstrap complete")
    return True


def is_pyd_loaded_inmemory() -> bool:
    """Check if the _portablemc.pyd is loaded in-memory."""
    return _PYD_LOADED_INMEMORY


def get_inmemory_module() -> object | None:
    """Get the in-memory loaded _portablemc module, or None."""
    return _PYD_MEMORY_MODULE
