"""
jvm_memory_loader.py
════════════════════
Memory-resident JVM loader for the in-process Minecraft launcher.

What this module does
─────────────────────
Maps jvm.dll and its JDK dependencies into VirtualAlloc'd memory using
PythonMemoryModule, then resolves the three JNI entry-points (JNI_CreateJavaVM,
JNI_GetDefaultJavaVMInitArgs, JNI_GetCreatedJavaVMs) as typed ctypes callables.

Two fundamental problems solved
────────────────────────────────
Problem 1 — GetProcAddress blind spot
  PythonMemoryModule's build_import_table() calls kernel32.GetProcAddress(hmod, name)
  to resolve inter-DLL imports.  That Win32 function only works for handles
  registered in the OS loader table — which our VirtualAlloc modules are not.
  Fix: monkey-patch pythonmemorymodule.getprocaddr to route lookups for our
  in-memory handles through MemoryModule's own PE export walker.

Problem 2 — jvm.dll DllMain is process-sensitive
  HotSpot's DllMain(DLL_PROCESS_ATTACH) records vm_lib_handle, but executing it
  from a manual mapper can be unsafe after a large Python bootstrap stack has
  already loaded many native extensions.  We therefore skip jvm.dll's DllMain
  by default and let jvm_boot_glue.py pin _ALT_JAVA_HOME_DIR so HotSpot can set
  java.home without calling os::jvm_path().  For diagnostics, set
  LAUNCHER_RUN_JVM_DLLMAIN=1 to run it synchronously.

Problem 3 — Async DllMain races
  PythonMemoryModule fires DllMain in a daemon thread.  If a dependency's
  DllMain hasn't finished before the next DLL tries to import it, symbol
  resolution races.
  Fix: _synchronous_threads() context manager makes Thread.start() block
  until the target completes, serialising all DllMain calls.

Load order
──────────
DFS post-order walk of jvm.dll's import graph via pefile ensures leaf DLLs
(verify.dll, jli.dll) are loaded before their consumers.  System / CRT DLLs
fall through to LoadLibraryW — they're already present in the Python process.

Integration with jvm_boot_glue.py
───────────────────────────────────
After load(), jvm_codebase() returns the VirtualAlloc base of jvm.dll.
jvm_boot_glue patches _jpype.pyd's IAT so that when jpype.startJVM() calls
LoadLibraryW('jvm.dll') it receives this base as a fake HMODULE.  Subsequent
GetProcAddress calls on that fake handle are also intercepted and routed to
our in-memory export table.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import logging
import os
import threading
from pathlib import Path
from typing import Callable, Iterator

# ── PythonMemoryModule surface ─────────────────────────────────────────────
import pythonmemorymodule as _pmm
import pythonmemorymodule.pefile as _pefile
from pythonmemorymodule import LoadLibraryW, MemoryModule

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# § 1  JNI function-type aliases
#      Typed ctypes callables for the three JNI entry-points exported by jvm.dll
# ══════════════════════════════════════════════════════════════════════════════

JNI_CreateJavaVM_t = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),  # JavaVM **pvm
    ctypes.POINTER(ctypes.c_void_p),  # JNIEnv **penv
    ctypes.c_void_p,  # JavaVMInitArgs *args
)

JNI_GetDefaultJavaVMInitArgs_t = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,  # void *args
)

JNI_GetCreatedJavaVMs_t = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),  # JavaVM **vmBuf
    ctypes.c_int,  # jsize bufLen
    ctypes.POINTER(ctypes.c_int),  # jsize *nVMs
)


# ══════════════════════════════════════════════════════════════════════════════
# § 2  JDK DLL classification
#      Which DLLs we own (load from memory) vs. which the OS handles
# ══════════════════════════════════════════════════════════════════════════════

# DLLs we load from memory via PythonMemoryModule.
# Everything else (CRT, Win32, api-ms-win-*) is already in the process.
_JDK_OWNED: frozenset[str] = frozenset(
    {
        "verify.dll",
        "jli.dll",
        "zip.dll",
        "java.dll",
        "jimage.dll",
        # NOTE: jsvml.dll (optional SIMD library) intentionally NOT memory-mapped.
        # HotSpot may load it opportunistically; letting the OS loader handle it
        # avoids extra manual-mapper surface area during early JVM bootstrap.
        "jvm.dll",
    }
)

# System DLLs that must always fall through to the OS loader.
_ALWAYS_OS: frozenset[str] = frozenset(
    {
        "kernel32.dll",
        "ntdll.dll",
        "user32.dll",
        "advapi32.dll",
        "ws2_32.dll",
        "psapi.dll",
        "dbghelp.dll",
        "ucrtbase.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
        "msvcp140_1.dll",
        "msvcp140_2.dll",
    }
)


def _is_jdk_dll(name: str) -> bool:
    return name.lower() in _JDK_OWNED


def _is_api_set(name: str) -> bool:
    """api-ms-win-* are OS forwarder stubs — always use the OS loader."""
    return name.lower().startswith("api-ms-win-")


# ══════════════════════════════════════════════════════════════════════════════
# § 3  Synchronous DllMain helper
#      Serialises PythonMemoryModule's async thread so each DllMain completes
#      before the next dependency is loaded.
# ══════════════════════════════════════════════════════════════════════════════


@contextlib.contextmanager
def _synchronous_threads() -> Iterator[None]:
    """
    Temporarily make threading.Thread.start() synchronous (blocks until done).

    PythonMemoryModule runs DllMain in a daemon thread.  Without this, a
    dependency's DllMain might not finish before the next DLL is loaded and
    tries to import symbols from it.
    """
    _real_start = threading.Thread.start

    def _blocking_start(self: threading.Thread, *args, **kwargs) -> None:  # type: ignore[override]
        _real_start(self, *args, **kwargs)
        self.join()

    threading.Thread.start = _blocking_start  # type: ignore[method-assign]
    try:
        yield
    finally:
        threading.Thread.start = _real_start  # type: ignore[method-assign]


# ══════════════════════════════════════════════════════════════════════════════
# § 4  JdkMemoryModule
#      MemoryModule subclass with behaviours tailored for JDK DLLs:
#        (a) custom dlopen resolver — routes inter-JDK imports through our registry
#        (b) synchronous DllMain — entry point runs before __init__ returns
#        (c) optional jvm.dll entry skip — avoids process-sensitive crashes
# ══════════════════════════════════════════════════════════════════════════════


class JdkMemoryModule(MemoryModule):
    """
    MemoryModule variant for JDK DLLs.

    Parameters
    ----------
    data
        Raw DLL bytes to map into memory.
    dlopen_fn
        Callable(dll_name: str) → HMODULE-like int.  Passed to
        build_import_table() so inter-JDK imports resolve through our registry
        instead of the OS loader.
    dll_name
        Human-readable name (e.g. ``"jvm.dll"``).  Stored for diagnostics.
    debug
        Verbose PythonMemoryModule debug output.
    """

    # Skip jvm.dll's DllMain by default.  The bootstrap pins
    # _ALT_JAVA_HOME_DIR so HotSpot does not need vm_lib_handle to derive
    # java.home, and skipping the entry point avoids process-sensitive crashes
    # seen after main.py has loaded its own native bootstrap stack.
    SKIP_ENTRY_POINT: frozenset[str] = frozenset({"jvm.dll"})

    def __init__(
        self,
        data: bytes,
        dlopen_fn: Callable[[str], int],
        dll_name: str = "",
        debug: bool = False,
    ) -> None:
        # Store both before super().__init__ because load_module() →
        # build_import_table() is called inside the parent constructor.
        self._dlopen_fn: Callable[[str], int] = dlopen_fn
        self._dll_name_lower: str = dll_name.lower()

        # Serialise DllMain so it finishes before the next DLL is loaded.
        with _synchronous_threads():
            super().__init__(data=data, debug=debug, command=None)

    def build_import_table(self, dlopen: Callable[[str], int] | None = None) -> None:  # type: ignore[override]
        """Route all import resolution through our custom dlopen_fn."""
        super().build_import_table(dlopen=self._dlopen_fn)

    def execPE(self) -> None:
        """
        Run DLL_PROCESS_ATTACH.

        Suppressed for DLLs listed in SKIP_ENTRY_POINT unless explicitly
        overridden with LAUNCHER_RUN_JVM_DLLMAIN=1 for diagnostics.
        """
        run_jvm_dllmain = os.environ.get(
            "LAUNCHER_RUN_JVM_DLLMAIN", ""
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if self._dll_name_lower in self.SKIP_ENTRY_POINT and not run_jvm_dllmain:
            log.debug(
                "Skipping DllMain for %-20s  (java.home pinned via _ALT_JAVA_HOME_DIR)",
                self._dll_name_lower,
            )
            return
        super().execPE()


# ══════════════════════════════════════════════════════════════════════════════
# § 5  GetProcAddress patch
#      pythonmemorymodule.getprocaddr delegates to kernel32.GetProcAddress,
#      which only works for OS-registered handles.  We intercept it and route
#      lookups for our VirtualAlloc-based modules through MemoryModule's own
#      PE export walker.
# ══════════════════════════════════════════════════════════════════════════════

# Module-level state for the getprocaddr patch.
_mem_registry: dict[int, JdkMemoryModule] = {}  # codebaseaddr → module
_pmm_patched: bool = False
_original_getprocaddr = _pmm.getprocaddr


def _patched_getprocaddr(handle: int, func_name_bytes: bytes) -> int:
    mod = _mem_registry.get(handle)
    if mod is not None:
        func_name = func_name_bytes.decode("ascii", errors="replace")
        try:
            # Use public helper so export directory is initialised lazily.
            fp = mod.get_proc_addr(func_name)
            return ctypes.cast(fp, ctypes.c_void_p).value or 0
        except WindowsError:
            log.debug(
                "getprocaddr: %r not found in in-memory module @ 0x%x",
                func_name,
                handle,
            )
            return 0
    return _original_getprocaddr(handle, func_name_bytes)


def _install_getprocaddr_patch() -> None:
    global _pmm_patched
    if _pmm_patched:
        return
    _pmm.getprocaddr = _patched_getprocaddr
    _pmm_patched = True
    log.debug("pythonmemorymodule.getprocaddr patch installed")


def _uninstall_getprocaddr_patch() -> None:
    global _pmm_patched
    _pmm.getprocaddr = _original_getprocaddr
    _pmm_patched = False
    log.debug("pythonmemorymodule.getprocaddr patch removed")


# ══════════════════════════════════════════════════════════════════════════════
# § 6  JvmMemoryLoader
#      Top-level orchestrator: dependency graph → load order → map → verify
# ══════════════════════════════════════════════════════════════════════════════


class JvmMemoryLoader:
    """
    Maps jvm.dll and all JDK dependencies into process memory.

    Quick start
    -----------
    ::

        loader = JvmMemoryLoader(jdk_bin=Path(r"...\\jdk-25.0.3+9\\bin")).load()
        assert loader.verify()
        fn = loader.jni_create_java_vm      # typed ctypes callable

    Directory layout expected
    -------------------------
    ``<jdk_bin>/``             — verify.dll, jli.dll, zip.dll, java.dll, jimage.dll
    ``<jdk_bin>/server/``      — jvm.dll

    System / CRT DLLs (kernel32, ucrtbase, vcruntime140, api-ms-win-*, …)
    are already present in the Python process and are not mapped from memory.
    """

    def __init__(self, jdk_bin: Path, debug: bool = False) -> None:
        self.jdk_bin: Path = jdk_bin
        self.jdk_server: Path = jdk_bin / "server"
        self.debug: bool = debug

        # Loaded modules: lowercase dll name → JdkMemoryModule
        self._mods: dict[str, JdkMemoryModule] = {}

        # Typed JNI entry-points (populated by load())
        self._jni_create_java_vm: JNI_CreateJavaVM_t | None = None
        self._jni_get_default_init_args: JNI_GetDefaultJavaVMInitArgs_t | None = None
        self._jni_get_created_jvms: JNI_GetCreatedJavaVMs_t | None = None

    @staticmethod
    def resolve_jvm_dll_file(jdk_bin: Path) -> Path:
        """
        Canonical on-disk ``jvm.dll`` under ``jdk_bin``.

        Mojang runtimes normally use ``bin/server/jvm.dll``; legacy layouts
        may place ``jvm.dll`` directly in ``bin/``.
        """
        server_path = jdk_bin / "server" / "jvm.dll"
        if server_path.is_file():
            return server_path
        flat = jdk_bin / "jvm.dll"
        return flat if flat.is_file() else server_path

    @property
    def jvm_dll_path(self) -> Path:
        """Absolute-path ``jvm.dll`` used for JPype/IAT spoofing."""
        return self.resolve_jvm_dll_file(self.jdk_bin)

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    def _find(self, name: str) -> Path | None:
        """Locate a JDK DLL on disk (server/ first, then bin/)."""
        for d in (self.jdk_server, self.jdk_bin):
            p = d / name
            if p.exists():
                return p
        return None

    def _make_dlopen(self) -> Callable[[str], int]:
        """
        Build the dlopen callable passed to JdkMemoryModule.build_import_table().

        Routing rules
        ─────────────
        • DLL already in our registry       → return its codebaseaddr
        • api-ms-win-* or _ALWAYS_OS DLLs  → LoadLibraryW (OS handles it)
        • Any other JDK DLL                → load it recursively first
        """
        mods = self._mods

        def dlopen(raw_name: str) -> int:
            name = raw_name.lower() if raw_name else ""

            if name in mods:
                addr = mods[name]._codebaseaddr
                log.debug("dlopen(%-22s) → in-memory 0x%x", name, addr)
                return addr

            if _is_api_set(name) or name in _ALWAYS_OS or not _is_jdk_dll(name):
                hmod = LoadLibraryW(raw_name)
                if not hmod:
                    raise OSError(f"LoadLibraryW failed for system DLL: {raw_name!r}")
                log.debug("dlopen(%-22s) → OS 0x%x", name, hmod)
                return hmod

            log.debug("dlopen(%-22s) → recursive load", name)
            self._load_one(name)
            return mods[name]._codebaseaddr

        return dlopen

    def _collect_deps(self, dll_name: str, visited: set[str], order: list[str]) -> None:
        """
        DFS post-order walk of the import graph.

        Produces a load order where every dependency appears before its consumer.
        System DLLs are excluded from the walk — they are not loaded from memory.
        """
        name = dll_name.lower()
        if name in visited:
            return
        visited.add(name)

        if not _is_jdk_dll(name):
            return

        path = self._find(name)
        if path is None:
            log.debug("collect_deps: %s not found, skipping", name)
            return

        try:
            pe = _pefile.PE(str(path), fast_load=False)
            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
                dep = entry.dll.decode("utf-8", errors="replace")
                self._collect_deps(dep, visited, order)
            pe.close()
        except Exception as exc:
            log.warning("pefile parse failed for %s: %s", name, exc)

        order.append(name)  # post-order: leaf first

    def _load_one(self, dll_name: str) -> None:
        """Map a single JDK DLL into memory and register it."""
        name = dll_name.lower()
        if name in self._mods:
            return

        path = self._find(name)
        if path is None:
            log.warning("_load_one: %s not found — skipping", name)
            return

        data = path.read_bytes()
        log.info("Loading %-22s  (%7d bytes)  %s", name, len(data), path)

        mod = JdkMemoryModule(
            data=data,
            dlopen_fn=self._make_dlopen(),
            dll_name=name,
            debug=self.debug,
        )

        # Register by codebaseaddr first so recursive getprocaddr lookups
        # during this module's own init can already find it.
        _mem_registry[mod._codebaseaddr] = mod
        self._mods[name] = mod

        log.info(
            "  ↳ %-22s  base=0x%016x  size=0x%x",
            name,
            mod._codebaseaddr,
            mod.OPTIONAL_HEADER.SizeOfImage,
        )

    def _resolve_jni(self, export_name: str, ftype: type):
        """Extract a typed JNI function pointer from jvm.dll's export table."""
        jvm_mod = self._mods.get("jvm.dll")
        if jvm_mod is None:
            raise RuntimeError("jvm.dll has not been loaded yet")
        farproc = jvm_mod.get_proc_addr(export_name)
        raw_addr = ctypes.cast(farproc, ctypes.c_void_p).value
        if not raw_addr:
            raise RuntimeError(f"{export_name!r} resolved to NULL")
        return ftype(raw_addr)

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def load(self) -> "JvmMemoryLoader":
        """
        Full load sequence.

        1. Install the getprocaddr patch so inter-JDK symbols resolve correctly.
        2. Walk jvm.dll's import graph to compute a safe load order.
        3. Map each JDK DLL into memory (leaves first).
        4. Extract the three JNI entry-point callables from jvm.dll.
        """
        _install_getprocaddr_patch()

        visited: set[str] = set()
        order: list[str] = []

        # jvm.dll dynamically loads several sibling JDK DLLs at runtime
        # (notably jimage.dll). They may not appear in jvm.dll's import table,
        # so we proactively map them as well to keep LoadLibrary* hooks in
        # jvm_boot_glue able to satisfy those loads from memory.
        eager = [
            "verify.dll",
            "jli.dll",
            "zip.dll",
            "java.dll",
            "jimage.dll",
            "jvm.dll",
        ]
        for root in eager:
            self._collect_deps(root, visited, order)

        log.info("Load order (%d JDK DLLs):", len(order))
        for i, name in enumerate(order, 1):
            log.info("  %2d. %s", i, name)

        for name in order:
            self._load_one(name)

        self._jni_create_java_vm = self._resolve_jni(  # type: ignore[assignment]
            "JNI_CreateJavaVM", JNI_CreateJavaVM_t
        )
        self._jni_get_default_init_args = self._resolve_jni(  # type: ignore[assignment]
            "JNI_GetDefaultJavaVMInitArgs", JNI_GetDefaultJavaVMInitArgs_t
        )
        self._jni_get_created_jvms = self._resolve_jni(  # type: ignore[assignment]
            "JNI_GetCreatedJavaVMs", JNI_GetCreatedJavaVMs_t
        )

        log.info(
            "JNI_CreateJavaVM         @ 0x%016x",
            ctypes.cast(self._jni_create_java_vm, ctypes.c_void_p).value,
        )
        log.info(
            "JNI_GetDefaultJavaVMInitArgs @ 0x%016x",
            ctypes.cast(self._jni_get_default_init_args, ctypes.c_void_p).value,
        )
        log.info(
            "JNI_GetCreatedJavaVMs    @ 0x%016x",
            ctypes.cast(self._jni_get_created_jvms, ctypes.c_void_p).value,
        )

        return self

    def verify(self) -> bool:
        """
        Confirm JNI_CreateJavaVM resolved to a non-NULL address inside
        our mapped jvm.dll region.  Returns True on success.
        """
        fn = self._jni_create_java_vm
        if fn is None:
            log.error("verify: JNI_CreateJavaVM was never resolved")
            return False

        addr = ctypes.cast(fn, ctypes.c_void_p).value
        if not addr:
            log.error("verify: JNI_CreateJavaVM is NULL")
            return False

        jvm_mod = self._mods.get("jvm.dll")
        if jvm_mod is None:
            log.error("verify: jvm.dll not in module registry")
            return False

        base = jvm_mod._codebaseaddr
        size = jvm_mod.OPTIONAL_HEADER.SizeOfImage
        ok = base <= addr < base + size

        if ok:
            log.info(
                "verify OK — JNI_CreateJavaVM @ 0x%016x inside [0x%016x, 0x%016x)",
                addr,
                base,
                base + size,
            )
        else:
            log.error(
                "verify FAIL — JNI_CreateJavaVM @ 0x%016x OUTSIDE [0x%016x, 0x%016x)",
                addr,
                base,
                base + size,
            )
        return ok

    # ── Typed JNI properties ───────────────────────────────────────────────

    @property
    def jni_create_java_vm(self) -> JNI_CreateJavaVM_t:
        if self._jni_create_java_vm is None:
            raise RuntimeError("Call load() first")
        return self._jni_create_java_vm

    @property
    def jni_get_default_init_args(self) -> JNI_GetDefaultJavaVMInitArgs_t:
        if self._jni_get_default_init_args is None:
            raise RuntimeError("Call load() first")
        return self._jni_get_default_init_args

    @property
    def jni_get_created_jvms(self) -> JNI_GetCreatedJavaVMs_t:
        if self._jni_get_created_jvms is None:
            raise RuntimeError("Call load() first")
        return self._jni_get_created_jvms

    @property
    def jvm_module(self) -> JdkMemoryModule:
        """Direct access to the mapped jvm.dll MemoryModule."""
        return self._mods["jvm.dll"]

    @property
    def loaded_modules(self) -> dict[str, JdkMemoryModule]:
        """Read-only snapshot of all loaded JDK modules (name → module)."""
        return dict(self._mods)

    def jvm_codebase(self) -> int:
        """
        VirtualAlloc base of the in-memory jvm.dll.

        jvm_boot_glue uses this as the fake HMODULE returned by the
        LoadLibraryW IAT hook so that jpype.startJVM() receives a handle
        whose GetProcAddress calls we can intercept.
        """
        return self._mods["jvm.dll"]._codebaseaddr

    def __repr__(self) -> str:
        return f"<JvmMemoryLoader mods={list(self._mods)}>"


# ══════════════════════════════════════════════════════════════════════════════
# § 7  Standalone verification entry-point
# ══════════════════════════════════════════════════════════════════════════════


def run_verification(jdk_bin: Path, debug: bool = False) -> bool:
    """
    Smoke-test: load jvm.dll from memory and verify JNI_CreateJavaVM resolves.
    Returns True on success.
    """
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)-8s %(message)s",
    )
    w = 60
    print(f"\n{'═' * w}")
    print("  JVM Memory Loader — Verification")
    print(f"{'═' * w}")
    print(f"  JDK bin : {jdk_bin}")
    print(f"  jvm.dll : {jdk_bin / 'server' / 'jvm.dll'}")
    print(f"{'─' * w}\n")

    try:
        loader = JvmMemoryLoader(jdk_bin=jdk_bin, debug=debug).load()
    except Exception as exc:
        print(f"  [FAIL] load() raised: {exc}")
        return False

    ok = loader.verify()

    print(f"\n{'─' * w}")
    print("  Loaded modules:")
    for name, mod in loader.loaded_modules.items():
        print(
            f"    {name:<24s}  base=0x{mod._codebaseaddr:016x}  "
            f"size=0x{mod.OPTIONAL_HEADER.SizeOfImage:08x}"
        )

    print(f"\n{'─' * w}")
    if ok:
        addr = ctypes.cast(loader.jni_create_java_vm, ctypes.c_void_p).value
        print(f"  [PASS] JNI_CreateJavaVM @ 0x{addr:016x}")
        print("  [PASS] Address confirmed inside jvm.dll mapped region")
    else:
        print("  [FAIL] JNI_CreateJavaVM verification failed")
    print(f"{'═' * w}\n")
    return ok


if __name__ == "__main__":
    _JDK_BIN = Path(
        r"C:\Users\wave6\Downloads"
        r"\OpenJDK25U-jdk_x64_windows_hotspot_25.0.3_9"
        r"\jdk-25.0.3+9\bin"
    )
    raise SystemExit(0 if run_verification(_JDK_BIN, debug=False) else 1)
