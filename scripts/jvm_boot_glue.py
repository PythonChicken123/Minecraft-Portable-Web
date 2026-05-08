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
      LoadLibraryW   → fake HINSTANCE (our VirtualAlloc base)
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
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from jvm_memory_loader import JvmMemoryLoader

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────
_SCRIPTS_DIR      = Path(__file__).resolve().parent
_PMC_INTERCEPT_PY = _SCRIPTS_DIR / "pmc_intercept.py"


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
        f = (d / "python_native_crash.log").open("a", encoding="utf-8")
        f.write("\n=== jvm_boot_glue crash capture ===\n")
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
    except Exception as exc:
        log.warning("Could not enable crash log: %s", exc)


def _wait_non_daemon_threads() -> None:
    """Block until all Java non-daemon threads finish."""
    import jpype
    Thread  = jpype.JClass("java.lang.Thread")
    current = Thread.currentThread()
    log.info("Waiting for Java non-daemon threads…")
    while True:
        live = [
            t for t in Thread.getAllStackTraces().keySet().toArray()
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

JNI_OK        =  0
JNI_ERR       = -1
JNI_EDETACHED = -2
JNI_VERSION_9 = 0x00090000
_PTR          = 8    # pointer size, x64


# ══════════════════════════════════════════════════════════════════════════════
# § 3  ctypes WINFUNCTYPE prototypes
# ══════════════════════════════════════════════════════════════════════════════

_LLW_t   = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_wchar_p)
_LLA_t   = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p)
_LLEXW_t = ctypes.WINFUNCTYPE(
    ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.wintypes.DWORD)
_GPA_t   = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)
_FL_t    = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.c_void_p)
_GMFA_t  = ctypes.WINFUNCTYPE(
    ctypes.wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD)
_GMFW_t  = ctypes.WINFUNCTYPE(
    ctypes.wintypes.DWORD, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.wintypes.DWORD)
_CJVM_t  = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_void_p,
)


# ══════════════════════════════════════════════════════════════════════════════
# § 4  IAT entry — one patchable slot in a PE module's Import Address Table
# ══════════════════════════════════════════════════════════════════════════════

class _IATEntry:
    _k32 = ctypes.windll.kernel32

    def __init__(self, addr: int) -> None:
        self._addr     = addr
        self._original = ctypes.c_void_p.from_address(addr).value

    @property
    def original(self) -> int:
        return self._original

    def patch(self, fn_ptr: int) -> None:
        vp  = self._k32.VirtualProtect
        old = ctypes.wintypes.DWORD(0)
        if not vp(ctypes.c_void_p(self._addr), _PTR, 0x04, ctypes.byref(old)):
            raise OSError(f"VirtualProtect failed @ 0x{self._addr:x}: {self._k32.GetLastError()}")
        ctypes.c_void_p.from_address(self._addr).value = fn_ptr
        dummy = ctypes.wintypes.DWORD(0)
        vp(ctypes.c_void_p(self._addr), _PTR, old, ctypes.byref(dummy))

    def restore(self) -> None:
        self.patch(self._original)


def _find_iat(module_base: int, target_dll: str, target_fn: str,
              module_path: str | None = None) -> _IATEntry:
    """Locate an IAT slot by walking the in-memory PE headers (x64 only)."""
    tdll = target_dll.upper()
    tfn  = target_fn.encode("ascii")

    # Fast path: pefile
    if module_path:
        try:
            import pythonmemorymodule.pefile as _pef  # type: ignore[import]
            pe = _pef.PE(module_path, fast_load=False)
            base = pe.OPTIONAL_HEADER.ImageBase
            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
                if entry.dll.decode("ascii", errors="replace").upper() != tdll:
                    continue
                for imp in entry.imports:
                    if imp.name == tfn:
                        pe.close()
                        return _IATEntry(module_base + (imp.address - base))
            pe.close()
        except Exception:
            pass

    # Manual PE walk
    if ctypes.c_uint16.from_address(module_base).value != 0x5A4D:
        raise RuntimeError("No MZ header")
    lfa   = ctypes.c_int32.from_address(module_base + 0x3C).value
    nt    = module_base + lfa
    if ctypes.c_uint32.from_address(nt).value != 0x00004550:
        raise RuntimeError("No PE sig")
    opt   = nt + 24
    if ctypes.c_uint16.from_address(opt).value != 0x20B:
        raise RuntimeError("Not PE32+")
    imp_va   = ctypes.c_uint32.from_address(opt + 120 + 8 ).value
    imp_size = ctypes.c_uint32.from_address(opt + 120 + 12).value
    if not imp_va or not imp_size:
        raise RuntimeError("No import directory")
    desc = module_base + imp_va
    while True:
        oft = ctypes.c_uint32.from_address(desc     ).value
        nrv = ctypes.c_uint32.from_address(desc + 12).value
        ft  = ctypes.c_uint32.from_address(desc + 16).value
        if not oft and not nrv and not ft:
            break
        dname = ctypes.string_at(module_base + nrv).decode("ascii", "replace").upper()
        if dname == tdll:
            i = 0
            while True:
                tv = ctypes.c_uint64.from_address(module_base + oft + i * _PTR).value
                if not tv:
                    break
                if not (tv >> 63):
                    ibn = module_base + (tv & 0x7FFF_FFFF_FFFF_FFFF)
                    if ctypes.string_at(ibn + 2) == tfn:
                        return _IATEntry(module_base + ft + i * _PTR)
                i += 1
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
        self._loader    = loader
        self._flags     = jvm_flags
        self._classpath = classpath
        self._main      = main_class
        self._gargs     = game_args
        self._debug     = debug
        self._fake      = loader.jvm_codebase()

        # IAT entries — populated by _patch_iat
        self._iat: dict[str, _IATEntry | None] = {
            "llw": None, "gpa": None, "gmfa": None, "gmfw": None,
            "lla": None, "llexw": None, "jgpa": None, "fl": None,
        }
        # Hook callables — held alive to prevent GC
        self._hooks: dict[str, object] = {}

    # ── hook construction ──────────────────────────────────────────────────

    def _make_hooks(self) -> None:
        fake   = self._fake
        loader = self._loader
        mods   = loader.loaded_modules
        by_addr: dict[int, object] = {m._codebaseaddr: m for m in mods.values()}

        # LoadLibraryW: intercept jvm.dll → fake handle
        real_llw = _LLW_t(self._iat["llw"].original)
        def _llw(path):
            if path and path.lower().endswith("jvm.dll"):
                return fake
            return real_llw(path)
        self._hooks["llw"] = _LLW_t(_llw)

        # JNI_CreateJavaVM trampoline — forwards JPype's jniArgs to in-memory impl
        real_create = loader.jni_create_java_vm
        def _cjvm(pvm, penv, args):
            log.info("Trampoline → in-memory JNI_CreateJavaVM")
            rc = real_create(pvm, penv, args)
            if rc == JNI_OK:
                log.info("  JavaVM* 0x%016x  JNIEnv* 0x%016x",
                         pvm[0] or 0, penv[0] or 0)
            else:
                log.error("  JNI_CreateJavaVM returned %d", rc)
            return rc
        self._hooks["cjvm"] = _CJVM_t(_cjvm)

        # GetProcAddress dispatch for fake handle
        ta = ctypes.cast(self._hooks["cjvm"], ctypes.c_void_p).value
        dispatch: dict[bytes, int] = {
            b"JNI_CreateJavaVM":            ta,
            b"JNI_GetCreatedJavaVMs":       ctypes.cast(loader.jni_get_created_jvms,      ctypes.c_void_p).value,
            b"JNI_GetDefaultJavaVMInitArgs": ctypes.cast(loader.jni_get_default_init_args, ctypes.c_void_p).value,
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
        jvm_path       = str(loader.jdk_bin / "server" / "jvm.dll")
        jvm_path_b     = jvm_path.encode("mbcs", errors="replace")
        jvm_path_w     = jvm_path

        if self._iat.get("gmfa"):
            real_gmfa = _GMFA_t(self._iat["gmfa"].original)
            def _gmfa(hmod, buf, size):
                if hmod == fake:
                    if not buf or size == 0: return 0
                    pay = jvm_path_b[:max(0, size-1)]
                    ctypes.memmove(buf, pay, len(pay))
                    ctypes.memset(buf + len(pay), 0, 1)
                    return len(pay)
                return real_gmfa(hmod, buf, size)
            self._hooks["gmfa"] = _GMFA_t(_gmfa)

        if self._iat.get("gmfw"):
            real_gmfw = _GMFW_t(self._iat["gmfw"].original)
            def _gmfw(hmod, buf, size):
                if hmod == fake:
                    if not buf or size == 0: return 0
                    src = jvm_path_w[:max(0, size-1)]
                    ctypes.memmove(buf, (src+"\0").encode("utf-16-le"), (len(src)+1)*2)
                    return len(src)
                return real_gmfw(hmod, buf, size)
            self._hooks["gmfw"] = _GMFW_t(_gmfw)

        if self._iat.get("lla"):
            real_lla = _LLA_t(self._iat["lla"].original)
            def _lla(path):
                if path:
                    name = os.path.basename(path.decode("mbcs","replace")).lower()
                    m = mods.get(name)
                    if m: return m._codebaseaddr
                return real_lla(path)
            self._hooks["lla"] = _LLA_t(_lla)

        if self._iat.get("llexw"):
            real_llexw = _LLEXW_t(self._iat["llexw"].original)
            def _llexw(path, hfile, flags):
                if path:
                    name = os.path.basename(path).lower()
                    m = mods.get(name)
                    if m: return m._codebaseaddr
                return real_llexw(path, hfile, flags)
            self._hooks["llexw"] = _LLEXW_t(_llexw)

        if self._iat.get("jgpa"):
            real_jgpa = _GPA_t(self._iat["jgpa"].original)
            def _jgpa(hmod, name):
                mo = by_addr.get(hmod)
                if mo and name:
                    try:
                        fp = mo.get_proc_addr(name.decode("ascii","replace"))
                        return ctypes.cast(fp, ctypes.c_void_p).value or 0
                    except Exception:
                        return 0
                return real_jgpa(hmod, name)
            self._hooks["jgpa"] = _GPA_t(_jgpa)

        if self._iat.get("fl"):
            real_fl = _FL_t(self._iat["fl"].original)
            def _fl(hmod):
                if hmod in by_addr: return 1
                return real_fl(hmod)
            self._hooks["fl"] = _FL_t(_fl)

    # ── IAT patch/restore ──────────────────────────────────────────────────

    def _patch_iat(self) -> None:
        import _jpype  # type: ignore[import]
        spec = importlib.util.find_spec("_jpype")
        jp_path = getattr(_jpype, "__file__", None) or (spec.origin if spec else None)
        if not jp_path:
            raise RuntimeError("Cannot locate _jpype.pyd")

        k32 = ctypes.windll.kernel32
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        k32.GetModuleHandleW.restype  = ctypes.c_void_p
        base = k32.GetModuleHandleW(str(jp_path))
        if not base:
            raise OSError(f"GetModuleHandleW failed for _jpype.pyd: {k32.GetLastError()}")
        log.info("_jpype.pyd base 0x%016x", base)

        self._iat["llw"] = _find_iat(base, "KERNEL32.DLL", "LoadLibraryW",   jp_path)
        self._iat["gpa"] = _find_iat(base, "KERNEL32.DLL", "GetProcAddress", jp_path)

        # Optional jvm.dll IAT hooks
        jvm_base = self._fake
        jvm_path = str(self._loader.jdk_bin / "server" / "jvm.dll")
        for key, fn in [("gmfa","GetModuleFileNameA"), ("gmfw","GetModuleFileNameW"),
                        ("lla","LoadLibraryA"), ("llexw","LoadLibraryExW"),
                        ("jgpa","GetProcAddress"), ("fl","FreeLibrary")]:
            try:
                self._iat[key] = _find_iat(jvm_base, "KERNEL32.DLL", fn, jvm_path)
            except Exception as exc:
                log.debug("Optional IAT %s skipped: %s", fn, exc)

        self._make_hooks()

        self._iat["llw"].patch(ctypes.cast(self._hooks["llw"], ctypes.c_void_p).value)
        self._iat["gpa"].patch(ctypes.cast(self._hooks["gpa"], ctypes.c_void_p).value)
        for key, hook_key in [("gmfa","gmfa"),("gmfw","gmfw"),("lla","lla"),
                               ("llexw","llexw"),("jgpa","jgpa"),("fl","fl")]:
            if self._iat.get(key) and self._hooks.get(hook_key):
                self._iat[key].patch(ctypes.cast(self._hooks[hook_key], ctypes.c_void_p).value)

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

    # ── boot ──────────────────────────────────────────────────────────────

    def boot(self) -> None:
        log.info("=== JvmBootGlue.boot() ===")
        _enable_crash_log()
        _flush_log()
        try:
            self._patch_iat()
            import jpype, jpype.imports  # noqa: F401
            log.info("jpype.startJVM(%s)", self._loader.jdk_bin / "server" / "jvm.dll")
            jpype.startJVM(
                str(self._loader.jdk_bin / "server" / "jvm.dll"),
                *self._flags,
                classpath=self._classpath,
                convertStrings=True,
                interrupt=True,
            )
        finally:
            self._restore_iat()

        import jpype
        log.info("Loading main class: %s", self._main)
        jpype.JClass(self._main).main(self._gargs[:])
        _wait_non_daemon_threads()
        log.info("=== JvmBootGlue.boot() done ===")


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
            home = Path(arg[len("-Djava.home="):])
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
    import urllib.request, zipfile, shutil

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
        import ssl, certifi  # type: ignore[import]
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
# § 8  portablemc adapter — three resolution paths
# ══════════════════════════════════════════════════════════════════════════════

class PortableMCGameAdapter:
    """
    Calls portablemc to install a Minecraft version and returns the four
    components needed by JvmBootGlue:

        (jvm_flags, classpath, main_class, game_args)

    Also auto-detects the JDK bin dir from portablemc's downloaded Java and
    exposes it via the ``detected_jdk_bin`` property.

    Resolution order
    ────────────────
    1. portablemc 5.x PyO3 Python API   — fastest; needs _portablemc.pyd
    2. pmc_intercept.py subprocess shim — reliable with any Python portablemc
    3. --dry CLI multi-format parsing   — last resort
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
        self.main_dir        = main_dir
        self.version         = version
        self.jdk_bin         = jdk_bin
        self.username        = username
        self.access_token    = access_token
        self.extra_jvm_flags = extra_jvm_flags or []
        self.detected_jdk_bin: Path | None = None   # set after resolve()

    # ── public ────────────────────────────────────────────────────────────

    def resolve(self) -> tuple[list[str], list[str], str, list[str]]:
        """Run the adapter and return (jvm_flags, classpath, main_class, game_args)."""
        for attempt in (
            ("Python API",   self._via_python_api),
            ("pmc_intercept", self._via_intercept),
            ("--dry CLI",     self._via_dry_run),
        ):
            label, fn = attempt
            try:
                result = fn()
                log.info("portablemc resolved via: %s", label)
                return result
            except Exception as exc:
                log.warning("portablemc %s failed: %s", label, exc)

        raise RuntimeError(
            "All portablemc resolution methods failed.\n"
            "Ensure portablemc is installed:  python -m pip install portablemc"
        )

    # ── path 1: portablemc 5.x PyO3 API ──────────────────────────────────

    def _via_python_api(self) -> tuple:
        with _CleanPath():
            from portablemc._portablemc.base import Installer  # type: ignore[import]

        installer = Installer(self.version)
        installer.set_main_dir(str(self.main_dir))
        installer.launcher_name = "portablemc"

        # Intercept the Popen that game.command() would create
        import subprocess as _sp
        _captured: list[str] = []
        _real_Popen = _sp.Popen

        def _is_java(args):
            return bool(args) and Path(str(args[0])).name.lower() in (
                "java.exe", "java", "javaw.exe", "javaw")

        class _Cap(_real_Popen):
            def __init__(self_, args, **kwargs):
                al = list(args) if not isinstance(args, str) else args.split()
                if _is_java(al):
                    _captured.extend(al)
                    raise _Captured()
                super().__init__(args, **kwargs)

        class _Captured(Exception):
            pass

        _sp.Popen = _Cap
        try:
            game = installer.install()
            game.command()
        except _Captured:
            pass
        except Exception:
            raise
        finally:
            _sp.Popen = _real_Popen

        if not _captured:
            raise RuntimeError("portablemc 5.x API did not call Popen")

        jdk = _jdk_bin_from_args(_captured)
        if jdk:
            self.detected_jdk_bin = jdk
        return _ArgSplitter.split(_captured[1:])

    # ── path 2: pmc_intercept.py subprocess shim ─────────────────────────

    def _via_intercept(self) -> tuple:
        if not _PMC_INTERCEPT_PY.exists():
            raise FileNotFoundError("pmc_intercept.py not found in scripts/")

        tmp = Path(tempfile.mktemp(suffix=".json"))
        env = os.environ.copy()
        pp  = env.get("PYTHONPATH", "")
        if pp:
            env["PYTHONPATH"] = os.pathsep.join(_clean_pythonpath(pp.split(os.pathsep)))

        cmd = [
            sys.executable, str(_PMC_INTERCEPT_PY),
            str(tmp),
            str(self.main_dir),
            self.version,
            self.username,
        ]
        log.info("Running pmc_intercept: %s %s %s", self.version, self.username, self.main_dir)
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, check=False,
            cwd=_safe_cwd(self.main_dir), env=env, timeout=300,
        )
        log.debug("pmc_intercept stdout:\n%s", proc.stdout[:3000])
        log.debug("pmc_intercept stderr:\n%s", proc.stderr[:3000])

        if not tmp.exists():
            raise RuntimeError(
                f"pmc_intercept.py did not produce output (rc={proc.returncode}).\n"
                f"stderr: {proc.stderr[:1000]}"
            )

        data = json.loads(tmp.read_text(encoding="utf-8"))
        tmp.unlink(missing_ok=True)

        if not data.get("found"):
            raise RuntimeError(f"pmc_intercept: {data.get('error', 'unknown')}")

        full_args: list[str] = data["args"]   # full_args[0] = java.exe
        jdk = _jdk_bin_from_args(full_args)
        if jdk:
            self.detected_jdk_bin = jdk
            log.info("Auto-detected JDK from intercept: %s", jdk)

        return _ArgSplitter.split(full_args[1:])  # drop java.exe

    # ── path 3: --dry CLI multi-format parsing ────────────────────────────

    def _via_dry_run(self) -> tuple:
        env = os.environ.copy()
        pp  = env.get("PYTHONPATH", "")
        if pp:
            env["PYTHONPATH"] = os.pathsep.join(_clean_pythonpath(pp.split(os.pathsep)))

        base = [sys.executable, "-m", "portablemc"]
        md   = str(self.main_dir)
        cwd  = _safe_cwd(self.main_dir)
        v    = self.version
        u    = self.username

        variants = [
            # Most portablemc versions: default output
            base + ["--main-dir", md, "start", "--dry", "-u", u, v],
            # Explicit human-color
            base + ["--main-dir", md, "--output", "human-color", "start", "--dry", "-u", u, v],
            # machine output (portablemc 5.x)
            base + ["--main-dir", md, "--output", "machine",     "start", "--dry", "-u", u, v],
            # with --work-dir
            base + ["--main-dir", md, "--work-dir", md, "--output", "machine", "start", "--dry", "-u", u, v],
            # without --dry  (parse from first run; only works if Java isn't run)
            base + ["--main-dir", md, "start", "-u", u, v],
        ]

        last_raw = ""
        for cmd in variants:
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, check=False,
                    cwd=cwd, env=env, timeout=300,
                )
                raw = proc.stdout + proc.stderr
                last_raw = raw
                log.debug("dry-run variant %s… (%d chars)", " ".join(cmd[3:6]), len(raw))
                if not raw.strip():
                    continue
                result = _DryOutputParser.parse(raw)
                if result:
                    jf, cp, mc, ga = result
                    if cp or mc:
                        return jf, cp, mc, ga
            except subprocess.TimeoutExpired:
                log.warning("Variant timed out: %s", " ".join(cmd[:5]))
            except Exception as exc:
                log.warning("Variant error: %s", exc)

        raise RuntimeError(
            f"--dry parsing failed.\nLast output:\n{last_raw[:3000]}"
        )

    # ── static helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _extract_args(jvm_args, game_args, main_class):
        return _ArgSplitter.extract(jvm_args, game_args, main_class)


# ══════════════════════════════════════════════════════════════════════════════
# § 9  Argument parsing helpers
# ══════════════════════════════════════════════════════════════════════════════

class _ArgSplitter:
    """Split flat JVM arg lists into (jvm_flags, classpath, main_class, game_args)."""

    GAME_MARKERS = {
        "--username", "--version", "--gameDir", "--assetsDir",
        "--assetIndex", "--uuid", "--accessToken", "--userType",
        "--versionType", "--quickPlayPath", "--quickPlaySingleplayer",
        "--quickPlayMultiplayer", "--quickPlayRealms",
        "--server", "--port",
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
        classpath:  list[str] = []
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
                classpath = arg[len("-Djava.class.path="):].split(os.pathsep)
            elif arg.startswith("-D") or arg.startswith("-X") or arg.startswith("-ea") or arg.startswith("-da"):
                pure_flags.append(arg)
            else:
                # Could be main class or game arg
                if not arg.startswith("-"):
                    if "." in arg and not arg.endswith(".jar"):
                        if not main_class:
                            main_class = arg
                            # Everything after main_class is a game arg
                            game_args = args_iter[i + 1:]
                            break
                    else:
                        pure_flags.append(arg)
                else:
                    pure_flags.append(arg)
            i += 1

        log.info(
            "Extracted: %d JVM flags, %d classpath entries, main=%r, %d game args",
            len(pure_flags), len(classpath), main_class, len(game_args),
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
                    jvm  = obj.get("jvm_args") or obj.get("args") or []
                    mc   = obj.get("main_class", "")
                    ga   = obj.get("game_args") or []
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
            if (len(parts) >= 3
                    and parts[0].strip() == "jvm_args"
                    and parts[1].strip() in ("additional", "args")):
                val = parts[2].strip()
                if val and val.lower() not in ("arguments:", "arguments"):
                    args.append(val)
        return args if len(args) >= 3 else None


# ══════════════════════════════════════════════════════════════════════════════
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
) -> None:
    """
    Full in-process Minecraft launcher.  No java.exe is spawned.

    memory_resident=False  (option 4)
        jpype.startJVM with the real jvm.dll path — simple, no IAT tricks.

    memory_resident=True   (option 5)
        jvm.dll and all JDK deps mapped into RAM via JvmMemoryLoader.
        jpype.startJVM intercepted via IAT hooks.
    """
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    # ── Step 1: optionally load JVM from memory ───────────────────────────
    loader = None
    if memory_resident:
        from jvm_memory_loader import JvmMemoryLoader  # type: ignore[import]
        log.info("Loading JVM from memory: %s", jdk_bin)
        loader = JvmMemoryLoader(jdk_bin=jdk_bin, debug=debug).load()
        if not loader.verify():
            raise RuntimeError("JNI_CreateJavaVM verification failed")

    # ── Step 2: resolve Minecraft game configuration ──────────────────────
    log.info("Resolving Minecraft version: %r", version)
    adapter = PortableMCGameAdapter(
        main_dir=main_dir, version=version, jdk_bin=jdk_bin,
        username=username, access_token=access_token,
        extra_jvm_flags=extra_jvm_flags,
    )
    jvm_flags, classpath, main_class, game_args = adapter.resolve()

    # Use JDK detected by portablemc if the caller's path had no jvm.dll
    if adapter.detected_jdk_bin and not (jdk_bin / "server" / "jvm.dll").exists():
        jdk_bin = adapter.detected_jdk_bin
        log.info("Using auto-detected JDK: %s", jdk_bin)
        if loader:
            loader.jdk_bin = jdk_bin
            loader.jdk_server = jdk_bin / "server"

    if extra_jvm_flags:
        jvm_flags = list(jvm_flags) + list(extra_jvm_flags)
    if extra_game_args:
        game_args = list(game_args) + list(extra_game_args)

    if not main_class:
        raise RuntimeError("portablemc did not provide a main class")
    if not classpath:
        raise RuntimeError("portablemc did not provide a classpath")

    log.info(
        "Config: main=%s  jvm_flags=%d  classpath=%d  game_args=%d",
        main_class, len(jvm_flags), len(classpath), len(game_args),
    )

    # ── Step 3: boot + launch ─────────────────────────────────────────────
    if not memory_resident:
        # Option 4: standard OS-loader JVM path
        import jpype, jpype.imports  # noqa: F401
        jvm_dll = str(jdk_bin / "server" / "jvm.dll")
        log.info("startJVM (OS loader): %s", jvm_dll)
        jpype.startJVM(
            jvm_dll, *jvm_flags, classpath=classpath,
            convertStrings=True, interrupt=True,
        )
        log.info("Launching: %s", main_class)
        jpype.JClass(main_class).main(game_args[:])
        _wait_non_daemon_threads()
        return

    # Option 5: memory-resident JVM path
    JvmBootGlue(
        loader=loader, jvm_flags=jvm_flags, classpath=classpath,
        main_class=main_class, game_args=game_args, debug=debug,
    ).boot()


# ══════════════════════════════════════════════════════════════════════════════
# § 11  __main__ entry
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _JDK_BIN  = Path(r"C:\Users\wave6\Downloads\OpenJDK25U-jdk_x64_windows_hotspot_25.0.3_9\jdk-25.0.3+9\bin")
    _MAIN_DIR = Path(r"C:\Users\wave6\AppData\Local\PortableMC")
    launch_minecraft(
        jdk_bin=_JDK_BIN, main_dir=_MAIN_DIR,
        version="fabric:1.21.4", username="Player", debug=True,
    )
