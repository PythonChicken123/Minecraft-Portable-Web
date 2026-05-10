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

Call ``bootstrap()`` (or ``prepend_vendor_portablemc()``) from every entry
point before any ``import portablemc`` statement.
"""

from __future__ import annotations

import logging
import sys
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

# ── Canonical paths ────────────────────────────────────────────────────────

_SCRIPTS_DIR = Path(__file__).resolve().parent
_VENDOR_ROOT = _SCRIPTS_DIR / "portablemc" / "portablemc-py" / "python"
_WHEEL_SEARCH = _SCRIPTS_DIR / "portablemc" / "target" / "wheels"
_PMC_PKG_DIR = _VENDOR_ROOT / "portablemc"


# ── Wheel extraction ───────────────────────────────────────────────────────

def _find_wheel() -> Path | None:
    """Return the first portablemc wheel found in the target/wheels dir."""
    if not _WHEEL_SEARCH.is_dir():
        return None
    for whl in sorted(_WHEEL_SEARCH.glob("portablemc-*.whl")):
        return whl
    return None


def _pyd_present() -> bool:
    """True when the native extension already lives in the vendored tree."""
    if not _PMC_PKG_DIR.is_dir():
        return False
    return any(_PMC_PKG_DIR.glob("_portablemc*.pyd"))


def _extract_pyd_from_wheel(wheel: Path) -> bool:
    """
    Extract every file from the wheel's ``portablemc/`` directory into the
    vendored Python tree.  Skips dist-info and __pycache__ entries.

    Returns True on success.
    """
    try:
        _PMC_PKG_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(wheel, "r") as zf:
            extracted = 0
            for name in zf.namelist():
                if not name.startswith("portablemc/"):
                    continue
                if "__pycache__" in name or name.endswith("/"):
                    continue
                # Strip the leading "portablemc/" prefix; write relative to _PMC_PKG_DIR
                rel = name[len("portablemc/"):]
                if not rel:
                    continue
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
