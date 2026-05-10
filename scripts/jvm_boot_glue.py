"""
jvm_boot_glue.py
════════════════
Bridges the memory-resident JVM into JPype and launches Minecraft in-process.

Boot sequence
─────────────
Option 4 (OS-loader JVM)
    jpype.startJVM(jvm_dll_path, ...)  ← normal OS LoadLibrary, no IAT tricks

Option 5 (memory-resident JVM)
  Stage 1 · IAT Hook
    Patch _jpype.pyd's Import Address Table:
      LoadLibraryW / LoadLibraryExW / LoadLibraryA (when present)
          → fake HINSTANCE (our VirtualAlloc base) for ``jvm.dll``
      GetModuleHandleW (optional) → fake base for ``jvm.dll`` name probes
      GetProcAddress → dispatch table for JNI entry-points
  Stage 2 · Transparent JVM Creation
    jpype.startJVM() → C++ calls LoadLibraryW (hooked) → GetProcAddress (hooked)
    → trampoline(pvm, penv, jniArgs) → in-memory JNI_CreateJavaVM → JVM boots
  Stage 3 · Game Launch
    IAT restored.  jpype.JClass(main_class).main(game_args[:])

portablemc adapter — resolution order
──────────────────────────────────────
  1. portablemc 5.x PyO3 Python API  (fastest, requires _portablemc.pyd)
  2. pmc_intercept.py subprocess shim (intercepts Popen; works with any version)
  3. --dry CLI parsing                (multi-format; last resort)

JPype 1.0+ rules enforced
──────────────────────────
  No JArray / JObject  ·  No manual thread attach  ·  No forced shutdown
  No @JImplements  ·  Dynamic class loading used post-JVM-start
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import faulthandler
import importlib.util
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jvm_memory_loader import JvmMemoryLoader

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────
_SCRIPTS_DIR = Path(__file__).resolve().parent
_PMC_INTERCEPT_PY = _SCRIPTS_DIR / "pmc_intercept.py"

# ── portablemc 5.x environment bootstrap ──────────────────────────────────
# This MUST be called before any portablemc import to ensure the vendored
# copy is used and (optionally) the native extension is loaded in-memory.
_PMC_BOOTSTRAP_COMPLETE = False
_PMC_INMEMORY_MODE = False


def _ensure_portablemc_environment(*, inmemory: bool = False) -> bool:
    """
    Ensure the portablemc 5.x environment is properly initialized.

    This function:
    1. Adds the vendored portablemc tree to sys.path
    2. Optionally loads the _portablemc.pyd in-memory (for strict exec policies)
    3. Scrubs any pip-installed portablemc to avoid version conflicts

    Parameters
    ----------
    inmemory : bool
        If True, attempt to load _portablemc.pyd via pythonmemorymodule
        instead of the normal LoadLibrary path.

    Returns True if the environment is ready for portablemc imports.
    """
    global _PMC_BOOTSTRAP_COMPLETE, _PMC_INMEMORY_MODE

    if _PMC_BOOTSTRAP_COMPLETE:
        return True

    # Ensure scripts dir is on path for local_portablemc import
    scripts_str = str(_SCRIPTS_DIR)
    if scripts_str not in sys.path:
        sys.path.insert(0, scripts_str)

    try:
        from local_portablemc import bootstrap, bootstrap_inmemory

        if inmemory:
            log.info("Bootstrapping portablemc 5.x in in-memory mode")
            result = bootstrap_inmemory(scripts_dir=_SCRIPTS_DIR)
            _PMC_INMEMORY_MODE = result
            _PMC_BOOTSTRAP_COMPLETE = result
            if result:
                log.info("portablemc 5.x in-memory bootstrap successful")
            else:
                log.warning(
                    "In-memory bootstrap failed; falling back to standard mode"
                )
                # Fall back to standard bootstrap
                _PMC_BOOTSTRAP_COMPLETE = bootstrap(scripts_dir=_SCRIPTS_DIR)
        else:
            _PMC_BOOTSTRAP_COMPLETE = bootstrap(scripts_dir=_SCRIPTS_DIR)

        return _PMC_BOOTSTRAP_COMPLETE

    except ImportError as exc:
        log.warning("Could not import local_portablemc: %s", exc)
        return False
    except Exception as exc:
        log.error("portablemc environment bootstrap failed: %s", exc)
        return False


def is_portablemc_inmemory() -> bool:
    """Check if portablemc is running in in-memory mode."""
    return _PMC_INMEMORY_MODE


# ══════════════════════════════════════════════════════════════════════════════
# § 1  Process-level utilities
# ══════════════════════════════════════════════════════════════════════════════


def _flush_log() -> None:
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass


def _enable_crash_log() -> None:
    try:
        d = Path.cwd() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        log_path = d / "python_native_crash.log"
        # Truncate on each boot so the log does not grow unboundedly with
        # JVM-internal handled exceptions (HotSpot uses AVs for null checks).
        f = log_path.open("w", encoding="utf-8")
        f.write(f"=== jvm_boot_glue crash capture {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
    except Exception as exc:
        log.warning("Could not enable crash log: %s", exc)


def _wait_non_daemon_threads() -> None:
    """Block until all Java non-daemon threads finish."""
    import jpype

    Thread = jpype.JClass("java.lang.Thread")
    current = Thread.currentThread()
    log.info("Waiting for Java non-daemon threads…")
    while True:
        live = [
            t
            for t in Thread.getAllStackTraces().keySet().toArray()
            if t.isAlive() and not t.isDaemon() and t != current
        ]
        if not live:
            break
        log.debug("  alive: %s", ", ".join(str(t.getName()) for t in live[:6]))
        time.sleep(1.0)
    log.info("All Java non-daemon threads done")


def _safe_cwd(path) -> str:
    """Return a subprocess-safe cwd — never a UNC path."""
    if path is None:
        return tempfile.gettempdir()
    s = str(path)
    if s.startswith("\\\\"):
        return tempfile.gettempdir()
    try:
        r = str(Path(s).resolve())
        return r if not r.startswith("\\\\") else tempfile.gettempdir()
    except Exception:
        return tempfile.gettempdir()


def _clean_pythonpath(paths: list[str]) -> list[str]:
    """Remove scripts/ from a sys.path list so installed portablemc wins."""
    local = str(_SCRIPTS_DIR).casefold()
    out: list[str] = []
    for p in paths:
        if not p:
            out.append(p)
            continue
        try:
            if str(Path(p).resolve()).casefold() == local:
                continue
        except OSError:
            pass
        out.append(p)
    return out


class _CleanPath:
    """Context manager: hide scripts/ from sys.path temporarily."""

    def __enter__(self):
        self._old = list(sys.path)
        sys.path[:] = _clean_pythonpath(sys.path)

    def __exit__(self, *_):
        sys.path[:] = self._old


# ══════════════════════════════════════════════════════════════════════════════
# § 2  JNI constants  (from jni.h)
# ══════════════════════════════════════════════════════════════════════════════

JNI_OK = 0
JNI_ERR = -1
JNI_EDETACHED = -2
JNI_VERSION_9 = 0x00090000
_PTR = 8  # pointer size, x64


def _resolve_jdk_jvm_dll_on_disk(jdk_bin: Path) -> Path:
    """
    Locate ``jvm.dll`` beside ``jdk_bin`` (same rules as ``JvmMemoryLoader``).

    Kept standalone so callers can resolve paths without requiring a loaded
    memory map — mirrors ``JvmMemoryLoader.resolve_jvm_dll_file``.
    """
    server_path = jdk_bin / "server" / "jvm.dll"
    if server_path.is_file():
        return server_path
    flat = jdk_bin / "jvm.dll"
    return flat if flat.is_file() else server_path


def _path_basenames_targets_jvm_dll(path_text: str) -> bool:
    """True when the basename is ``jvm.dll`` (any spelling / slashes)."""
    if not path_text:
        return False
    normalized = os.path.basename(path_text.replace("/", "\\")).casefold()
    return normalized == "jvm.dll"


def _is_jvm_named_module_lookup(name: str | None) -> bool:
    """``GetModuleHandle*`` probe that targets the JVM image."""
    if not name:
        return False
    n = name.replace("/", "\\").casefold()
    return n.endswith("jvm.dll") or n == "jvm"


def _wide_path_arg_to_str(path) -> str:
    """Decode a Win32 wide path argument (``LPCWSTR`` or Python ``str``)."""
    if path is None:
        return ""
    if isinstance(path, str):
        return path
    try:
        return ctypes.wstring_at(int(path)).rstrip("\0")
    except (ctypes.ArgumentError, OSError, ValueError, TypeError):
        return ""


# ═══════════════════════════════════════��══════════════════════════════════════
# § 3  ctypes WINFUNCTYPE prototypes
# ══════════════════════════════════════════════════════════════════════════════

_LLW_t = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_wchar_p)
_LLA_t = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p)
_LLEXW_t = ctypes.WINFUNCTYPE(
    ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.wintypes.DWORD
)
_GPA_t = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)
_FL_t = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.c_void_p)
_GMFA_t = ctypes.WINFUNCTYPE(
    ctypes.wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD
)
_GMFW_t = ctypes.WINFUNCTYPE(
    ctypes.wintypes.DWORD, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.wintypes.DWORD
)
# GetModuleHandleW/A — name in, HMODULE out
_GMHW_t = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_wchar_p)
_GMHA_t = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p)
# GetModuleHandleExW/A — flags, name-or-address, out HMODULE*
# Use c_void_p for the second arg because it's polymorphic (LPCWSTR or address).
_GMHEW_t = ctypes.WINFUNCTYPE(
    ctypes.wintypes.BOOL,
    ctypes.wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
)
_GMHEA_t = ctypes.WINFUNCTYPE(
    ctypes.wintypes.BOOL,
    ctypes.wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
)
# Use c_void_p for all wide-string path args in IAT callbacks.
# c_wchar_p truncates \\?\ UNC-prefixed paths at the first inner null,
# so we receive the raw pointer and use ctypes.wstring_at to decode.
_GFAEW_t = ctypes.WINFUNCTYPE(
    ctypes.wintypes.BOOL, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.c_void_p
)
_GFPW_t = ctypes.WINFUNCTYPE(
    ctypes.wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
)
_FFFW_t = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_CFW_t = ctypes.WINFUNCTYPE(
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.wintypes.DWORD,
    ctypes.wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.wintypes.DWORD,
    ctypes.wintypes.DWORD,
    ctypes.c_void_p,
)
_GFAA_t = ctypes.WINFUNCTYPE(ctypes.wintypes.DWORD, ctypes.c_char_p)
_FFFA_t = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p)
_FULLPATH_t = ctypes.CFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t
)
_CJVM_t = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_void_p,
)
# DllMain(HINSTANCE, DWORD reason, LPVOID reserved)
_DLLMAIN_t = ctypes.WINFUNCTYPE(
    ctypes.wintypes.BOOL,
    ctypes.c_void_p,
    ctypes.wintypes.DWORD,
    ctypes.c_void_p,
)
# UCRT: int _stat64i32(const char *path, struct _stat64i32 *buffer)
_STAT64I32_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)


# ══════════════════════════════════════════════════════════════════════════════
# § 4  IAT entry — one patchable slot in a PE module's Import Address Table
# ══════════════════════════════════════════════════════════════════════════════


class _IATEntry:
    _k32 = ctypes.windll.kernel32

    def __init__(self, addr: int) -> None:
        self._addr = addr
        self._original = ctypes.c_void_p.from_address(addr).value

    @property
    def original(self) -> int:
        return self._original

    def patch(self, fn_ptr: int) -> None:
        vp = self._k32.VirtualProtect
        old = ctypes.wintypes.DWORD(0)
        if not vp(ctypes.c_void_p(self._addr), _PTR, 0x04, ctypes.byref(old)):
            raise OSError(
                f"VirtualProtect failed @ 0x{self._addr:x}: {self._k32.GetLastError()}"
            )
        ctypes.c_void_p.from_address(self._addr).value = fn_ptr
        dummy = ctypes.wintypes.DWORD(0)
        vp(ctypes.c_void_p(self._addr), _PTR, old, ctypes.byref(dummy))

    def restore(self) -> None:
        self.patch(self._original)


# ── PE parser internals ────────────────────────────────────────────────────
#
# A long-standing bug used to live here: the previous manual walk read the
# Import directory at OptionalHeader offset (120 + 8) = 128, which is actually
# DataDirectory[2] (Resource), not DataDirectory[1] (Import).  In PE32+,
# IMAGE_DATA_DIRECTORY entries start at OptionalHeader offset 112 and are
# 8 bytes each:
#
#     [0] Export   VA=112  Size=116
#     [1] Import   VA=120  Size=124   ← what we actually want
#     [2] Resource VA=128  Size=132
#     ...
#
# That stray "+ 8" caused us to walk Resource-tree bytes as if they were
# IMAGE_IMPORT_DESCRIPTORs, dereferencing whatever happened to live there as
# a "Name RVA" — which is the access violation the user hit:
#
#     File ".../jvm_boot_glue.py", line 248, in _find_iat
#       dname = ctypes.string_at(module_base + nrv).decode(...)
#     Windows fatal exception: access violation
#
# The bug was masked whenever pefile was available (the _jpype.pyd path
# succeeded that way), but the in-memory jvm.dll mapping is not on the OS
# loader's list, so pefile silently failed for it and we fell through to the
# broken manual walk.  Below we (a) fix the offsets, (b) parse the on-disk
# bytes whenever a real path is available (image headers are not modified by
# pythonmemorymodule, but disk bytes are guaranteed clean), and (c) bound-
# check every dereference so a malformed table can no longer crash Python.


def _parse_pe_imports(data: bytes) -> dict[tuple[str, bytes], int]:
    """
    Parse a PE32+ image (from on-disk bytes) and return a mapping

        {(dll_name_upper, function_name_bytes): IAT slot RVA}

    The returned RVAs are image-relative — add ``module_base`` to obtain the
    address of the IAT slot inside the loaded module (works for both OS-
    loaded and pythonmemorymodule-mapped images, because section RVAs are
    identical in either layout).

    All offsets are validated against the buffer length, so a truncated or
    malformed file raises a plain ``RuntimeError`` instead of crashing.
    """
    if len(data) < 0x40 or data[0:2] != b"MZ":
        raise RuntimeError("Not a PE: missing MZ header")

    lfa = int.from_bytes(data[0x3C:0x40], "little")
    if lfa < 0 or lfa + 24 > len(data) or data[lfa : lfa + 4] != b"PE\0\0":
        raise RuntimeError("Not a PE: missing PE signature")

    opt = lfa + 24
    if opt + 2 > len(data):
        raise RuntimeError("Truncated optional header")
    magic = int.from_bytes(data[opt : opt + 2], "little")
    if magic != 0x20B:
        raise RuntimeError(f"Not PE32+ (magic=0x{magic:x})")

    # DataDirectory[1] = Import   →   VA at opt+120, Size at opt+124
    if opt + 128 > len(data):
        raise RuntimeError("Truncated data directories")
    imp_va = int.from_bytes(data[opt + 120 : opt + 124], "little")
    imp_size = int.from_bytes(data[opt + 124 : opt + 128], "little")
    if not imp_va or not imp_size:
        raise RuntimeError("No import directory")

    # Section table starts after the optional header.  We need it because the
    # import directory's RVA must be translated to a file offset to read it
    # from the on-disk bytes.
    nsections = int.from_bytes(data[lfa + 6 : lfa + 8], "little")
    sz_opthdr = int.from_bytes(data[lfa + 20 : lfa + 22], "little")
    sec_tbl = lfa + 24 + sz_opthdr
    sections: list[tuple[int, int, int, int]] = []  # (va, vsize, raw_off, raw_sz)
    for i in range(nsections):
        s = sec_tbl + i * 40
        if s + 40 > len(data):
            raise RuntimeError("Truncated section table")
        vsize = int.from_bytes(data[s + 8 : s + 12], "little")
        va = int.from_bytes(data[s + 12 : s + 16], "little")
        rawsz = int.from_bytes(data[s + 16 : s + 20], "little")
        rawoff = int.from_bytes(data[s + 20 : s + 24], "little")
        sections.append((va, vsize, rawoff, rawsz))

    def rva_to_off(rva: int) -> int:
        for va, vsize, rawoff, rawsz in sections:
            if va <= rva < va + max(vsize, rawsz):
                return rawoff + (rva - va)
        raise RuntimeError(f"RVA 0x{rva:x} not in any section")

    def read_cstr(off: int, limit: int = 4096) -> bytes:
        end = data.find(b"\x00", off, min(off + limit, len(data)))
        if end < 0:
            raise RuntimeError("Unterminated C string in PE")
        return data[off:end]

    out: dict[tuple[str, bytes], int] = {}

    # IMAGE_IMPORT_DESCRIPTOR is 20 bytes:
    #   DWORD OriginalFirstThunk   (offset  0)
    #   DWORD TimeDateStamp        (offset  4)
    #   DWORD ForwarderChain       (offset  8)
    #   DWORD Name                 (offset 12)
    #   DWORD FirstThunk           (offset 16)
    desc_off = rva_to_off(imp_va)
    end_off = desc_off + imp_size  # hard upper bound
    while desc_off + 20 <= min(end_off, len(data)):
        oft = int.from_bytes(data[desc_off : desc_off + 4], "little")
        nrv = int.from_bytes(data[desc_off + 12 : desc_off + 16], "little")
        ft = int.from_bytes(data[desc_off + 16 : desc_off + 20], "little")
        if oft == 0 and ft == 0:
            break  # canonical terminator (Name field is don't-care)
        try:
            dll_name = read_cstr(rva_to_off(nrv)).decode("ascii", "replace").upper()
        except RuntimeError:
            desc_off += 20
            continue

        # Walk the Import Name Table (OFT) and record IAT RVA = ft + i*8 for
        # each named import.  Ordinal imports (high bit set) are skipped.
        thunk_rva = oft if oft else ft  # bound imports use FT for both
        try:
            thunk_off = rva_to_off(thunk_rva)
        except RuntimeError:
            desc_off += 20
            continue

        i = 0
        while thunk_off + 8 <= len(data):
            tv = int.from_bytes(data[thunk_off : thunk_off + 8], "little")
            if tv == 0:
                break
            if not (tv >> 63):
                # Named import — Hint(2) + zero-terminated name follows.
                try:
                    ibn_off = rva_to_off(tv & 0x7FFF_FFFF_FFFF_FFFF)
                    fname = read_cstr(ibn_off + 2)
                    out[(dll_name, fname)] = ft + i * _PTR
                except RuntimeError:
                    pass
            i += 1
            thunk_off += 8
        desc_off += 20

    return out


# Cache: parsed import tables keyed by canonical disk path.  PE imports are
# immutable for a given binary, so re-reading on every _find_iat() call would
# be wasteful.
_PE_IMPORTS_CACHE: dict[str, dict[tuple[str, bytes], int]] = {}


def _imports_for_path(module_path: str) -> dict[tuple[str, bytes], int]:
    key = str(Path(module_path).resolve()).lower()
    cached = _PE_IMPORTS_CACHE.get(key)
    if cached is not None:
        return cached
    with open(module_path, "rb") as fh:
        data = fh.read()
    imports = _parse_pe_imports(data)
    _PE_IMPORTS_CACHE[key] = imports
    return imports


def _find_iat(
    module_base: int, target_dll: str, target_fn: str, module_path: str | None = None
) -> _IATEntry:
    """
    Locate an IAT slot inside ``module_base`` for ``target_dll!target_fn``.

    Pass ``target_dll="*"`` to match ``target_fn`` across any imported DLL.

    Strategy
    ────────
    1. If ``module_path`` is supplied, parse the on-disk PE bytes (clean, not
       subject to any in-memory rewriting) and translate the resulting RVA
       to ``module_base + RVA``.  This is the fast and bullet-proof path
       and is used for both real OS-loaded modules and the in-memory jvm.dll
       (whose disk file is always available — we mapped its bytes from
       there).
    2. Otherwise, fall back to a bounds-checked in-memory walk.  Every
       dereference is guarded by VirtualQuery / size-check so a malformed
       table cannot AV the host process.

    The disk-bytes parse uses the *correct* DataDirectory[1] offsets — see
    the comment block above ``_parse_pe_imports`` for the historical bug
    this replaces.
    """
    any_dll = target_dll == "*"
    tdll = target_dll.upper()
    tfn = target_fn.encode("ascii")

    # ── path 1: parse the on-disk PE bytes ─────────────────────────────────
    if module_path and os.path.isfile(module_path):
        try:
            imports = _imports_for_path(module_path)
            if any_dll:
                for (dll_name, func_name), rva in imports.items():
                    if func_name == tfn:
                        log.debug(
                            "disk-parse wildcard _find_iat matched %s!%s",
                            dll_name,
                            target_fn,
                        )
                        return _IATEntry(module_base + rva)
            else:
                rva = imports.get((tdll, tfn))
                if rva is not None:
                    return _IATEntry(module_base + rva)
            raise RuntimeError(
                f"IAT entry {target_dll}!{target_fn} not in {module_path}"
            )
        except (OSError, RuntimeError) as exc:
            log.debug(
                "disk-parse _find_iat(%s, %s) failed: %s — trying memory walk",
                target_dll,
                target_fn,
                exc,
            )

    # ── path 2: bounds-checked in-memory walk (no disk file) ───────────────
    # Validate that module_base points at a readable PE32+ image first.
    try:
        if ctypes.c_uint16.from_address(module_base).value != 0x5A4D:
            raise RuntimeError("No MZ header")
        lfa = ctypes.c_int32.from_address(module_base + 0x3C).value
        if lfa < 0 or lfa > 0x10_0000:
            raise RuntimeError(f"Implausible e_lfanew: 0x{lfa:x}")
        nt = module_base + lfa
        if ctypes.c_uint32.from_address(nt).value != 0x00004550:
            raise RuntimeError("No PE sig")
        opt = nt + 24
        if ctypes.c_uint16.from_address(opt).value != 0x20B:
            raise RuntimeError("Not PE32+")

        # CORRECT offsets: DataDirectory[1] = (opt+120, opt+124).  The
        # historical "+ 8" bug pointed at DataDirectory[2] (Resource).
        imp_va = ctypes.c_uint32.from_address(opt + 120).value
        imp_size = ctypes.c_uint32.from_address(opt + 124).value
    except OSError as exc:
        raise RuntimeError(
            f"Cannot read PE headers at 0x{module_base:x}: {exc}"
        ) from exc

    if not imp_va or not imp_size:
        raise RuntimeError("No import directory")

    # Bound the descriptor walk to the directory size — never read past.
    desc = module_base + imp_va
    desc_end = desc + imp_size

    def _safe_cstr(addr: int, max_len: int = 4096) -> bytes:
        """string_at with a probe: returns b'' if the page is not readable."""
        try:
            # IsBadReadPtr is officially deprecated but still works for our
            # one-shot validation — and unlike VirtualQuery it doesn't
            # require composing MEMORY_BASIC_INFORMATION for every call.
            ctypes.windll.kernel32.IsBadReadPtr.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            ctypes.windll.kernel32.IsBadReadPtr.restype = ctypes.wintypes.BOOL
            if ctypes.windll.kernel32.IsBadReadPtr(ctypes.c_void_p(addr), 1):
                return b""
            return ctypes.string_at(addr, max_len).split(b"\x00", 1)[0]
        except OSError:
            return b""

    while desc + 20 <= desc_end:
        try:
            oft = ctypes.c_uint32.from_address(desc).value
            nrv = ctypes.c_uint32.from_address(desc + 12).value
            ft = ctypes.c_uint32.from_address(desc + 16).value
        except OSError:
            break
        if oft == 0 and ft == 0:
            break

        dname = _safe_cstr(module_base + nrv).decode("ascii", "replace").upper()
        if any_dll or dname == tdll:
            thunk = oft or ft  # fall back to FT for bound imports
            i = 0
            while True:
                try:
                    tv = ctypes.c_uint64.from_address(
                        module_base + thunk + i * _PTR
                    ).value
                except OSError:
                    break
                if not tv:
                    break
                if not (tv >> 63):
                    name = _safe_cstr(module_base + (tv & 0x7FFF_FFFF_FFFF_FFFF) + 2)
                    if name == tfn:
                        return _IATEntry(module_base + ft + i * _PTR)
                i += 1
                if i > 65536:  # paranoia bound
                    break
        desc += 20

    raise RuntimeError(f"IAT entry {target_dll}!{target_fn} not found")


# ══════════════════════════════════════════════════════════════════════════════
# § 5  JvmBootGlue — IAT-hooked in-memory JVM bootstrap (option 5)
# ══════════════════════════════════════════════════════════════════════════════


class JvmBootGlue:
    """
    Patches _jpype.pyd's IAT so that jpype.startJVM() transparently uses the
    in-memory jvm.dll image built by JvmMemoryLoader.
    """

    def __init__(
        self,
        loader: "JvmMemoryLoader",
        jvm_flags: list[str],
        classpath: list[str],
        main_class: str,
        game_args: list[str],
        debug: bool = False,
    ) -> None:
        self._loader = loader
        self._flags = jvm_flags
        self._classpath = classpath
        self._main = main_class
        self._gargs = game_args
        self._debug = debug
        self._fake = loader.jvm_codebase()

        # IAT entries — populated by _patch_iat.  Keys ending with the dll
        # they live in: jpype-side hooks (llw/gpa) target _jpype.pyd; all
        # others (gmfa, gmfw, gmhw, gmha, gmhew, gmhea, lla, llexw, jgpa,
        # fl) target jvm.dll's IAT so calls jvm.dll makes into the OS loader
        # are intercepted before the real loader sees an unknown handle.
        self._iat: dict[str, _IATEntry | None] = {
            "llw": None,
            "gpa": None,
            # Extra _jpype.pyd import slots — JPype 1.5+ may call these instead
            # of LoadLibraryW when resolving the JVM path.
            "jp_lla": None,
            "jp_llexw": None,
            "jp_gmhw": None,
            # FreeLibrary in _jpype.pyd — suppressed for fake handles so JPype's
            # C++ cleanup path never hands the OS a raw VirtualAlloc base address.
            "jp_fl": None,
            "gmfa": None,
            "gmfw": None,
            "gmhw": None,
            "gmha": None,  # GetModuleHandleW / A
            "gmhew": None,
            "gmhea": None,  # GetModuleHandleExW / A
            "lla": None,
            "llexw": None,
            "jgpa": None,
            "fl": None,
            "stat": None,
            "gfaew": None,
            "gfpw": None,
            "fffw": None,
            "cfw": None,
            "gfaa": None,
            "fffa": None,
            "fullpath": None,
        }
        # jvm.dll memory range — needed for GetModuleHandleEx FROM_ADDRESS.
        # Populated by _patch_iat once we know the SizeOfImage from the PE
        # header at self._fake.
        self._jvm_lo: int = 0
        self._jvm_hi: int = 0
        # Hook callables — held alive to prevent GC
        self._hooks: dict[str, object] = {}

    # ── hook construction ──────────────────────────────────────────────────

    def _make_hooks(self) -> None:
        fake = self._fake
        loader = self._loader
        mods = loader.loaded_modules
        by_addr: dict[int, object] = {m._codebaseaddr: m for m in mods.values()}

        # LoadLibraryW: intercept jvm.dll → fake handle
        real_llw = _LLW_t(self._iat["llw"].original)

        def _llw(path):
            s = _wide_path_arg_to_str(path)
            if _path_basenames_targets_jvm_dll(s):
                return fake
            return real_llw(path)

        self._hooks["llw"] = _LLW_t(_llw)

        # --- _jpype: LoadLibraryA / LoadLibraryExW (often used by MSVC CRT shims)

        if self._iat.get("jp_lla"):
            real_jp_lla = _LLA_t(self._iat["jp_lla"].original)

            def _jp_lla(path):
                if path:
                    try:
                        text = path.decode("mbcs", errors="replace")
                    except Exception:
                        text = ""
                    if _path_basenames_targets_jvm_dll(text):
                        return fake
                return real_jp_lla(path)

            self._hooks["jp_lla"] = _LLA_t(_jp_lla)

        if self._iat.get("jp_llexw"):
            real_jp_llexw = _LLEXW_t(self._iat["jp_llexw"].original)

            def _jp_llexw(path, hfile, flags):
                s = _wide_path_arg_to_str(path)
                if _path_basenames_targets_jvm_dll(s):
                    return fake
                return real_jp_llexw(path, hfile, flags)

            self._hooks["jp_llexw"] = _LLEXW_t(_jp_llexw)

        if self._iat.get("jp_gmhw"):
            real_jp_gmhw = _GMHW_t(self._iat["jp_gmhw"].original)

            def _jp_gmhw(name):
                if name:
                    try:
                        s = ctypes.c_wchar_p(name).value
                    except (ValueError, OSError, TypeError):
                        s = None
                    if _is_jvm_named_module_lookup(s):
                        return fake
                return real_jp_gmhw(name)

            self._hooks["jp_gmhw"] = _GMHW_t(_jp_gmhw)

        # FreeLibrary in _jpype.pyd — suppress when fake handle passed so JPype
        # error/cleanup paths cannot crash the process on an unmapped address.
        if self._iat.get("jp_fl"):
            real_jp_fl = _FL_t(self._iat["jp_fl"].original)

            def _jp_fl(hmod):
                if hmod == fake or hmod in by_addr:
                    log.debug(
                        "FreeLibrary(_jpype.pyd) suppressed for in-memory"
                        " handle 0x%x",
                        hmod or 0,
                    )
                    return 1
                return real_jp_fl(hmod)

            self._hooks["jp_fl"] = _FL_t(_jp_fl)

        # JNI_CreateJavaVM trampoline — hybrid strategy
        # ────────────────────────────────────────────────────────────────────
        # Primary path: load jvm.dll through the OS loader (ctypes.WinDLL).
        # The OS loader runs DllMain properly, initialises the UCRT, and
        # registers the module so GetModuleHandleExW works.  All of the
        # in-memory GetModuleHandle* / GetModuleFileName* hooks are therefore
        # irrelevant for this path — we only need LoadLibraryW / GetProcAddress
        # in _jpype.pyd to be intercepted so JPype sees a fake HMODULE and
        # our GetProcAddress hook can dispatch JNI function lookups.
        #
        # Fallback: if the OS loader path fails (e.g. a DLL is blocked) we
        # drop back to the original in-memory JNI_CreateJavaVM.
        real_create = loader.jni_create_java_vm
        _jdk_bin_for_trampoline = loader.jdk_bin  # captured at hook-build time

        def _cjvm(pvm, penv, args):
            log.info(
                "Trampoline → JNI_CreateJavaVM  (hybrid: OS loader first,"
                " in-memory fallback)"
            )
            # ── Primary: OS-loaded jvm.dll ────────────────────────────────
            os_rc: int | None = None
            try:
                _jvm_disk = str(
                    _jdk_bin_for_trampoline / "server" / "jvm.dll"
                )
                # ctypes.WinDLL triggers LoadLibraryW → our hook returns fake,
                # but WinDLL stores its own internal handle which is the real
                # OS handle from the FIRST real LoadLibrary call (before our
                # hook ran) or from kernel's module list if already loaded.
                # To guarantee we get the OS handle we call the REAL
                # LoadLibraryW via the original (unhooked) slot.
                k32 = ctypes.windll.kernel32
                k32.LoadLibraryW.restype = ctypes.c_void_p
                k32.LoadLibraryW.argtypes = [ctypes.c_wchar_p]
                _real_llw_addr = self._iat["llw"].original if self._iat.get("llw") else None
                if _real_llw_addr:
                    _orig_llw = _LLW_t(_real_llw_addr)
                    _os_handle = _orig_llw(_jvm_disk)
                else:
                    _os_handle = k32.LoadLibraryW(_jvm_disk)
                if _os_handle:
                    k32.GetProcAddress.restype = ctypes.c_void_p
                    k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                    _fn_addr = k32.GetProcAddress(
                        ctypes.c_void_p(_os_handle), b"JNI_CreateJavaVM"
                    )
                    if _fn_addr:
                        _os_create = _CJVM_t(_fn_addr)
                        os_rc = _os_create(pvm, penv, args)
                        log.info("OS-loader JNI_CreateJavaVM → %d", os_rc)
                    else:
                        log.warning(
                            "OS-loaded jvm.dll missing JNI_CreateJavaVM export"
                        )
                else:
                    log.warning(
                        "OS LoadLibraryW(%s) returned NULL (err=%d)",
                        _jvm_disk,
                        ctypes.GetLastError(),
                    )
            except Exception as _exc:
                log.warning("OS-loader JNI_CreateJavaVM path failed: %s", _exc)
                os_rc = None

            if os_rc is not None:
                if os_rc == JNI_OK:
                    log.info(
                        "  ✅ JavaVM* 0x%016x  JNIEnv* 0x%016x",
                        pvm[0] or 0,
                        penv[0] or 0,
                    )
                else:
                    log.error("  OS-loader JNI_CreateJavaVM returned %d", os_rc)
                return os_rc

            # ── Fallback: in-memory jvm.dll ───────────────────────────────
            log.info("Trampoline → in-memory JNI_CreateJavaVM (fallback)")
            rc = real_create(pvm, penv, args)
            if rc == JNI_OK:
                log.info(
                    "  ✅ JavaVM* 0x%016x  JNIEnv* 0x%016x",
                    pvm[0] or 0,
                    penv[0] or 0,
                )
            else:
                log.error("  in-memory JNI_CreateJavaVM returned %d", rc)
            return rc

        self._hooks["cjvm"] = _CJVM_t(_cjvm)

        # GetProcAddress dispatch for fake handle
        ta = ctypes.cast(self._hooks["cjvm"], ctypes.c_void_p).value
        dispatch: dict[bytes, int] = {
            b"JNI_CreateJavaVM": ta,
            b"JNI_GetCreatedJavaVMs": ctypes.cast(
                loader.jni_get_created_jvms, ctypes.c_void_p
            ).value,
            b"JNI_GetDefaultJavaVMInitArgs": ctypes.cast(
                loader.jni_get_default_init_args, ctypes.c_void_p
            ).value,
        }
        real_gpa = _GPA_t(self._iat["gpa"].original)

        def _gpa(hmod, name):
            if hmod == fake and name:
                addr = dispatch.get(name)
                if addr:
                    return addr
                log.warning("GPA(fake, %r) unknown", name)
                return 0
            return real_gpa(hmod, name)

        self._hooks["gpa"] = _GPA_t(_gpa)

        # jvm.dll IAT hooks (GetModuleFileNameA/W, LoadLibraryA/ExW, GPA, FreeLibrary)
        jvm_path = str(loader.jvm_dll_path)
        jvm_path_b = jvm_path.encode("mbcs", errors="replace")
        jvm_path_w = jvm_path
        runtime_image = loader.jdk_bin.parent / "lib" / "modules"

        def _norm_probe_path(path_text: str) -> str:
            s = path_text.replace("/", "\\")
            if s.startswith("\\\\?\\UNC\\"):
                s = "\\" + s[7:]
            elif s.startswith("\\\\?\\"):
                s = s[4:]
            return os.path.normcase(os.path.abspath(s))

        runtime_image_norm = _norm_probe_path(str(runtime_image))

        def _is_runtime_image_probe(path_text: str) -> bool:
            try:
                return _norm_probe_path(path_text) == runtime_image_norm
            except Exception:
                return False

        if self._iat.get("gmfa"):
            real_gmfa = _GMFA_t(self._iat["gmfa"].original)

            def _gmfa(hmod, buf, size):
                if hmod == fake:
                    if not buf or size == 0:
                        return 0
                    pay = jvm_path_b[: max(0, size - 1)]
                    ctypes.memmove(buf, pay, len(pay))
                    ctypes.memset(buf + len(pay), 0, 1)
                    return len(pay)
                return real_gmfa(hmod, buf, size)

            self._hooks["gmfa"] = _GMFA_t(_gmfa)

        if self._iat.get("gmfw"):
            real_gmfw = _GMFW_t(self._iat["gmfw"].original)

            def _gmfw(hmod, buf, size):
                if hmod == fake:
                    if not buf or size == 0:
                        return 0
                    src = jvm_path_w[: max(0, size - 1)]
                    ctypes.memmove(
                        buf, (src + "\0").encode("utf-16-le"), (len(src) + 1) * 2
                    )
                    return len(src)
                return real_gmfw(hmod, buf, size)

            self._hooks["gmfw"] = _GMFW_t(_gmfw)

        if self._iat.get("lla"):
            real_lla = _LLA_t(self._iat["lla"].original)

            def _lla(path):
                if path:
                    name = os.path.basename(path.decode("mbcs", "replace")).lower()
                    m = mods.get(name)
                    if m:
                        return m._codebaseaddr
                return real_lla(path)

            self._hooks["lla"] = _LLA_t(_lla)

        if self._iat.get("llexw"):
            real_llexw = _LLEXW_t(self._iat["llexw"].original)

            def _llexw(path, hfile, flags):
                if path:
                    name = os.path.basename(path).lower()
                    m = mods.get(name)
                    if m:
                        return m._codebaseaddr
                return real_llexw(path, hfile, flags)

            self._hooks["llexw"] = _LLEXW_t(_llexw)

        if self._iat.get("llw2"):
            real_llw2 = _LLW_t(self._iat["llw2"].original)

            def _llw2(path):
                if path:
                    # jvm.dll can LoadLibraryW absolute paths to sibling JDK DLLs
                    # (e.g. jimage.dll). Satisfy from our in-memory registry.
                    try:
                        name = os.path.basename(path).lower()
                    except Exception:
                        name = ""
                    if name == "jvm.dll":
                        return fake
                    m = mods.get(name)
                    if m:
                        return m._codebaseaddr
                return real_llw2(path)

            self._hooks["llw2"] = _LLW_t(_llw2)

        if self._iat.get("jgpa"):
            real_jgpa = _GPA_t(self._iat["jgpa"].original)

            def _jgpa(hmod, name):
                mo = by_addr.get(hmod)
                if mo and name:
                    try:
                        fp = mo.get_proc_addr(name.decode("ascii", "replace"))
                        return ctypes.cast(fp, ctypes.c_void_p).value or 0
                    except Exception:
                        return 0
                return real_jgpa(hmod, name)

            self._hooks["jgpa"] = _GPA_t(_jgpa)

        if self._iat.get("fl"):
            real_fl = _FL_t(self._iat["fl"].original)

            def _fl(hmod):
                if hmod in by_addr:
                    return 1
                return real_fl(hmod)

            self._hooks["fl"] = _FL_t(_fl)

        if self._iat.get("stat"):
            real_stat = _STAT64I32_t(self._iat["stat"].original)

            def _stat64i32(path, stbuf):
                try:
                    path_text = path.decode("mbcs", "replace") if path else ""
                except Exception:
                    path_text = repr(path)
                log.info("DIAG _stat64i32 called: %r", path_text[:120])
                rc = real_stat(path, stbuf)
                log.info("DIAG _stat64i32(%s) -> %d", path_text[:120], rc)
                if (
                    rc != 0
                    and path_text
                    and _is_runtime_image_probe(path_text)
                    and runtime_image.is_file()
                ):
                    if stbuf:
                        ctypes.memset(stbuf, 0, 160)
                    log.warning(
                        "Forcing _stat64i32 success for runtime image: %s", path_text
                    )
                    return 0
                return rc

            self._hooks["stat"] = _STAT64I32_t(_stat64i32)

        # ── Shared helper ────────────────────────────────────────────────────
        # wide_abs_unc_path() inside the memory-mapped jvm.dll produces a
        # corrupted UNC prefix:  instead of  "\\?\C:\...\lib\modules"
        # the result buffer begins  "\C\0\C:\...\lib\modules"
        # (the \\?\  prefix constant resolves to wrong .rdata bytes).
        # wstring_at() therefore reads only "\C" (2 chars + null).
        # The real absolute path is embedded starting at byte-offset 8 of
        # that buffer (wchar offset 4 = skip \C + null wchar + leading \).
        def _real_path_from_corrupted(ptr) -> str:
            """Decode the real Windows path from a corrupted \\?\\-prefix buffer."""
            short = _wptr_to_str(ptr)  # stops at embedded null → "\C"
            if (
                ptr
                and short
                and len(short) == 2
                and short[0] == "\\"
                and short[1] == "C"
            ):
                # Try reading the continuation after the null. Historically the
                # "real" DOS path starts 8 bytes after the beginning, but we also
                # include a fallback scanner because some builds shift this layout.
                try:
                    # ptr may be a c_void_p instance; int() gives address for arithmetic.
                    addr = int(ptr)
                    real = ctypes.wstring_at(addr + 8)
                    if real:
                        # Accept "C:\..." directly or "\C:\..." (one stray slash).
                        if len(real) >= 3 and real[1:2] == ":":
                            return real
                        if len(real) >= 4 and real[0] == "\\" and real[2:3] == ":":
                            return real[1:]
                except Exception:
                    pass

                # Fallback: scan a small window for an embedded UTF-16LE "X:\"
                # pattern, then decode from there.
                try:
                    addr = int(ptr)
                    # Avoid crashing by reading across an unmapped page.
                    k32 = ctypes.windll.kernel32

                    class _MBI(ctypes.Structure):
                        _fields_ = [
                            ("BaseAddress", ctypes.c_void_p),
                            ("AllocationBase", ctypes.c_void_p),
                            ("AllocationProtect", ctypes.wintypes.DWORD),
                            ("RegionSize", ctypes.c_size_t),
                            ("State", ctypes.wintypes.DWORD),
                            ("Protect", ctypes.wintypes.DWORD),
                            ("Type", ctypes.wintypes.DWORD),
                        ]

                    mbi = _MBI()
                    if k32.VirtualQuery(
                        ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
                    ):
                        region_end = int(mbi.BaseAddress) + int(mbi.RegionSize or 0)
                        avail = max(0, region_end - addr)
                        n = min(1024, avail)
                        raw = ctypes.string_at(addr, n) if n else b""
                    else:
                        raw = b""
                    pat = b":\x00\\\x00"
                    hit = raw.find(pat)
                    while hit != -1:
                        if hit >= 2:
                            lo = raw[hit - 2]
                            hi = raw[hit - 1]
                            # Expect "<letter>\0:\0\\0"
                            if hi == 0 and (
                                (65 <= lo <= 90) or (97 <= lo <= 122)
                            ):
                                start = hit - 2
                                # find UTF-16LE terminator from start
                                end = raw.find(b"\x00\x00", start)
                                if end != -1:
                                    try:
                                        cand = raw[start:end].decode(
                                            "utf-16le", errors="ignore"
                                        )
                                        if len(cand) >= 3 and cand[1:2] == ":":
                                            return cand
                                    except Exception:
                                        pass
                                break
                        hit = raw.find(pat, hit + 4)
                except Exception:
                    pass
            return short

        def _fix_corrupted_wptr(ptr) -> tuple[str, object]:
            """
            Return (decoded_path, arg) for WinAPI calls.

            Some HotSpot builds construct a broken wide string that contains an
            embedded NUL after 2 chars (e.g. "\\C\\0\\C:\\..."). Passing that
            pointer to WinAPI makes the OS see only "\\C" and fail.

            If corruption detected, call real WinAPI with a stable temporary
            c_wchar_p built from the recovered path.
            """
            real_p = _real_path_from_corrupted(ptr)
            short = _wptr_to_str(ptr)
            if real_p and short and real_p != short:
                return real_p, ctypes.c_wchar_p(real_p)
            return short, ptr

        runtime_image_str = str(runtime_image)
        runtime_image_norm_check = os.path.normcase(os.path.abspath(runtime_image_str))

        def _is_jimage_path(p: str) -> bool:
            """True if p (after normalization) is the runtime lib/modules file."""
            if not p:
                return False
            try:
                return os.path.normcase(os.path.abspath(p)) == runtime_image_norm_check
            except Exception:
                return False

        if self._iat.get("gfaew"):
            real_gfaew = _GFAEW_t(self._iat["gfaew"].original)

            def _gfaew(path_ptr, info_level, out_info):
                real_p, arg = _fix_corrupted_wptr(path_ptr)
                rc = real_gfaew(arg, info_level, out_info)
                if (
                    not rc
                    and runtime_image.is_file()
                    # Only spoof success when we are confident the probe is
                    # actually targeting the runtime image, or when the probe
                    # path is clearly corrupted ("\C").
                    and (_is_jimage_path(real_p) or real_p in ("\\C", ""))
                ):
                    if out_info:
                        ctypes.memset(out_info, 0, 64)
                        ctypes.c_uint32.from_address(
                            out_info
                        ).value = 0x20  # FILE_ATTRIBUTE_ARCHIVE
                        size = runtime_image.stat().st_size
                        ctypes.c_uint32.from_address(out_info + 32).value = (
                            size >> 32
                        ) & 0xFFFFFFFF
                        ctypes.c_uint32.from_address(out_info + 36).value = (
                            size & 0xFFFFFFFF
                        )
                    log.warning(
                        "Forced GetFileAttributesExW -> success for runtime image (probe=%r real=%s)",
                        real_p,
                        runtime_image,
                    )
                    return 1
                return rc

            self._hooks["gfaew"] = _GFAEW_t(_gfaew)

        if self._iat.get("gfpw"):
            real_gfpw = _GFPW_t(self._iat["gfpw"].original)

            def _gfpw(path_ptr, size, buffer, file_part):
                real_p, arg = _fix_corrupted_wptr(path_ptr)
                rc = real_gfpw(arg, size, buffer, file_part)
                if "modules" in real_p.lower():
                    log.info(
                        "jvm.dll GetFullPathNameW(%s, size=%d) -> %d",
                        real_p,
                        size,
                        rc,
                    )
                return rc

            self._hooks["gfpw"] = _GFPW_t(_gfpw)

        if self._iat.get("fffw"):
            real_fffw = _FFFW_t(self._iat["fffw"].original)
            invalid_handle = ctypes.c_void_p(-1).value
            FILE_ATTRIBUTE_REPARSE_POINT = 0x400

            def _fffw(path_ptr, find_data):
                real_p, arg = _fix_corrupted_wptr(path_ptr)
                h = real_fffw(arg, find_data)
                if (
                    find_data
                    and h not in (None, 0, invalid_handle)
                    and _is_jimage_path(real_p)
                ):
                    try:
                        attrs = ctypes.c_uint32.from_address(find_data).value
                        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                            ctypes.c_uint32.from_address(find_data).value = (
                                attrs & ~FILE_ATTRIBUTE_REPARSE_POINT
                            )
                            log.warning(
                                "Cleared reparse flag for runtime image (probe=%r)",
                                real_p,
                            )
                    except Exception:
                        pass
                return h

            self._hooks["fffw"] = _FFFW_t(_fffw)

        if self._iat.get("cfw"):
            real_cfw = _CFW_t(self._iat["cfw"].original)
            invalid_handle = ctypes.c_void_p(-1).value
            FILE_SHARE_READ = 0x00000001
            FILE_SHARE_WRITE = 0x00000002
            FILE_SHARE_DELETE = 0x00000004
            OPEN_EXISTING = 3
            FILE_ATTRIBUTE_NORMAL = 0x00000080

            def _cfw(path_ptr, access, share, sec, creation, flags, template):
                real_p, arg = _fix_corrupted_wptr(path_ptr)
                h = real_cfw(arg, access, share, sec, creation, flags, template)
                if "modules" in real_p.lower():
                    log.info(
                        "jvm.dll CreateFileW real_path=%s acc=0x%x -> 0x%x",
                        real_p,
                        access,
                        h or 0,
                    )
                if (
                    h == invalid_handle
                    and runtime_image.is_file()
                    and _is_jimage_path(real_p)
                ):
                    h = real_cfw(
                        runtime_image_str,
                        access,
                        share | FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                        sec,
                        OPEN_EXISTING,
                        flags or FILE_ATTRIBUTE_NORMAL,
                        template,
                    )
                    log.warning(
                        "Retry CreateFileW(%s) for runtime image -> 0x%x",
                        runtime_image_str,
                        h or 0,
                    )
                return h

            self._hooks["cfw"] = _CFW_t(_cfw)

        if self._iat.get("gfaa"):
            real_gfaa = _GFAA_t(self._iat["gfaa"].original)
            INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
            FILE_ATTRIBUTE_ARCHIVE = 0x20

            def _gfaa(path):
                path_text = path.decode("mbcs", "replace") if path else ""
                rc = real_gfaa(path)
                if path_text and "modules" in path_text.lower():
                    log.info("jvm.dll GetFileAttributesA(%s) -> 0x%x", path_text, rc)
                if (
                    rc == INVALID_FILE_ATTRIBUTES
                    and path_text
                    and _is_runtime_image_probe(path_text)
                    and runtime_image.is_file()
                ):
                    log.warning(
                        "Forcing GetFileAttributesA success for runtime image: %s",
                        path_text,
                    )
                    return FILE_ATTRIBUTE_ARCHIVE
                return rc

            self._hooks["gfaa"] = _GFAA_t(_gfaa)

        if self._iat.get("fffa"):
            real_fffa = _FFFA_t(self._iat["fffa"].original)
            invalid_handle = ctypes.c_void_p(-1).value
            FILE_ATTRIBUTE_REPARSE_POINT = 0x400

            def _fffa(path, find_data):
                path_text = path.decode("mbcs", "replace") if path else ""
                h = real_fffa(path, find_data)
                if path_text and "modules" in path_text.lower():
                    attrs = None
                    if find_data and h not in (None, 0, invalid_handle):
                        try:
                            attrs = ctypes.c_uint32.from_address(find_data).value
                        except Exception:
                            attrs = None
                    log.info(
                        "jvm.dll FindFirstFileA(%s) -> 0x%x attrs=%r",
                        path_text,
                        h or 0,
                        attrs,
                    )
                    if (
                        find_data
                        and h not in (None, 0, invalid_handle)
                        and _is_runtime_image_probe(path_text)
                    ):
                        attrs = ctypes.c_uint32.from_address(find_data).value
                        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                            ctypes.c_uint32.from_address(find_data).value = (
                                attrs & ~FILE_ATTRIBUTE_REPARSE_POINT
                            )
                            log.warning(
                                "Cleared A reparse flag for runtime image: %s",
                                path_text,
                            )
                return h

            self._hooks["fffa"] = _FFFA_t(_fffa)

        if self._iat.get("fullpath"):
            real_fullpath = _FULLPATH_t(self._iat["fullpath"].original)

            def _fullpath(absbuf, relpath, maxlen):
                path_text = relpath.decode("mbcs", "replace") if relpath else ""
                rc = real_fullpath(absbuf, relpath, maxlen)
                if path_text and "modules" in path_text.lower():
                    out = None
                    if rc:
                        try:
                            out = ctypes.string_at(rc).decode("mbcs", "replace")
                        except Exception:
                            out = None
                    log.info(
                        "jvm.dll _fullpath(%s, max=%d) -> 0x%x %r",
                        path_text,
                        maxlen,
                        rc or 0,
                        out,
                    )
                return rc

            self._hooks["fullpath"] = _FULLPATH_t(_fullpath)

        # ── Module-handle resolution hooks ────────────────────────────────
        #
        # Why these matter (the actual cause of "Failed setting boot class path")
        # ────────────────────────────────────────────────────────────────────
        # OpenJDK 9+ initialises sun.boot.library.path / java.home with this
        # idiom inside jvm.dll's os::init_system_properties_values:
        #
        #     HMODULE h = nullptr;
        #     if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
        #                             (LPCWSTR)&os::init_system_properties_values,
        #                             &h)) {
        #         return;                               // ← we hit this!
        #     }
        #     GetModuleFileNameW(h, home_path, MAX_PATH);
        #
        # The address argument lives inside our in-memory jvm.dll, but the
        # OS loader has no record of that mapping — GetModuleHandleExW
        # returns FALSE, the function bails out, java.home is never set,
        # and HotSpot later panics with "Failed setting boot class path".
        #
        # The fix is to intercept GetModuleHandleEx{W,A} (and the simpler
        # GetModuleHandle{W,A} for "jvm.dll" name lookups) on jvm.dll's
        # IAT and return our fake handle whenever the query targets the
        # in-memory image.  The existing GetModuleFileNameW hook then
        # answers the follow-up call with the on-disk path.
        FLAG_FROM_ADDRESS = 0x04
        jvm_lo = self._jvm_lo
        jvm_hi = self._jvm_hi

        if self._iat.get("gmhw"):
            real_gmhw = _GMHW_t(self._iat["gmhw"].original)

            def _gmhw(name):
                try:
                    s = ctypes.c_wchar_p(name).value if name else None
                except (ValueError, OSError, TypeError):
                    s = None
                if _is_jvm_named_module_lookup(s):
                    return fake
                return real_gmhw(name)

            self._hooks["gmhw"] = _GMHW_t(_gmhw)

        if self._iat.get("gmha"):
            real_gmha = _GMHA_t(self._iat["gmha"].original)

            def _gmha(name):
                decoded = (
                    name.decode("mbcs", "replace") if name else ""
                )
                if _is_jvm_named_module_lookup(decoded):
                    return fake
                return real_gmha(name)

            self._hooks["gmha"] = _GMHA_t(_gmha)

        if self._iat.get("gmhew"):
            real_gmhew = _GMHEW_t(self._iat["gmhew"].original)

            def _gmhew(flags, name_or_addr, out_h):
                # FROM_ADDRESS branch: name_or_addr is an address.
                if flags & FLAG_FROM_ADDRESS:
                    addr = int(name_or_addr or 0)
                    if jvm_lo <= addr < jvm_hi:
                        if out_h:
                            out_h[0] = ctypes.c_void_p(fake)
                        return 1
                else:
                    # Name branch: name_or_addr is LPCWSTR.
                    try:
                        s = (
                            ctypes.c_wchar_p(name_or_addr).value
                            if name_or_addr
                            else None
                        )
                    except (ValueError, OSError):
                        s = None
                    if _is_jvm_named_module_lookup(s):
                        if out_h:
                            out_h[0] = ctypes.c_void_p(fake)
                        return 1
                return real_gmhew(flags, name_or_addr, out_h)

            self._hooks["gmhew"] = _GMHEW_t(_gmhew)

        if self._iat.get("gmhea"):
            real_gmhea = _GMHEA_t(self._iat["gmhea"].original)

            def _gmhea(flags, name_or_addr, out_h):
                if flags & FLAG_FROM_ADDRESS:
                    addr = int(name_or_addr or 0)
                    if jvm_lo <= addr < jvm_hi:
                        if out_h:
                            out_h[0] = ctypes.c_void_p(fake)
                        return 1
                else:
                    try:
                        s = (
                            ctypes.c_char_p(name_or_addr).value
                            if name_or_addr
                            else None
                        )
                        s = s.decode("mbcs", "replace") if s else None
                    except (ValueError, OSError):
                        s = None
                    if _is_jvm_named_module_lookup(s):
                        if out_h:
                            out_h[0] = ctypes.c_void_p(fake)
                        return 1
                return real_gmhea(flags, name_or_addr, out_h)

            self._hooks["gmhea"] = _GMHEA_t(_gmhea)

    # ── IAT patch/restore ──────────────────────────────────────────────────

    def _patch_iat(self) -> None:
        import _jpype  # type: ignore[import]

        spec = importlib.util.find_spec("_jpype")
        jp_path = getattr(_jpype, "__file__", None) or (spec.origin if spec else None)
        if not jp_path:
            raise RuntimeError("Cannot locate _jpype.pyd")

        k32 = ctypes.windll.kernel32
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        base = k32.GetModuleHandleW(str(jp_path))
        if not base:
            raise OSError(
                f"GetModuleHandleW failed for _jpype.pyd: {k32.GetLastError()}"
            )
        log.info("_jpype.pyd base 0x%016x", base)

        # Helper: match by function name across any imported DLL/api-set.
        # JDK builds can import loader functions from KERNEL32.DLL, KERNELBASE,
        # or api-ms-win-* aliases, and the exact alias set changes between
        # toolchains/JDK releases.  Wildcarding the DLL keeps the hooks tied to
        # the API name instead of a fragile fixed alias list.
        def _try_iat(mod_base: int, mod_path: str, fn: str) -> _IATEntry | None:
            try:
                return _find_iat(mod_base, "*", fn, mod_path)
            except Exception as exc:
                log.debug("wildcard IAT %s not found in %s: %s", fn, mod_path, exc)
                return None

        self._iat["llw"] = _try_iat(base, jp_path, "LoadLibraryW") or _find_iat(
            base, "KERNEL32.DLL", "LoadLibraryW", jp_path
        )
        self._iat["gpa"] = _try_iat(base, jp_path, "GetProcAddress") or _find_iat(
            base, "KERNEL32.DLL", "GetProcAddress", jp_path
        )

        self._iat["jp_lla"] = _try_iat(base, jp_path, "LoadLibraryA")
        self._iat["jp_llexw"] = _try_iat(base, jp_path, "LoadLibraryExW")
        self._iat["jp_gmhw"] = _try_iat(base, jp_path, "GetModuleHandleW")
        self._iat["jp_fl"] = _try_iat(base, jp_path, "FreeLibrary")

        # Compute jvm.dll's in-memory range so GetModuleHandleEx FROM_ADDRESS
        # can be answered without false positives.  SizeOfImage lives at
        # OptionalHeader+56 (4 bytes, both PE32 and PE32+).  If the read
        # fails for any reason we fall back to a 64 MiB window — jvm.dll on
        # OpenJDK 25 is ~16 MiB, so this safely covers the whole image.
        jvm_base = self._fake
        try:
            lfa = ctypes.c_int32.from_address(jvm_base + 0x3C).value
            opt = jvm_base + lfa + 24
            sz = ctypes.c_uint32.from_address(opt + 56).value or (64 << 20)
        except OSError:
            sz = 64 << 20
        self._jvm_lo = jvm_base
        self._jvm_hi = jvm_base + sz
        log.info("jvm.dll image range [0x%016x, 0x%016x)", self._jvm_lo, self._jvm_hi)

        # Optional jvm.dll IAT hooks.  GetModuleHandle{Ex}{W,A} are the new
        # entries that fix "Failed setting boot class path" on JDK 9+.
        jvm_path = str(self._loader.jvm_dll_path)
        for key, fn in [
            ("gmfa", "GetModuleFileNameA"),
            ("gmfw", "GetModuleFileNameW"),
            ("gmhw", "GetModuleHandleW"),
            ("gmha", "GetModuleHandleA"),
            ("gmhew", "GetModuleHandleExW"),
            ("gmhea", "GetModuleHandleExA"),
            ("llw2", "LoadLibraryW"),
            ("lla", "LoadLibraryA"),
            ("llexw", "LoadLibraryExW"),
            ("jgpa", "GetProcAddress"),
            ("fl", "FreeLibrary"),
            ("stat", "_stat64i32"),
            ("gfaew", "GetFileAttributesExW"),
            ("gfpw", "GetFullPathNameW"),
            ("fffw", "FindFirstFileW"),
            ("cfw", "CreateFileW"),
            ("gfaa", "GetFileAttributesA"),
            ("fffa", "FindFirstFileA"),
            ("fullpath", "_fullpath"),
        ]:
            ent = _try_iat(jvm_base, jvm_path, fn)
            if ent is None:
                log.debug("Optional IAT %s not found in jvm.dll imports", fn)
            self._iat[key] = ent

        self._make_hooks()

        self._iat["llw"].patch(ctypes.cast(self._hooks["llw"], ctypes.c_void_p).value)
        self._iat["gpa"].patch(ctypes.cast(self._hooks["gpa"], ctypes.c_void_p).value)
        for key in ("jp_lla", "jp_llexw", "jp_gmhw", "jp_fl"):
            if self._iat.get(key) and self._hooks.get(key):
                self._iat[key].patch(
                    ctypes.cast(self._hooks[key], ctypes.c_void_p).value
                )
                log.info("  hooked _jpype IAT: %s", key)
        for key in (
            "gmfa",
            "gmfw",
            "gmhw",
            "gmha",
            "gmhew",
            "gmhea",
            "llw2",
            "lla",
            "llexw",
            "jgpa",
            "fl",
            "stat",
            "gfaew",
            "gfpw",
            "fffw",
            "cfw",
            "gfaa",
            "fffa",
            "fullpath",
        ):
            if self._iat.get(key) and self._hooks.get(key):
                self._iat[key].patch(
                    ctypes.cast(self._hooks[key], ctypes.c_void_p).value
                )
                log.info("  hooked jvm.dll IAT: %s", key)

        log.info("IAT hooks installed")
        _flush_log()

    def _restore_iat(self) -> None:
        for entry in self._iat.values():
            if entry:
                try:
                    entry.restore()
                except Exception as exc:
                    log.warning("IAT restore failed: %s", exc)
        self._hooks.clear()
        log.info("IAT hooks restored")

    def _run_jvm_dllmain(self) -> None:
        """
        Manually invoke jvm.dll's DllMain(DLL_PROCESS_ATTACH).

        PythonMemoryModule skips jvm.dll's entry point by default because
        HotSpot's DllMain calls GetModuleHandleExW to discover its own HMODULE.
        At load time our IAT hooks are not yet installed, so that call fails
        and the entry point returns FALSE, aborting the load.

        We defer the call until *after* _patch_iat() has redirected
        GetModuleHandleExW / GetModuleFileNameW inside jvm.dll's IAT.  With
        those hooks in place DllMain succeeds and initialises internal CRT /
        JVM globals that JNI_CreateJavaVM requires.
        """
        jvm_mod = self._loader.jvm_module
        pmm = jvm_mod.pythonmemorymodule
        if pmm.contents.initialized:
            log.info("jvm.dll DllMain already ran (initialized=1)")
            return

        codebase = jvm_mod._codebaseaddr
        entry_rva = pmm.contents.headers.contents.OptionalHeader.AddressOfEntryPoint
        if entry_rva == 0:
            log.warning("jvm.dll has no entry point; skipping manual DllMain")
            return

        entry_addr = codebase + entry_rva
        DllMain = _DLLMAIN_t(entry_addr)
        log.info(
            "Calling jvm.dll DllMain @ 0x%016x (DLL_PROCESS_ATTACH)", entry_addr
        )
        ok = DllMain(ctypes.c_void_p(codebase), 1, ctypes.c_void_p(0))
        if not ok:
            raise RuntimeError(
                "jvm.dll DllMain(DLL_PROCESS_ATTACH) returned FALSE"
            )
        pmm.contents.initialized = 1
        log.info("jvm.dll DllMain completed successfully")

    # ── boot ──────────────────────────────────────────────────────────────

    def boot(self) -> None:
        log.info("=== JvmBootGlue.boot() ===")
        _enable_crash_log()
        _flush_log()
        try:
            self._patch_iat()
            self._run_jvm_dllmain()
            import jpype  # noqa: F401
            import jpype.imports

            _pjvm = str(self._loader.jvm_dll_path)
            log.info("jpype.startJVM(%s)", _pjvm)
            jpype.startJVM(
                _pjvm,
                *self._flags,
                classpath=self._classpath,
                convertStrings=True,
                interrupt=True,
            )
        finally:
            self._restore_iat()

        import jpype

        log.info("Loading main class: %s", self._main)
        try:
            jpype.JClass(self._main).main(self._gargs[:])
            _wait_non_daemon_threads()
        except Exception as exc:
            log.error("Java main() raised %s: %s", type(exc).__name__, exc)
            raise
        log.info("=== JvmBootGlue.boot() done ===")


# ══════════════════════════════════════════════════════════════════════════════
# § 5b  JVM path enforcement — pin java.home / sun.boot.library.path
#
# Why this exists
# ───────────────
# When jvm.dll is mapped from RAM (option 5 / memory_resident=True), HotSpot's
# startup code calls GetModuleFileNameA/W on its own HMODULE to derive the
# JDK home directory.  For a VirtualAlloc'd module that handle is not known
# to the OS loader and the call returns an empty string.  HotSpot then fails
# to locate the runtime image at <java.home>/lib/modules and aborts with:
#
#     Error occurred during initialization of VM
#     Could not find or load module image: lib/modules
#
# The IAT hooks installed in § 5 cover *some* of HotSpot's GetModuleFileName
# call sites, but other code paths read -Djava.home / -Dsun.boot.library.path
# straight from the JavaVMInitArgs.  When portablemc emits a -Djava.home that
# points at *its own* downloaded JDK (different from the one whose jvm.dll
# bytes we actually mapped) HotSpot walks into the wrong tree and crashes the
# same way.
#
# Solution
# ────────
# Strip every conflicting -D path property from the supplied flag list and
# replace them with authoritative values pinned to the JDK that owns the
# in-memory jvm.dll.  A pre-flight check confirms <java.home>/lib/modules
# exists so we fail fast with a clear error instead of an opaque HotSpot
# panic.  The same overrides are also applied to the OS-loader path
# (option 4) — they are harmless there and eliminate "wrong runtime"
# crashes when the caller's JDK_BIN does not match portablemc's java.home.
# ══════════════════════════════════════════════════════════════════════════════

# -D system properties whose values must be controlled by us, not by
# portablemc, the user environment, or whatever launcher script invoked us.
# Anything starting with one of these prefixes is stripped from the flag
# list before our authoritative replacements are appended.
_PINNED_PROPS: tuple[str, ...] = (
    "-Djava.home=",
    "-Dsun.boot.library.path=",
    "-Djava.library.path=",
    "-Djava.endorsed.dirs=",
    "-Djava.ext.dirs=",
)

# JVM-relevant environment variables that silently merge extra command-line
# flags at startup.  We unset them while booting so they cannot inject
# conflicting -Djava.home / -Xbootclasspath values behind our back.
_SCRUBBED_ENV_VARS: tuple[str, ...] = (
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
)

# HotSpot's Windows port checks this internal override before calling
# os::jvm_path() / GetModuleFileName(vm_lib_handle).  Setting it while
# bootstrapping a memory-mapped JVM avoids lib/modules lookup failures if the
# OS loader cannot describe our VirtualAlloc'd jvm.dll image.
_HOTSPOT_ALT_JAVA_HOME = "_ALT_JAVA_HOME_DIR"


def _java_home_from_bin(jdk_bin: Path) -> Path:
    """
    Return the JDK installation root for a path that ends in ``bin/``.

    HotSpot's <java.home> is the directory *containing* bin/, not bin/ itself.
    For an OpenJDK 25 layout that means:

        jdk_bin   = .../jdk-25.0.3+9/bin
        java.home = .../jdk-25.0.3+9
    """
    bin_dir = Path(jdk_bin).resolve()
    if bin_dir.name.lower() == "bin":
        return bin_dir.parent
    # Non-canonical layout: fall back to the parent and let the runtime-image
    # check below produce the diagnostic.
    return bin_dir.parent


def _verify_runtime_image(java_home: Path, *, fatal: bool) -> bool:
    """
    Confirm ``<java.home>/lib/modules`` exists.  This is the JImage file that
    contains every class in the JDK's modular runtime — without it, HotSpot
    cannot start.

    Parameters
    ----------
    java_home
        Computed JDK root (parent of bin/).
    fatal
        When True (memory-resident mode) raise FileNotFoundError on miss.
        When False (OS-loader mode) emit a warning and let the caller proceed.
    """
    modules_jimage = java_home / "lib" / "modules"
    if modules_jimage.is_file():
        log.info("runtime image OK   : %s", modules_jimage)
        return True

    msg = (
        f"JDK runtime image not found: {modules_jimage}\n"
        "  java.home is computed as the parent of jdk_bin and must contain\n"
        "  lib/modules — the JImage archive HotSpot loads at startup.\n"
        "  Either jdk_bin does not point at a real JDK 9+ installation,\n"
        "  or its bin/ directory was renamed/moved."
    )
    if fatal:
        raise FileNotFoundError(msg)
    log.warning(
        "runtime image MISSING: %s (continuing — OS loader will retry)", modules_jimage
    )
    return False


def _enforce_jvm_path_overrides(
    jvm_flags: list[str],
    jdk_bin: Path,
    *,
    require_runtime_image: bool,
) -> list[str]:
    """
    Return a flag list with authoritative -Djava.home, -Dsun.boot.library.path
    and -Djava.library.path values pinned to the JDK whose jvm.dll bytes were
    actually mapped (or whose path was passed to jpype.startJVM).  Conflicting
    -D properties already present in ``jvm_flags`` are removed first.

    The returned list contains the overrides BOTH at the front and at the end:
      * Front-loading guards against early HotSpot init code that reads the
        first occurrence.
      * Tail-duplicating guarantees the override wins under HotSpot's
        "last -D wins" semantics regardless of how the args are merged
        further downstream (e.g. by JPype before JNI_CreateJavaVM).
    """
    java_home = _java_home_from_bin(jdk_bin)
    _verify_runtime_image(java_home, fatal=require_runtime_image)

    # Strip every flag whose key is in _PINNED_PROPS — we will replace them.
    filtered: list[str] = []
    dropped: list[str] = []
    for f in jvm_flags:
        if any(f.startswith(p) for p in _PINNED_PROPS):
            dropped.append(f)
        else:
            filtered.append(f)

    if dropped:
        log.info("Stripped %d conflicting -D path properties:", len(dropped))
        for f in dropped:
            log.info("  ✗  %s", f)

    # Compose -Djava.library.path: prepend jdk_bin to whatever the caller asked
    # for so the JDK's own native libs win, but keep mod native dirs (lwjgl,
    # game-specific natives, etc.) reachable for System.loadLibrary().
    user_libpath = ""
    for f in jvm_flags:
        if f.startswith("-Djava.library.path="):
            user_libpath = f[len("-Djava.library.path=") :]
            break

    libpath_parts: list[str] = [str(jdk_bin)]
    for p in user_libpath.split(os.pathsep):
        p = p.strip()
        if p and p not in libpath_parts:
            libpath_parts.append(p)
    java_library_path = os.pathsep.join(libpath_parts)

    overrides = [
        f"-Djava.home={java_home}",
        f"-Dsun.boot.library.path={jdk_bin}",
        f"-Djava.library.path={java_library_path}",
    ]

    log.info("Pinned -Djava.home              = %s", java_home)
    log.info("Pinned -Dsun.boot.library.path  = %s", jdk_bin)
    log.info("Pinned -Djava.library.path      = %s", java_library_path)

    # Front + tail duplication — see docstring.
    return [*overrides, *filtered, *overrides]


def _augment_dll_search_path(jdk_bin: Path) -> list[object]:
    """
    Make ``jdk_bin`` and ``jdk_bin/server`` discoverable by the OS loader for
    the lifetime of the returned cookies.

    Even in memory-resident mode, several auxiliary JDK libraries (jawt.dll,
    sunmscapi.dll, awt.dll, …) are loaded by the JVM through the regular OS
    loader after JNI_CreateJavaVM completes.  AddDllDirectory provides a
    narrowly-scoped search list without polluting PATH.

    Returns
    -------
    list[object]
        AddDllDirectory cookies — keep them alive for the lifetime of the
        JVM.  When the list is GC'd the directories are removed automatically.
    """
    cookies: list[object] = []
    for d in (jdk_bin, jdk_bin / "server"):
        if not d.is_dir():
            continue
        try:
            cookies.append(os.add_dll_directory(str(d)))  # type: ignore[attr-defined]
            log.debug("Added DLL search dir: %s", d)
        except (FileNotFoundError, OSError, AttributeError) as exc:
            log.debug("add_dll_directory(%s) failed: %s", d, exc)
    return cookies


def _wptr_to_str(ptr) -> str:
    """Decode a raw wide-char pointer (c_void_p int) to Python str, safely."""
    if not ptr:
        return ""
    try:
        # Some IAT callbacks pass a c_void_p object; ctypes.wstring_at expects
        # an integer address (or c_wchar_p). Force address extraction.
        return ctypes.wstring_at(int(ptr))
    except Exception:
        return ""


def _set_process_env(name: str, value: str | None) -> None:
    """Set/delete an environment variable in both Python and Win32 state."""
    k32 = ctypes.windll.kernel32
    k32.SetEnvironmentVariableW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    k32.SetEnvironmentVariableW.restype = ctypes.wintypes.BOOL
    if value is None:
        os.environ.pop(name, None)
        if not k32.SetEnvironmentVariableW(name, None):
            log.debug(
                "SetEnvironmentVariableW(%s, NULL) failed: %s", name, k32.GetLastError()
            )
    else:
        os.environ[name] = value
        if not k32.SetEnvironmentVariableW(name, value):
            log.debug(
                "SetEnvironmentVariableW(%s) failed: %s", name, k32.GetLastError()
            )


def _ucrt_getenv(name: str) -> str | None:
    """Read getenv() from the UCRT API set that HotSpot also imports."""
    try:
        crt = ctypes.CDLL("api-ms-win-crt-environment-l1-1-0.dll")
        crt.getenv.argtypes = [ctypes.c_char_p]
        crt.getenv.restype = ctypes.c_char_p
        raw = crt.getenv(name.encode("ascii"))
        return raw.decode("mbcs", "replace") if raw else None
    except Exception as exc:
        log.debug("UCRT getenv(%s) probe failed: %s", name, exc)
        return None


class _ScrubJvmEnv:
    """
    Context manager that temporarily clears JVM-injecting environment
    variables (JAVA_TOOL_OPTIONS, _JAVA_OPTIONS, JDK_JAVA_OPTIONS) and pins
    JAVA_HOME to the directory the launcher chose.

    HotSpot reads these variables at startup and silently merges their
    contents into its argument list — they can override our pinned -D
    properties.  Restoring them on exit keeps the change scoped to the
    JNI_CreateJavaVM call.
    """

    def __init__(self, java_home: Path) -> None:
        self._java_home = str(java_home)
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "_ScrubJvmEnv":
        # Snapshot + clear injectors.
        for name in _SCRUBBED_ENV_VARS:
            self._saved[name] = os.environ.get(name)
            if self._saved[name]:
                log.info("Scrubbing %s=%r for JVM boot", name, self._saved[name])
            _set_process_env(name, None)
        # Pin JAVA_HOME so child processes (rare but possible: native tools
        # spawned by mods) inherit the correct one.
        self._saved["JAVA_HOME"] = os.environ.get("JAVA_HOME")
        _set_process_env("JAVA_HOME", self._java_home)

        # OpenJDK/HotSpot Windows-specific escape hatch used by
        # os::init_system_properties_values().  This is read before HotSpot
        # tries GetModuleFileName(vm_lib_handle), so it protects the
        # memory-resident path even if the module handle cannot be resolved by
        # the OS loader.
        self._saved[_HOTSPOT_ALT_JAVA_HOME] = os.environ.get(_HOTSPOT_ALT_JAVA_HOME)
        _set_process_env(_HOTSPOT_ALT_JAVA_HOME, self._java_home)
        log.info(
            "Pinned %s=%s for HotSpot bootstrap (UCRT getenv sees: %r)",
            _HOTSPOT_ALT_JAVA_HOME,
            self._java_home,
            _ucrt_getenv(_HOTSPOT_ALT_JAVA_HOME),
        )
        return self

    def __exit__(self, *_exc) -> None:
        for name, val in self._saved.items():
            _set_process_env(name, val)


# ══════════════════════════════════════════════════════════════════════════════
# § 6  JDK detection helpers
# ══════════════════════════════════════════════════════════════════════════════


def _jdk_bin_from_java_exe(java_exe: str | Path) -> Path | None:
    """
    Given the path to java.exe, return the parent bin/ directory if
    bin/server/jvm.dll exists beside it.
    """
    try:
        bin_dir = Path(java_exe).resolve().parent
        jvm_dll = bin_dir / "server" / "jvm.dll"
        if jvm_dll.exists():
            return bin_dir
        # Some JRE layouts put jvm.dll directly in bin/
        if (bin_dir / "jvm.dll").exists():
            return bin_dir
    except Exception:
        pass
    return None


def _jdk_bin_from_args(args: list[str]) -> Path | None:
    """Extract JDK bin dir from a Java launch arg list (args[0] = java.exe)."""
    if not args:
        return None
    # args[0] is java.exe
    result = _jdk_bin_from_java_exe(args[0])
    if result:
        return result
    # Also look for -Djava.home=...
    for arg in args:
        if arg.startswith("-Djava.home="):
            home = Path(arg[len("-Djava.home=") :])
            candidate = home / "bin"
            if (candidate / "server" / "jvm.dll").exists():
                return candidate
    return None


def find_portablemc_jdk(base_dir: Path) -> Path | None:
    """
    Search portablemc's jvm/ and runtime/ cache dirs for a JDK whose
    bin/server/jvm.dll exists.  Returns the bin/ directory on success.
    """
    search_roots = [
        base_dir / "jvm",
        base_dir / "runtime",
        base_dir / "jre",
    ]
    for root in search_roots:
        if not root.exists():
            continue
        # Sort by path depth so we prefer shallower (non-nested) distributions
        for java_exe in sorted(root.rglob("java.exe"), key=lambda p: len(p.parts)):
            if java_exe.parent.name != "bin":
                continue
            jvm_dll = java_exe.parent / "server" / "jvm.dll"
            if jvm_dll.exists():
                log.info("Found portablemc JDK: %s", java_exe.parent)
                return java_exe.parent
    return None


# ══════════════════════════════════════════════════════════════════════════════
# § 7  JDK installer  (downloads Adoptium JRE to LOCALAPPDATA if needed)
# ══════════════════════════════════════════════════════════════════════════════


def install_jdk(dest_dir: Path, java_version: int = 21) -> Path | None:
    """
    Download Eclipse Temurin JRE (x64 Windows) to dest_dir and return its
    bin/ directory, or None on failure.

    Uses the Adoptium REST API:
      GET /v3/binary/latest/<version>/ga/windows/x64/jre/hotspot/normal/eclipse
    """
    import shutil
    import urllib.request
    import zipfile

    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = find_portablemc_jdk(dest_dir)
    if existing:
        return existing

    url = (
        f"https://api.adoptium.net/v3/binary/latest/{java_version}/ga"
        f"/windows/x64/jre/hotspot/normal/eclipse"
    )
    zip_path = dest_dir / f"temurin-{java_version}-jre-windows-x64.zip"
    log.info("Downloading Adoptium Temurin JRE %d → %s", java_version, zip_path)
    print(f"  📥 Downloading Temurin JRE {java_version} from Adoptium…")

    try:
        import ssl  # type: ignore[import]

        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(url, context=ctx, timeout=120) as r:
            with open(zip_path, "wb") as f:
                shutil.copyfileobj(r, f)
    except Exception as exc:
        log.warning("certifi download failed (%s), retrying without verify", exc)
        try:
            ctx2 = ssl._create_unverified_context()
            with urllib.request.urlopen(url, context=ctx2, timeout=120) as r:
                with open(zip_path, "wb") as f:
                    shutil.copyfileobj(r, f)
        except Exception as exc2:
            log.error("JDK download failed: %s", exc2)
            zip_path.unlink(missing_ok=True)
            return None

    log.info("Extracting JRE…")
    print("  📦 Extracting…")
    extract_dir = dest_dir / f"temurin-{java_version}-jre"
    extract_dir.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)
        zip_path.unlink(missing_ok=True)
    except Exception as exc:
        log.error("JRE extraction failed: %s", exc)
        return None

    result = find_portablemc_jdk(extract_dir)
    if result:
        log.info("JRE ready: %s", result)
        print(f"  ✅ JRE installed: {result}")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# § 8  portablemc adapter — direct 5.x API resolution
# ══════════════════════════════════════════════════════════════════════════════


class PortableMCGameAdapter:
    """
    Calls portablemc to install a Minecraft version and returns the four
    components needed by JvmBootGlue:

        (jvm_flags, classpath, main_class, game_args)

    Also auto-detects the JDK bin dir from portablemc's downloaded Java and
    exposes it via the ``detected_jdk_bin`` property.
    """

    def __init__(
        self,
        main_dir: Path,
        version: str,
        jdk_bin: Path,
        username: str = "Player",
        access_token: str = "0",
        extra_jvm_flags: list[str] | None = None,
    ) -> None:
        self.main_dir = main_dir
        self.version = version
        self.jdk_bin = jdk_bin
        self.username = username
        self.access_token = access_token
        self.extra_jvm_flags = extra_jvm_flags or []
        self.detected_jdk_bin: Path | None = None  # set after resolve()

    # ── public ────────────────────────────────────────────────────────────

    def resolve(
        self, *, inmemory: bool = False
    ) -> tuple[list[str], list[str], str, list[str]]:
        """
        Run the adapter and return (jvm_flags, classpath, main_class, game_args).

        Parameters
        ----------
        inmemory : bool
            If True, attempt to load portablemc's native extension in-memory
            using pythonmemorymodule.  This is useful for environments with
            strict .exe/.dll execution policies.
        """
        # Step 1: Ensure portablemc environment is initialized
        if not _ensure_portablemc_environment(inmemory=inmemory):
            raise RuntimeError(
                "portablemc 5.x environment initialization failed.\n"
                "Ensure the vendored portablemc tree exists at:\n"
                f"  {_SCRIPTS_DIR / 'portablemc' / 'portablemc-py' / 'python'}\n"
                "Or ensure the wheel is available at:\n"
                f"  {_SCRIPTS_DIR / 'portablemc' / 'target' / 'wheels'}"
            )

        # Step 2: Try the Python API
        try:
            result = self._via_python_api()
            mode = "in-memory" if is_portablemc_inmemory() else "standard"
            log.info("portablemc resolved via: Python API (%s mode)", mode)
            return result
        except ImportError as exc:
            # More specific error for import failures
            raise RuntimeError(
                f"portablemc 5.x module import failed: {exc}\n"
                "The native extension (_portablemc.pyd) may not be compatible "
                "with this Python version.\n"
                "Expected location: "
                f"{_SCRIPTS_DIR / 'portablemc' / 'portablemc-py' / 'python' / 'portablemc'}"
            ) from exc
        except AttributeError as exc:
            # API mismatch - wrong portablemc version
            raise RuntimeError(
                f"portablemc API mismatch: {exc}\n"
                "This launcher requires portablemc 5.x with PyO3 bindings.\n"
                "Ensure you have the correct version of the native extension."
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"portablemc 5.x API resolution failed: {type(exc).__name__}: {exc}\n"
                "Check that the vendored portablemc installation is complete "
                "and the native extension is compatible with this Python version."
            ) from exc

    # ── path 1: portablemc 5.x PyO3 API ──────────────────────────────────
    def _build_installer(self):
        version_text = self.version.strip()
        colon = version_text.find(":")
        if colon >= 0:
            prefix = version_text[:colon].lower()
            suffix = version_text[colon + 1 :].strip()
        else:
            prefix = ""
            suffix = version_text

        with _CleanPath():
            if prefix in ("fabric", "quilt", "legacyfabric", "babric"):
                from portablemc.fabric import (  # type: ignore[import]
                    Installer as FabricInstaller,
                    Loader as FabricLoader,
                    GameVersion as FabricGameVersion,
                )

                loader_map = {
                    "fabric": FabricLoader.Fabric,
                    "quilt": FabricLoader.Quilt,
                    "legacyfabric": FabricLoader.LegacyFabric,
                    "babric": FabricLoader.Babric,
                }
                game_version = (
                    FabricGameVersion.Stable
                    if not suffix or suffix == "latest"
                    else suffix
                )
                installer = FabricInstaller(loader_map[prefix], game_version)
            elif prefix in ("forge", "neoforge"):
                from portablemc.forge import (  # type: ignore[import]
                    Installer as ForgeInstaller,
                    Loader as ForgeLoader,
                    Version as ForgeVersion,
                )

                loader = (
                    ForgeLoader.Forge if prefix == "forge" else ForgeLoader.NeoForge
                )
                if not suffix or suffix == "latest":
                    raise RuntimeError(
                        f"Forge/NeoForge needs explicit game version (got {self.version!r})"
                    )
                installer = ForgeInstaller(loader, ForgeVersion.Stable(suffix))
            else:
                from portablemc.mojang import Installer as MojangInstaller  # type: ignore[import]

                installer = MojangInstaller(suffix if suffix else version_text)

        return installer

    @staticmethod
    def _extract_command_spec(game) -> tuple[list[str], str | None]:
        command_factory = game.command()

        partial_args = getattr(command_factory, "args", ())
        partial_kwargs = getattr(command_factory, "keywords", None) or {}
        raw_args = partial_args[0] if partial_args else None
        cwd = partial_kwargs.get("cwd")

        if raw_args is None:
            raise RuntimeError("portablemc 5.x API did not expose launch args")

        if isinstance(raw_args, str):
            full_args = raw_args.split()
        elif isinstance(raw_args, (list, tuple)):
            full_args = [str(arg) for arg in raw_args]
        else:
            raise RuntimeError(
                f"portablemc 5.x API returned unsupported arg type: {type(raw_args)!r}"
            )

        if not full_args:
            raise RuntimeError("portablemc 5.x API returned empty launch args")

        return full_args, (str(cwd) if cwd is not None else None)

    def _via_python_api(self) -> tuple:
        installer = self._build_installer()
        installer.set_main_dir(str(self.main_dir))
        installer.launcher_name = "portablemc"
        try:
            set_username = getattr(installer, "set_auth_offline_username", None)
            if callable(set_username):
                set_username(self.username)
        except Exception:
            log.debug("portablemc installer rejected offline username override")

        game = installer.install()
        full_args, command_cwd = self._extract_command_spec(game)
        if command_cwd:
            log.debug("portablemc command cwd: %s", command_cwd)

        exe = Path(str(full_args[0])).name.lower()
        if exe not in ("java.exe", "java", "javaw.exe", "javaw"):
            raise RuntimeError(
                f"portablemc 5.x API returned non-java command: {full_args[0]!r}"
            )

        jdk = _jdk_bin_from_args(full_args)
        if jdk:
            self.detected_jdk_bin = jdk
            log.info("Auto-detected JDK from Python API: %s", jdk)
        else:
            log.warning("Could not auto-detect JDK from Python API launch args")
        return _ArgSplitter.split(full_args[1:])

    # ── static helpers ──────────────────────────────────────��──────────────

    @staticmethod
    def _extract_args(jvm_args, game_args, main_class):
        return _ArgSplitter.extract(jvm_args, game_args, main_class)


# ══════════════════════════════════════════════════════════════════════════════
# § 9  Argument parsing helpers
# ══════════════════════════════════════════════════════════════════════════════


class _ArgSplitter:
    """Split flat JVM arg lists into (jvm_flags, classpath, main_class, game_args)."""

    GAME_MARKERS = {
        "--username",
        "--version",
        "--gameDir",
        "--assetsDir",
        "--assetIndex",
        "--uuid",
        "--accessToken",
        "--userType",
        "--versionType",
        "--quickPlayPath",
        "--quickPlaySingleplayer",
        "--quickPlayMultiplayer",
        "--quickPlayRealms",
        "--server",
        "--port",
    }

    @classmethod
    def split(cls, args: list[str]) -> tuple:
        """Split flat args (no java.exe) into four components."""
        return cls.extract(args, [], "")

    @classmethod
    def extract(
        cls,
        jvm_args: list[str],
        game_args: list[str],
        main_class: str,
    ) -> tuple[list[str], list[str], str, list[str]]:
        classpath: list[str] = []
        pure_flags: list[str] = []
        skip = False

        # First pass: extract classpath from jvm_args
        args_iter = list(jvm_args)
        i = 0
        while i < len(args_iter):
            arg = args_iter[i]
            if skip:
                skip = False
                i += 1
                continue
            if arg in ("-cp", "-classpath"):
                if i + 1 < len(args_iter):
                    classpath = args_iter[i + 1].split(os.pathsep)
                    skip = True
            elif arg.startswith("-Djava.class.path="):
                classpath = arg[len("-Djava.class.path=") :].split(os.pathsep)
            elif (
                arg.startswith("-D")
                or arg.startswith("-X")
                or arg.startswith("-ea")
                or arg.startswith("-da")
            ):
                pure_flags.append(arg)
            else:
                # Could be main class or game arg
                if not arg.startswith("-"):
                    if "." in arg and not arg.endswith(".jar"):
                        if not main_class:
                            main_class = arg
                            # Everything after main_class is a game arg
                            game_args = args_iter[i + 1 :]
                            break
                    else:
                        pure_flags.append(arg)
                else:
                    pure_flags.append(arg)
            i += 1

        log.info(
            "Extracted: %d JVM flags, %d classpath entries, main=%r, %d game args",
            len(pure_flags),
            len(classpath),
            main_class,
            len(game_args),
        )
        return pure_flags, classpath, main_class, list(game_args)


class _DryOutputParser:
    """Multi-strategy parser for portablemc --dry output."""

    @classmethod
    def parse(cls, raw: str) -> tuple | None:
        # Strategy 1: JSON lines
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if obj.get("event") == "jvm_args":
                args = obj.get("args") or []
                if isinstance(args, list) and len(args) >= 2:
                    return _ArgSplitter.split(args)

            for evt in ("game_launch", "game_start", "jvm_start", "jvm"):
                if obj.get("event") == evt:
                    jvm = obj.get("jvm_args") or obj.get("args") or []
                    mc = obj.get("main_class", "")
                    ga = obj.get("game_args") or []
                    if isinstance(jvm, list) and jvm:
                        return _ArgSplitter.extract(jvm, ga, mc)

            for key in ("args", "command", "jvm_args"):
                val = obj.get(key)
                if isinstance(val, list) and "-cp" in val:
                    return _ArgSplitter.split(val)

        # Strategy 2: "Arguments:" text block
        args = cls._text_block(raw)
        if args and "-cp" in args:
            return _ArgSplitter.split(args)

        # Strategy 3: tab-delimited machine format
        args = cls._tab_machine(raw)
        if args:
            return _ArgSplitter.split(args)

        return None

    @staticmethod
    def _text_block(raw: str) -> list[str] | None:
        lines = raw.splitlines()
        in_block = False
        args: list[str] = []
        for line in lines:
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
            if not in_block:
                if re.search(r"\barguments?\b\s*:", clean, re.IGNORECASE):
                    in_block = True
                continue
            if not clean:
                if args:
                    break
                continue
            if re.match(r"^\[", clean) and re.search(r"\]\s+\S", clean[:30]):
                break
            arg = clean.lstrip("| \t")
            if arg:
                args.append(arg)
        return args if len(args) >= 3 and "-cp" in args else None

    @staticmethod
    def _tab_machine(raw: str) -> list[str] | None:
        args: list[str] = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if (
                len(parts) >= 3
                and parts[0].strip() == "jvm_args"
                and parts[1].strip() in ("additional", "args")
            ):
                val = parts[2].strip()
                if val and val.lower() not in ("arguments:", "arguments"):
                    args.append(val)
        return args if len(args) >= 3 else None


# ════════════  ═════════════════════════════════════════════════════════════════
# § 10  Top-level entry point
# ══════════════════════════════════════════════════════════════════════════════


def launch_minecraft(
    jdk_bin: Path,
    main_dir: Path,
    version: str = "fabric:latest",
    username: str = "Player",
    access_token: str = "0",
    extra_jvm_flags: list[str] | None = None,
    extra_game_args: list[str] | None = None,
    debug: bool = False,
    memory_resident: bool = False,
    portablemc_inmemory: bool = False,
) -> None:
    """
    Full in-process Minecraft launcher.  No java.exe is spawned.

    memory_resident=False  (option 4)
        jpype.startJVM with the real jvm.dll path — simple, no IAT tricks.

    memory_resident=True   (option 5)
        jvm.dll and all JDK deps mapped into RAM via JvmMemoryLoader.
        jpype.startJVM intercepted via IAT hooks.

    portablemc_inmemory=True
        Load the _portablemc.pyd extension in-memory using pythonmemorymodule
        instead of the normal LoadLibrary path.  This is useful for environments
        with strict .exe/.dll execution policies.

    Note: subprocess/os.system/external process calls are NOT used.
    Everything runs within the current Python process.
    """
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    # ── Step 1: resolve Minecraft game configuration ──────────────────────
    # Portablemc MUST run before the memory loader so we know which JDK it
    # downloaded for this version (java-runtime-epsilon for 1.21+).  Loading
    # a different JDK from memory first and then trying to override java.home
    # causes HotSpot's CRT bootstrap to abort with
    # "Failed setting boot class path" or a segfault inside JNI_CreateJavaVM.
    log.info("Resolving Minecraft version: %r", version)
    log.info("portablemc mode: %s", "in-memory" if portablemc_inmemory else "standard")
    adapter = PortableMCGameAdapter(
        main_dir=main_dir,
        version=version,
        jdk_bin=jdk_bin,
        username=username,
        access_token=access_token,
        extra_jvm_flags=extra_jvm_flags,
    )
    jvm_flags, classpath, main_class, game_args = adapter.resolve(
        inmemory=portablemc_inmemory
    )

    # Always prefer portablemc's auto-detected JDK (e.g. java-runtime-epsilon)
    # over the caller's explicit jdk_bin.  For option 5 the memory loader MUST
    # map the same JDK whose -Djava.home / classpath portablemc configured.
    # For option 4 it guarantees the OS-loaded jvm.dll matches the classpath.
    if adapter.detected_jdk_bin:
        if memory_resident:
            log.info(
                "Switching memory-loader JDK: %s → %s  (portablemc detected)",
                jdk_bin,
                adapter.detected_jdk_bin,
            )
        else:
            log.info(
                "Using portablemc-detected JDK for option 4: %s",
                adapter.detected_jdk_bin,
            )
        jdk_bin = adapter.detected_jdk_bin

    # ── Step 2: optionally load JVM from memory ───────────────────────────
    # Now that jdk_bin is definitely the JDK portablemc configured, we can
    # memory-map the correct jvm.dll (and its sibling DLLs).
    loader = None
    if memory_resident:
        from jvm_memory_loader import JvmMemoryLoader  # type: ignore[import]

        log.info("Loading JVM from memory: %s", jdk_bin)
        loader = JvmMemoryLoader(jdk_bin=jdk_bin, debug=debug).load()
        if not loader.verify():
            raise RuntimeError("JNI_CreateJavaVM verification failed")

    if extra_jvm_flags:
        jvm_flags = list(jvm_flags) + list(extra_jvm_flags)
    if extra_game_args:
        game_args = list(game_args) + list(extra_game_args)

    if not main_class:
        raise RuntimeError("portablemc did not provide a main class")
    if not classpath:
        raise RuntimeError("portablemc did not provide a classpath")

    # ── Step 2b: pin -Djava.home / -Dsun.boot.library.path / -Djava.library.path
    #
    # Run AFTER all flag merges so portablemc, extra_jvm_flags, and any -D
    # properties from JVM_BOOT_FLAGS-style env vars are normalised together.
    # In memory-resident mode lib/modules MUST exist or HotSpot will abort
    # with "Could not find or load module image: lib/modules" — fail fast.
    jvm_flags = _enforce_jvm_path_overrides(
        jvm_flags,
        jdk_bin,
        require_runtime_image=memory_resident,
    )

    # Make jdk_bin / jdk_bin/server resolvable for any auxiliary JDK DLL
    # the JVM loads through the OS loader after JNI_CreateJavaVM (jawt.dll,
    # sunmscapi.dll, awt.dll, ...).  Cookies stay alive for the run.
    _dll_search_cookies = _augment_dll_search_path(jdk_bin)  # noqa: F841

    log.info(
        "Config: main=%s  jvm_flags=%d  classpath=%d  game_args=%d",
        main_class,
        len(jvm_flags),
        len(classpath),
        len(game_args),
    )

    # ── Step 3: boot + launch ─────────────────────────────────────────────
    java_home = _java_home_from_bin(jdk_bin)

    with _ScrubJvmEnv(java_home):
        if not memory_resident:
            # Option 4: standard OS-loader JVM path
            import jpype  # noqa: F401
            import jpype.imports

            jvm_dll = str(_resolve_jdk_jvm_dll_on_disk(jdk_bin))
            if not Path(jvm_dll).is_file():
                raise FileNotFoundError(f"jvm.dll not found beside {jdk_bin}")
            log.info("startJVM (OS loader): %s", jvm_dll)
            jpype.startJVM(
                jvm_dll,
                *jvm_flags,
                classpath=classpath,
                convertStrings=True,
                interrupt=True,
            )
            log.info("Launching: %s", main_class)
            try:
                jpype.JClass(main_class).main(game_args[:])
                _wait_non_daemon_threads()
            except Exception as exc:
                log.error("Java main() raised %s: %s", type(exc).__name__, exc)
                raise
            return

        # Option 5: memory-resident JVM path
        JvmBootGlue(
            loader=loader,
            jvm_flags=jvm_flags,
            classpath=classpath,
            main_class=main_class,
            game_args=game_args,
            debug=debug,
        ).boot()


# ══════════════════════════════════════════════════════════════════════════════
# § 11  __main__ entry
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _JDK_BIN = Path(
        r"C:\Users\wave6\Downloads\OpenJDK25U-jdk_x64_windows_hotspot_25.0.3_9\jdk-25.0.3+9\bin"
    )
    _MAIN_DIR = Path(r"C:\Users\wave6\AppData\Local\PortableMC")
    launch_minecraft(
        jdk_bin=_JDK_BIN,
        main_dir=_MAIN_DIR,
        version="fabric:1.21.4",
        username="Player",
        debug=True,
    )
