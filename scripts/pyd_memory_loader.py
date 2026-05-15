"""
pyd_memory_loader.py
════════════════════
Utility module for loading Python extension modules (.pyd files) directly
into memory using pythonmemorymodule, bypassing the normal LoadLibrary path.

This is useful for environments with strict .exe/.dll execution policies
where the OS loader would block the extension from loading.

Usage
─────
    from pyd_memory_loader import load_pyd_inmemory, PydMemoryLoader

    # Simple one-shot loading
    module = load_pyd_inmemory("/path/to/_mymodule.cp314-win_amd64.pyd")
    if module:
        import sys
        sys.modules["_mymodule"] = module

    # Or use the loader class for more control
    loader = PydMemoryLoader("/path/to/_mymodule.cp314-win_amd64.pyd")
    if loader.load():
        loader.register_module("_mymodule")
        loader.register_module("mypackage._mymodule")

Technical Details
─────────────────
1. Reads the .pyd binary into a bytes buffer
2. Maps it into memory using pythonmemorymodule.MemoryModule
3. Locates the PyInit_<name> function in the module's export table
4. Calls PyInit_<name> to initialize the module and get the module object
5. Registers the module in sys.modules under the specified name(s)

The key insight is that Python extension modules are just regular DLLs with
a PyInit_<name> export that returns a PyObject* to the module.  By mapping
the DLL into memory manually and calling this function, we can load the
extension without going through the OS loader.

Constraints
───────────
- Windows only (pythonmemorymodule uses Win32 APIs)
- x64 only (current implementation)
- The .pyd must be compatible with the running Python version
- TLS callbacks and complex DllMain logic may not work correctly
"""

from __future__ import annotations

import ctypes
import logging
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pythonmemorymodule import MemoryModule

log = logging.getLogger(__name__)

# Track loaded modules to prevent double-loading
_LOADED_PYDS: dict[str, "PydMemoryLoader"] = {}


class PydMemoryLoader:
    """
    Load a .pyd file into memory and expose it as a Python module.

    Example
    ───────
        loader = PydMemoryLoader("path/to/_portablemc.cp314-win_amd64.pyd")
        if loader.load():
            # Register under the expected import name
            loader.register_module("_portablemc")
            loader.register_module("portablemc._portablemc")

            # Now imports will work
            import _portablemc
    """

    def __init__(self, pyd_path: str | Path) -> None:
        """
        Initialize the loader with a path to a .pyd file.

        Parameters
        ----------
        pyd_path : str | Path
            Absolute or relative path to the .pyd file to load.
        """
        self.pyd_path = Path(pyd_path).resolve()
        self._memory_module: "MemoryModule | None" = None
        self._module: types.ModuleType | None = None
        self._init_func_name: str = ""
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        """Check if the .pyd has been successfully loaded."""
        return self._loaded

    @property
    def module(self) -> types.ModuleType | None:
        """Get the loaded Python module object, or None if not loaded."""
        return self._module

    def _infer_module_name(self) -> str:
        """
        Infer the module name from the .pyd filename.

        Standard naming: _modulename.cp314-win_amd64.pyd
        Returns: _modulename
        """
        name = self.pyd_path.stem  # e.g., "_portablemc.cp314-win_amd64"
        # Strip version/platform suffix
        if "." in name:
            name = name.split(".")[0]
        return name

    def load(self) -> bool:
        """
        Load the .pyd into memory and initialize it.

        Returns True on success, False on failure.
        """
        if self._loaded:
            log.debug("PYD already loaded: %s", self.pyd_path)
            return True

        if not self.pyd_path.exists():
            log.error("PYD file not found: %s", self.pyd_path)
            return False

        try:
            # Import pythonmemorymodule
            from pythonmemorymodule import MemoryModule

            log.info("Loading PYD in-memory: %s", self.pyd_path)

            # Read the .pyd binary
            pyd_data = self.pyd_path.read_bytes()
            log.debug("Read %d bytes from %s", len(pyd_data), self.pyd_path)

            # Create a MemoryModule from the bytes
            self._memory_module = MemoryModule(data=pyd_data, debug=False)

            # Infer the module name and init function
            module_name = self._infer_module_name()
            self._init_func_name = f"PyInit_{module_name}"

            log.debug("Looking for init function: %s", self._init_func_name)

            # Get the PyInit function address
            init_func_addr = self._memory_module.get_proc_addr(self._init_func_name)

            if not init_func_addr:
                log.error(
                    "Could not find %s in %s", self._init_func_name, self.pyd_path
                )
                return False

            # Create a ctypes function type for PyInit
            # PyInit_* functions return PyObject* (which ctypes maps to py_object)
            PyInit_func = ctypes.PYFUNCTYPE(ctypes.py_object)
            raw_addr = ctypes.cast(init_func_addr, ctypes.c_void_p).value

            if not raw_addr:
                log.error("Init function address is NULL")
                return False

            init_func = PyInit_func(raw_addr)

            # Call the init function to get the module object
            log.debug("Calling %s @ 0x%016x", self._init_func_name, raw_addr)
            self._module = init_func()

            if self._module is None:
                log.error("%s returned None", self._init_func_name)
                return False

            self._loaded = True
            _LOADED_PYDS[str(self.pyd_path)] = self
            log.info(
                "Successfully loaded PYD in-memory: %s -> %s",
                self.pyd_path.name,
                type(self._module).__name__,
            )
            return True

        except ImportError as exc:
            log.error("pythonmemorymodule not available: %s", exc)
            return False
        except OSError as exc:
            log.error("OS error loading PYD: %s", exc)
            return False
        except Exception as exc:
            log.error("Failed to load PYD in-memory: %s: %s", type(exc).__name__, exc)
            return False

    def register_module(self, name: str) -> bool:
        """
        Register the loaded module in sys.modules under the given name.

        Parameters
        ----------
        name : str
            The import name to register, e.g., "_portablemc" or
            "portablemc._portablemc".

        Returns True on success.
        """
        if not self._loaded or self._module is None:
            log.warning("Cannot register module %r: not loaded", name)
            return False

        if name in sys.modules:
            log.debug("Module %r already in sys.modules, overwriting", name)

        sys.modules[name] = self._module
        log.debug("Registered module: %s", name)
        return True

    def get_submodule(self, submod_name: str) -> types.ModuleType | None:
        """
        Get a submodule from the loaded extension module.

        Some PyO3 extensions expose multiple submodules (e.g., portablemc.mojang,
        portablemc.fabric).  This method retrieves them by attribute access.

        Parameters
        ----------
        submod_name : str
            The submodule name (without the parent package prefix).

        Returns the submodule object, or None if not found.
        """
        if not self._loaded or self._module is None:
            return None
        return getattr(self._module, submod_name, None)

    def register_submodules(self, parent_name: str, submod_names: list[str]) -> int:
        """
        Register submodules exposed by the extension into sys.modules.

        Parameters
        ----------
        parent_name : str
            The parent package name, e.g., "portablemc".
        submod_names : list[str]
            Names of submodules to register, e.g., ["mojang", "fabric", "forge"].

        Returns the number of submodules successfully registered.
        """
        count = 0
        for name in submod_names:
            submod = self.get_submodule(name)
            if submod is not None:
                full_name = f"{parent_name}.{name}"
                sys.modules[full_name] = submod
                log.debug("Registered submodule: %s", full_name)
                count += 1
            else:
                log.debug("Submodule %s not found in extension", name)
        return count


def load_pyd_inmemory(pyd_path: str | Path) -> types.ModuleType | None:
    """
    Convenience function to load a .pyd file in-memory.

    Parameters
    ----------
    pyd_path : str | Path
        Path to the .pyd file.

    Returns the loaded module, or None on failure.
    """
    # Check cache first
    key = str(Path(pyd_path).resolve())
    if key in _LOADED_PYDS:
        return _LOADED_PYDS[key].module

    loader = PydMemoryLoader(pyd_path)
    if loader.load():
        return loader.module
    return None


def is_pyd_loaded(pyd_path: str | Path) -> bool:
    """Check if a .pyd file has already been loaded in-memory."""
    key = str(Path(pyd_path).resolve())
    return key in _LOADED_PYDS


def get_loaded_pyd(pyd_path: str | Path) -> PydMemoryLoader | None:
    """Get the loader for an already-loaded .pyd file."""
    key = str(Path(pyd_path).resolve())
    return _LOADED_PYDS.get(key)
