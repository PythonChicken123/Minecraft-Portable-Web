"""
jvm_boot_glue.py
────────────────
Bridges the memory-resident JVM (Task 2) into the JPype runtime and
launches the Minecraft main class entirely in-process.

Architecture — three-stage boot
================================

Stage 1 · IAT Hook  (before jpype.startJVM)
    Patch two kernel32 entries in _jpype.pyd's Import Address Table:
      · LoadLibraryW   → _hook_load_library_w
      · GetProcAddress → _hook_get_proc_address
    The hooks are invisible to the rest of the process; only _jpype.pyd's
    private IAT copy is modified.

Stage 2 · Transparent JVM Creation  (inside jpype.startJVM)
    jpype.startJVM() calls _jpype.startup(jvmpath, full_args, ...)
    which calls into JPContext::startJVM() in C++:

      C++: loadEntryPoints(jvmpath)
            → LoadLibraryW(jvm.dll)           ← HOOKED → fake HINSTANCE
            → GetProcAddress(fake, "JNI_CreateJavaVM")  ← HOOKED → _trampoline_create_jvm
            → GetProcAddress(fake, "JNI_GetCreatedJavaVMs") ← HOOKED → real fn

      C++: CreateJVM_Method(&m_JavaVM, &env, jniArgs)
            → calls _trampoline_create_jvm(pvm, penv, jniArgs)
            → forwards jniArgs straight to loader.jni_create_java_vm()
            → JVM is created from in-memory jvm.dll
            → returns JNI_OK; m_JavaVM/env are valid

      C++: m_Running = true
      C++: initializeResources(env, interrupt)  ← runs normally

    Python: initializeResources()               ← runs normally

Stage 3 · In-Process Game Launch  (after jpype.startJVM)
    IAT is restored.  JPype is fully operational.
    game.main_class is loaded via jpype.JClass and .main() is called
    with the game argument list via the JPype 1.0+ [:] shorthand.

JPype 1.0+ compliance
=====================
· No JArray / JObject usage
· No manual thread attachment
· No forced shutdown
· No @JImplements — lambdas used for SAM types
· Dynamic class loading used post-JVM-start
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import faulthandler
import importlib.util
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jvm_memory_loader import JvmMemoryLoader

log = logging.getLogger(__name__)
_LOCAL_SCRIPT_DIR = Path(__file__).resolve().parent
_CRASH_LOG_FILE = None


def _flush_logging() -> None:
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _enable_native_crash_log() -> None:
    global _CRASH_LOG_FILE
    if _CRASH_LOG_FILE is not None:
        return
    try:
        crash_dir = Path.cwd() / "logs"
        crash_dir.mkdir(parents=True, exist_ok=True)
        _CRASH_LOG_FILE = (crash_dir / "python_native_crash.log").open("a", encoding="utf-8")
        _CRASH_LOG_FILE.write("\n=== jvm_boot_glue native crash capture enabled ===\n")
        _CRASH_LOG_FILE.flush()
        faulthandler.enable(file=_CRASH_LOG_FILE, all_threads=True)
    except Exception as exc:
        log.warning("Could not enable native crash log: %s", exc)


def _wait_for_java_non_daemon_threads() -> None:
    """
    Keep Python alive after Minecraft's main() returns.

    Minecraft can hand off to Java client threads and return from main().
    If Python exits immediately, JPype tears down the JVM and Minecraft stops.
    """
    import jpype

    Thread = jpype.JClass("java.lang.Thread")
    current = Thread.currentThread()
    log.info("Minecraft main() returned; waiting for Java non-daemon threads")

    while True:
        threads = list(Thread.getAllStackTraces().keySet().toArray())
        live_threads = [
            thread for thread in threads
            if thread.isAlive() and not thread.isDaemon() and thread != current
        ]
        if not live_threads:
            log.info("No Java non-daemon threads remain")
            return
        names = ", ".join(str(thread.getName()) for thread in live_threads[:8])
        log.debug("Waiting on Java threads: %s", names)
        time.sleep(1.0)


def _without_local_launcher_path(paths: list[str]) -> list[str]:
    """
    Return sys/PYTHONPATH entries with this launcher's scripts directory removed.

    This project also has scripts/portablemc.py for the Flask UI.  When that
    directory is on sys.path it shadows the installed portablemc package and can
    make `python -m portablemc` run the UI guard instead of the launcher package.
    """
    cleaned: list[str] = []
    local_scripts = str(_LOCAL_SCRIPT_DIR).casefold()
    for entry in paths:
        if not entry:
            cleaned.append(entry)
            continue
        try:
            if str(Path(entry).resolve()).casefold() == local_scripts:
                continue
        except OSError:
            pass
        cleaned.append(entry)
    return cleaned


class _CleanImportPath:
    """Temporarily hide scripts/portablemc.py while importing portablemc."""

    def __enter__(self) -> None:
        self._old_path = list(sys.path)
        sys.path[:] = _without_local_launcher_path(sys.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        sys.path[:] = self._old_path


# ══════════════════════════════════════════════════════════════════════════════
# § 1  JNI constants and vtable offsets  (from native/jni_include/jni.h)
# ══════════════════════════════════════════════════════════════════════════════

JNI_OK        =  0
JNI_ERR       = -1
JNI_EDETACHED = -2
JNI_VERSION_9 = 0x00090000     # used by JPype (USE_JNI_VERSION in jp_context.cpp)

# JNIInvokeInterface_ slot indices (0-based), each 8 bytes on x64:
#   0  reserved0
#   1  reserved1
#   2  reserved2
#   3  DestroyJavaVM
#   4  AttachCurrentThread
#   5  DetachCurrentThread
#   6  GetEnv                  ← we use this one
#   7  AttachCurrentThreadAsDaemon
_SLOT_DESTROY         = 3
_SLOT_ATTACH          = 4
_SLOT_DETACH          = 5
_SLOT_GET_ENV         = 6
_SLOT_ATTACH_DAEMON   = 7
_PTR_SIZE             = 8      # x64


# ══════════════════════════════════════════════════════════════════════════════
# § 2  WINFUNCTYPE prototypes for the IAT hooks
# ══════════════════════════════════════════════════════════════════════════════

# LoadLibraryW(LPCWSTR lpLibFileName) → HMODULE
_LoadLibraryW_t = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_wchar_p)

# LoadLibraryA(LPCSTR lpLibFileName) → HMODULE
_LoadLibraryA_t = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p)

# LoadLibraryExW(LPCWSTR lpLibFileName, HANDLE hFile, DWORD dwFlags) -> HMODULE
_LoadLibraryExW_t = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.wintypes.DWORD)

# GetProcAddress(HMODULE hModule, LPCSTR lpProcName) → FARPROC
_GetProcAddress_t = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)

# FreeLibrary(HMODULE hLibModule) → BOOL
_FreeLibrary_t = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.c_void_p)

# GetModuleFileNameA(HMODULE hModule, LPSTR lpFilename, DWORD nSize) → DWORD
_GetModuleFileNameA_t = ctypes.WINFUNCTYPE(
    ctypes.wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.wintypes.DWORD,
)

# GetModuleFileNameW(HMODULE hModule, LPWSTR lpFilename, DWORD nSize) -> DWORD
_GetModuleFileNameW_t = ctypes.WINFUNCTYPE(
    ctypes.wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.wintypes.DWORD,
)

# JNI_CreateJavaVM(JavaVM **pvm, void **penv, void *args) → jint
_JNI_CreateJavaVM_t = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),   # JavaVM **pvm
    ctypes.POINTER(ctypes.c_void_p),   # JNIEnv **penv
    ctypes.c_void_p,                    # JavaVMInitArgs *args
)

# JNIInvokeInterface::GetEnv(JavaVM*, void**, jint) → jint
_GetEnv_t = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,                    # JavaVM *vm
    ctypes.POINTER(ctypes.c_void_p),    # void **env
    ctypes.c_int,                       # jint version
)

# JNIInvokeInterface::AttachCurrentThread(JavaVM*, void**, void*) → jint
_AttachCurrentThread_t = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_void_p,
)


# ══════════════════════════════════════════════════════════════════════════════
# § 3  IAT Patcher
# ══════════════════════════════════════════════════════════════════════════════

class _IATEntry:
    """Manages a single slot in a module's Import Address Table."""

    def __init__(self, addr: int, original: int) -> None:
        self._addr = addr
        self.original = original

    def patch(self, new_addr: int) -> None:
        """Overwrite the IAT slot with a new function pointer."""
        k32 = ctypes.windll.kernel32
        old_protect = ctypes.wintypes.DWORD()
        if not k32.VirtualProtect(
            ctypes.c_void_p(self._addr),
            ctypes.c_size_t(8),
            ctypes.wintypes.DWORD(0x04),
            ctypes.byref(old_protect),
        ):
            raise OSError(f"VirtualProtect(RW) failed: {k32.GetLastError()}")

        ctypes.memmove(ctypes.c_void_p(self._addr), ctypes.byref(ctypes.c_void_p(new_addr)), 8)

        k32.VirtualProtect(
            ctypes.c_void_p(self._addr),
            ctypes.c_size_t(8),
            old_protect,
            ctypes.byref(old_protect),
        )

    def restore(self) -> None:
        """Restore the original Win32 function pointer."""
        self.patch(self.original)


def _find_iat_entry(
    module_base: int,
    target_dll: str,
    target_func: str,
    module_path: str | None = None,
) -> _IATEntry:
    """
    Parse a PE module's headers to find the IAT slot for a specific import.
    """
    log.debug("Scanning IAT of 0x%x for %s!%s", module_base, target_dll, target_func)

    nt_header_off = ctypes.cast(module_base + 0x3C, ctypes.POINTER(ctypes.c_uint32)).contents.value
    nt_header = module_base + nt_header_off

    import_dir_off = nt_header + 0x18 + 0x70 + 8
    import_rva = ctypes.cast(import_dir_off, ctypes.POINTER(ctypes.c_uint32)).contents.value
    if not import_rva:
        raise RuntimeError("Module has no import directory")

    import_desc_addr = module_base + import_rva

    while True:
        name_rva = ctypes.cast(import_desc_addr + 12, ctypes.POINTER(ctypes.c_uint32)).contents.value
        if not name_rva:
            break

        dll_name = ctypes.string_at(module_base + name_rva).decode("ascii")
        if dll_name.upper() == target_dll.upper():
            first_thunk_rva = ctypes.cast(import_desc_addr + 16, ctypes.POINTER(ctypes.c_uint32)).contents.value
            original_first_thunk_rva = ctypes.cast(import_desc_addr, ctypes.POINTER(ctypes.c_uint32)).contents.value

            thunk_ptr = module_base + first_thunk_rva
            orig_thunk_ptr = module_base + original_first_thunk_rva if original_first_thunk_rva else thunk_ptr

            while True:
                val = ctypes.cast(orig_thunk_ptr, ctypes.POINTER(ctypes.c_uint64)).contents.value
                if not val:
                    break

                if not (val & (1 << 63)):
                    func_name_addr = module_base + val + 2
                    func_name = ctypes.string_at(func_name_addr).decode("ascii")

                    if func_name == target_func:
                        current_addr = ctypes.cast(thunk_ptr, ctypes.POINTER(ctypes.c_uint64)).contents.value
                        log.debug("Found %s!%s at IAT slot 0x%x (current: 0x%x)", target_dll, target_func, thunk_ptr, current_addr)
                        return _IATEntry(thunk_ptr, current_addr)

                thunk_ptr += 8
                orig_thunk_ptr += 8

        import_desc_addr += 20

    raise RuntimeError(f"Could not find IAT entry for {target_dll}!{target_func} in module at 0x{module_base:x}")


# ══════════════════════════════════════════════════════════════════════════════
# § 4  JVM Bootstrap Glue
# ══════════════════════════════════════════════════════════════════════════════

class JvmBootGlue:
    """
    Orchestrates the JPype-to-memory-JVM transition.
    """

    def __init__(
        self,
        loader: JvmMemoryLoader,
        jvm_flags: list[str],
        classpath: list[str],
        main_class: str,
        game_args: list[str],
        debug: bool = False,
    ) -> None:
        self._loader = loader
        self._jvm_flags = jvm_flags
        self._classpath = classpath
        self._main_class = main_class
        self._game_args = game_args
        self._debug = debug

        self._iat_load_library: _IATEntry | None = None
        self._iat_get_proc: _IATEntry | None = None

        self._iat_get_module_file_name_a: _IATEntry | None = None
        self._iat_jvm_get_module_file_name_w: _IATEntry | None = None
        self._iat_jvm_load_library_a: _IATEntry | None = None
        self._iat_jvm_load_library_ex_w: _IATEntry | None = None
        self._iat_jvm_get_proc: _IATEntry | None = None
        self._iat_jvm_free_library: _IATEntry | None = None

        self._hook_llw_fn:    _LoadLibraryW_t | None   = None
        self._hook_gpa_fn:    _GetProcAddress_t | None = None
        self._hook_gmfa_fn:   _GetModuleFileNameA_t | None = None
        self._hook_jvm_gm_fn_w: _GetModuleFileNameW_t | None = None
        self._hook_jvm_lla_fn: _LoadLibraryA_t | None = None
        self._hook_jvm_llex_w_fn: _LoadLibraryExW_t | None = None
        self._hook_jvm_gpa_fn: _GetProcAddress_t | None = None
        self._hook_jvm_fl_fn: _FreeLibrary_t | None = None
        self._trampoline_fn:  _JNI_CreateJavaVM_t | None = None

        self._fake_hmod: int = loader.jvm_codebase()

    def _build_hooks(self) -> None:
        fake_hmod    = self._fake_hmod
        loader       = self._loader
        real_llw     = self._iat_load_library.original
        real_gpa     = self._iat_get_proc.original

        _real_llw = _LoadLibraryW_t(real_llw)

        def _h_load_library_w(path: str | None) -> int:
            if path and path.lower().endswith("jvm.dll"):
                log.debug("IAT hook LoadLibraryW(%r) → fake 0x%x", path, fake_hmod)
                return fake_hmod
            result = _real_llw(path)
            log.debug("IAT hook LoadLibraryW(%r) → pass-through 0x%x", path, result)
            return result

        self._hook_llw_fn = _LoadLibraryW_t(_h_load_library_w)

        _real_create = loader.jni_create_java_vm

        def _h_create_jvm(
            pvm:  ctypes.POINTER(ctypes.c_void_p),
            penv: ctypes.POINTER(ctypes.c_void_p),
            args: ctypes.c_void_p,
        ) -> int:
            log.info("Trampoline: forwarding CreateJavaVM to in-memory jvm.dll")
            rc = _real_create(pvm, penv, args)
            if rc == JNI_OK:
                log.info("  JavaVM*  = 0x%016x", pvm[0] if pvm[0] else 0)
                log.info("  JNIEnv*  = 0x%016x", penv[0] if penv[0] else 0)
            else:
                log.error("  JNI_CreateJavaVM returned %d", rc)
            return rc

        self._trampoline_fn = _JNI_CreateJavaVM_t(_h_create_jvm)

        trampoline_addr = ctypes.cast(self._trampoline_fn, ctypes.c_void_p).value
        get_created_addr = ctypes.cast(loader.jni_get_created_jvms, ctypes.c_void_p).value
        get_init_args_addr = ctypes.cast(loader.jni_get_default_init_args, ctypes.c_void_p).value

        _dispatch: dict[bytes, int] = {
            b"JNI_CreateJavaVM":            trampoline_addr,
            b"JNI_GetCreatedJavaVMs":       get_created_addr,
            b"JNI_GetDefaultJavaVMInitArgs": get_init_args_addr,
        }

        _real_gpa = _GetProcAddress_t(real_gpa)

        def _h_get_proc_address(hmod: int, name: bytes | None) -> int:
            if hmod == fake_hmod and name:
                fn_addr = _dispatch.get(name)
                if fn_addr is not None:
                    log.debug("IAT hook GetProcAddress(fake, %r) → 0x%x", name, fn_addr)
                    return fn_addr
                log.warning("IAT hook GetProcAddress(fake, %r) — unknown export → 0", name)
                return 0
            return _real_gpa(hmod, name)

        self._hook_gpa_fn = _GetProcAddress_t(_h_get_proc_address)

    def _patch_jvm_iat(self) -> None:
        """Patch memory-loaded jvm.dll APIs for JDK 25 modular loading support."""
        jvm_path = str(self._loader.jdk_bin / "server" / "jvm.dll")
        jvm_path_bytes = jvm_path.encode("mbcs", errors="replace")
        jvm_path_w = jvm_path
        fake_hmod = self._fake_hmod
        loaded_modules = self._loader.loaded_modules
        modules_by_handle = {mod._codebaseaddr: mod for mod in loaded_modules.values()}

        # ── GetModuleFileName Hooks ──────────────────────────────────────────
        self._iat_get_module_file_name_a = _find_iat_entry(fake_hmod, "KERNEL32.DLL", "GetModuleFileNameA", jvm_path)
        self._iat_jvm_get_module_file_name_w = _find_iat_entry(fake_hmod, "KERNEL32.DLL", "GetModuleFileNameW", jvm_path)
        
        real_gmfa = _GetModuleFileNameA_t(self._iat_get_module_file_name_a.original)
        real_gmfw = _GetModuleFileNameW_t(self._iat_jvm_get_module_file_name_w.original)

        def _h_get_module_file_name_a(hmod: int, buffer: int | None, size: int) -> int:
            if hmod == fake_hmod:
                if not buffer or size == 0: return 0
                payload = jvm_path_bytes[: max(0, size - 1)]
                ctypes.memmove(buffer, payload, len(payload))
                ctypes.memset(buffer + len(payload), 0, 1)
                log.info("IAT hook GetModuleFileNameA(fake_jvm) -> %s", jvm_path)
                return len(payload)
            return real_gmfa(hmod, buffer, size)

        def _h_get_module_file_name_w(hmod: int, buffer: int | None, size: int) -> int:
            if hmod == fake_hmod:
                if not buffer or size == 0: return 0
                payload_bytes = jvm_path_w.encode("utf-16le")
                bytes_to_copy = min(len(payload_bytes), (size - 1) * 2)
                ctypes.memmove(buffer, payload_bytes, bytes_to_copy)
                ctypes.memset(buffer + bytes_to_copy, 0, 2)
                log.info("IAT hook GetModuleFileNameW(fake_jvm) -> %s", jvm_path_w)
                return bytes_to_copy // 2
            return real_gmfw(hmod, buffer, size)

        self._hook_gmfa_fn = _GetModuleFileNameA_t(_h_get_module_file_name_a)
        self._hook_jvm_gm_fn_w = _GetModuleFileNameW_t(_h_get_module_file_name_w)
        self._iat_get_module_file_name_a.patch(ctypes.cast(self._hook_gmfa_fn, ctypes.c_void_p).value)
        self._iat_jvm_get_module_file_name_w.patch(ctypes.cast(self._hook_jvm_gm_fn_w, ctypes.c_void_p).value)

        # ── LoadLibrary Hooks ────────────────────────────────────────────────
        self._iat_jvm_load_library_a = _find_iat_entry(fake_hmod, "KERNEL32.DLL", "LoadLibraryA", jvm_path)
        self._iat_jvm_load_library_ex_w = _find_iat_entry(fake_hmod, "KERNEL32.DLL", "LoadLibraryExW", jvm_path)
        self._iat_jvm_get_proc = _find_iat_entry(fake_hmod, "KERNEL32.DLL", "GetProcAddress", jvm_path)
        self._iat_jvm_free_library = _find_iat_entry(fake_hmod, "KERNEL32.DLL", "FreeLibrary", jvm_path)

        real_lla = _LoadLibraryA_t(self._iat_jvm_load_library_a.original)
        real_llexw = _LoadLibraryExW_t(self._iat_jvm_load_library_ex_w.original)
        real_gpa = _GetProcAddress_t(self._iat_jvm_get_proc.original)
        real_fl = _FreeLibrary_t(self._iat_jvm_free_library.original)

        def _h_jvm_load_library_a(path: bytes | None) -> int:
            if path:
                path_text = path.decode("mbcs", errors="replace")
                name = os.path.basename(path_text).lower()
                mod = loaded_modules.get(name)
                if mod is not None:
                    log.info("IAT hook jvm.dll LoadLibraryA(%r) -> memory 0x%x", path_text, mod._codebaseaddr)
                    return mod._codebaseaddr
            return real_lla(path)

        def _h_jvm_load_library_ex_w(path: str | None, hfile: int, flags: int) -> int:
            if path:
                name = os.path.basename(path).lower()
                mod = loaded_modules.get(name)
                if mod is not None:
                    log.info("IAT hook jvm.dll LoadLibraryExW(%r, flags=0x%x) -> memory 0x%x", path, flags, mod._codebaseaddr)
                    return mod._codebaseaddr
            return real_llexw(path, hfile, flags)

        def _h_jvm_get_proc_address(hmod: int, name: bytes | None) -> int:
            mod = modules_by_handle.get(hmod)
            if mod is not None and name:
                try:
                    farproc = mod.get_proc_addr(name.decode("ascii", errors="replace"))
                    addr = ctypes.cast(farproc, ctypes.c_void_p).value
                    log.debug("IAT hook jvm.dll GetProcAddress(memory, %r) -> 0x%x", name, addr)
                    return addr or 0
                except Exception:
                    log.warning("IAT hook jvm.dll GetProcAddress(memory, %r) -> missing", name)
                    return 0
            return real_gpa(hmod, name)

        def _h_jvm_free_library(hmod: int) -> int:
            if hmod in modules_by_handle:
                log.debug("IAT hook jvm.dll FreeLibrary(memory 0x%x) -> ignored", hmod)
                return 1
            return real_fl(hmod)

        self._hook_jvm_lla_fn = _LoadLibraryA_t(_h_jvm_load_library_a)
        self._hook_jvm_llex_w_fn = _LoadLibraryExW_t(_h_jvm_load_library_ex_w)
        self._hook_jvm_gpa_fn = _GetProcAddress_t(_h_jvm_get_proc_address)
        self._hook_jvm_fl_fn = _FreeLibrary_t(_h_jvm_free_library)

        self._iat_jvm_load_library_a.patch(ctypes.cast(self._hook_jvm_lla_fn, ctypes.c_void_p).value)
        self._iat_jvm_load_library_ex_w.patch(ctypes.cast(self._hook_jvm_llex_w_fn, ctypes.c_void_p).value)
        self._iat_jvm_get_proc.patch(ctypes.cast(self._hook_jvm_gpa_fn, ctypes.c_void_p).value)
        self._iat_jvm_free_library.patch(ctypes.cast(self._hook_jvm_fl_fn, ctypes.c_void_p).value)
        log.info("IAT hooks installed in memory jvm.dll for LoadLibraryA/ExW/GetProcAddress/FreeLibrary")

    def _patch_iat(self) -> None:
        """Locate the ``_jpype.pyd`` module base, parse its IAT and write hook function pointers."""
        import _jpype
        log.info("IAT patch: locating _jpype.pyd")
        _flush_logging()
        spec = importlib.util.find_spec("_jpype")
        jpype_path = getattr(_jpype, "__file__", None) or (spec.origin if spec else None)
        if not jpype_path: raise RuntimeError("Cannot locate _jpype.pyd")

        k32 = ctypes.windll.kernel32
        module_base = k32.GetModuleHandleW(str(jpype_path))
        if not module_base:
            try: module_base = ctypes.WinDLL(jpype_path)._handle
            except OSError: module_base = 0
        if not module_base:
            raise OSError(f"GetModuleHandleW failed for {jpype_path!r}")
        
        log.info("IAT patch: _jpype.pyd base 0x%016x", module_base)
        _flush_logging()

        self._iat_load_library = _find_iat_entry(module_base, "KERNEL32.DLL", "LoadLibraryW", str(jpype_path))
        self._iat_get_proc = _find_iat_entry(module_base, "KERNEL32.DLL", "GetProcAddress", str(jpype_path))

        self._build_hooks()

        self._iat_load_library.patch(ctypes.cast(self._hook_llw_fn, ctypes.c_void_p).value)
        self._iat_get_proc.patch(ctypes.cast(self._hook_gpa_fn, ctypes.c_void_p).value)

        log.info("IAT hooks installed in _jpype.pyd")
        self._patch_jvm_iat()
        _flush_logging()

    def _restore_iat(self) -> None:
        """Restore original function pointers."""
        if self._iat_load_library: self._iat_load_library.restore()
        if self._iat_get_proc: self._iat_get_proc.restore()
        if self._iat_get_module_file_name_a: self._iat_get_module_file_name_a.restore()
        if self._iat_jvm_get_module_file_name_w: self._iat_jvm_get_module_file_name_w.restore()
        if self._iat_jvm_load_library_a: self._iat_jvm_load_library_a.restore()
        if self._iat_jvm_load_library_ex_w: self._iat_jvm_load_library_ex_w.restore()
        if self._iat_jvm_get_proc: self._iat_jvm_get_proc.restore()
        if self._iat_jvm_free_library: self._iat_jvm_free_library.restore()

        self._hook_llw_fn    = None
        self._hook_gpa_fn    = None
        self._hook_gmfa_fn   = None
        self._hook_jvm_gm_fn_w = None
        self._hook_jvm_lla_fn = None
        self._hook_jvm_llex_w_fn = None
        self._hook_jvm_gpa_fn = None
        self._hook_jvm_fl_fn = None
        self._trampoline_fn  = None
        log.info("IAT hooks removed — restored")

    def _attach_jpype(self) -> None:
        import jpype
        import jpype.imports
        jvm_dll_path = str(self._loader.jdk_bin / "server" / "jvm.dll")
        log.info("jpype.startJVM(%r, ...)", jvm_dll_path)
        jpype.startJVM(
            jvm_dll_path,
            *self._jvm_flags,
            classpath=self._classpath,
            convertStrings=True,
            interrupt=True,
        )

    def _launch_game(self) -> None:
        import jpype
        log.info("Loading main class: %s", self._main_class)
        MainClass = jpype.JClass(self._main_class)
        log.info("Invoking %s.main(%d args)", self._main_class, len(self._game_args))
        MainClass.main(self._game_args[:])
        _wait_for_java_non_daemon_threads()

    def boot(self) -> None:
        log.info("=== JvmBootGlue.boot() starting ===")
        _enable_native_crash_log()
        _flush_logging()
        try:
            self._patch_iat()
            self._attach_jpype()
        finally:
            self._restore_iat()
        self._launch_game()
        log.info("=== JvmBootGlue.boot() returned (game exited) ===")


class PortableMCGameAdapter:
    def __init__(self, main_dir: Path, version: str, jdk_bin: Path, username: str = "Player", access_token: str = "0", extra_jvm_flags: list[str] | None = None) -> None:
        self.main_dir        = main_dir
        self.version         = version
        self.jdk_bin         = jdk_bin
        self.username        = username
        self.access_token    = access_token
        self.extra_jvm_flags = extra_jvm_flags or []

    def resolve(self) -> tuple[list[str], list[str], str, list[str]]:
        try: return self._resolve_via_python_api()
        except Exception as exc:
            log.warning("portablemc Python API unavailable (%s); falling back to --dry parsing", exc)
            return self._resolve_via_dry_run()

    def _resolve_via_python_api(self) -> tuple[list[str], list[str], str, list[str]]:
        with _CleanImportPath(): from portablemc import base as pmc_base
        jvm_path = str(self.jdk_bin / "server" / "jvm.dll")
        installer = pmc_base.Installer(main_dir=str(self.main_dir), work_dir=str(self.main_dir))
        game = installer.install(version=self.version, jvm=jvm_path, username=self.username, auth_token=self.access_token)
        return self._extract_args(list(game.jvm_args), list(game.game_args), game.main_class)

    def _resolve_via_dry_run(self) -> tuple[list[str], list[str], str, list[str]]:
        import subprocess, json, re, shlex
        cmd = [sys.executable, "-m", "portablemc", "--main-dir", str(self.main_dir), "--work-dir", str(self.main_dir), "--output", "machine", "-vv", "start", "--dry", "--jvm", str(self.jdk_bin / "server" / "jvm.dll"), "-u", self.username, self.version]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=str(self.main_dir))
        raw = proc.stdout + proc.stderr
        jvm_args_raw, game_args_raw, main_class = [], [], ""
        for line in raw.splitlines():
            m = re.search(r'"event"\s*:\s*"jvm_args".*?"args"\s*:\s*(\[.*?\])', line)
            if m: jvm_args_raw = json.loads(m.group(1))
            m2 = re.search(r'"event"\s*:\s*"game_args".*?"args"\s*:\s*(\[.*?\])', line)
            if m2: game_args_raw = json.loads(m2.group(1))
            m3 = re.search(r'"event"\s*:\s*"main_class".*?"class"\s*:\s*"([^"]+)"', line)
            if m3: main_class = m3.group(1)
        if not jvm_args_raw: raise RuntimeError("dry-run produced no parseable Java command")
        return self._extract_args(jvm_args_raw, game_args_raw, main_class)

    @staticmethod
    def _extract_args(jvm_args, game_args, main_class) -> tuple[list[str], list[str], str, list[str]]:
        classpath, pure_flags, skip_next = [], [], False
        for i, arg in enumerate(jvm_args):
            if skip_next: skip_next = False; continue
            if arg in ("-cp", "-classpath"):
                if i + 1 < len(jvm_args): classpath = jvm_args[i + 1].split(os.pathsep); skip_next = True
            elif arg.startswith("-Djava.class.path="): classpath = arg[len("-Djava.class.path="):].split(os.pathsep)
            else: pure_flags.append(arg)
        if pure_flags and not pure_flags[-1].startswith("-"):
            candidate = pure_flags[-1]
            if "." in candidate:
                if not main_class: main_class = candidate
                pure_flags = pure_flags[:-1]
        return pure_flags, classpath, main_class, game_args


def launch_minecraft(jdk_bin, main_dir, version="fabric:latest", username="Player", access_token="0", extra_jvm_flags=None, extra_game_args=None, debug=False, memory_resident=False) -> None:
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s — %(message)s")
    loader = None
    if memory_resident:
        from jvm_memory_loader import JvmMemoryLoader
        log.info("Loading JVM from memory (jdk_bin=%s)", jdk_bin)
        loader = JvmMemoryLoader(jdk_bin=jdk_bin, debug=debug).load()
        if not loader.verify(): raise RuntimeError("JNI_CreateJavaVM verification failed")

    adapter = PortableMCGameAdapter(main_dir=main_dir, version=version, jdk_bin=jdk_bin, username=username, access_token=access_token, extra_jvm_flags=extra_jvm_flags)
    jvm_flags, classpath, main_class, game_args = adapter.resolve()
    if extra_jvm_flags: jvm_flags = list(jvm_flags) + list(extra_jvm_flags)
    if extra_game_args: game_args = list(game_args) + list(extra_game_args)

    if memory_resident:
        jdk_home, boot_library_path = str(jdk_bin.parent), str(jdk_bin)
        jvm_flags = [f"-Djava.home={jdk_home}", f"-Djdk.home={jdk_home}", f"-Dsun.boot.library.path={boot_library_path}", f"-Djava.library.path={boot_library_path}"] + [f for f in jvm_flags if not any(f.startswith(p) for p in ("-Djava.home=", "-Djdk.home=", "-Dsun.boot.library.path=", "-Djava.library.path="))]

    if not memory_resident:
        import jpype
        jpype.startJVM(str(jdk_bin / "server" / "jvm.dll"), *jvm_flags, classpath=classpath, convertStrings=True, interrupt=True)
        jpype.JClass(main_class).main(game_args[:])
        _wait_for_java_non_daemon_threads()
        return

    JvmBootGlue(loader=loader, jvm_flags=jvm_flags, classpath=classpath, main_class=main_class, game_args=game_args, debug=debug).boot()


if __name__ == "__main__":
    _JDK_BIN  = Path(r"C:\Users\wave6\Downloads\OpenJDK25U-jdk_x64_windows_hotspot_25.0.3_9\jdk-25.0.3+9\bin")
    _MAIN_DIR = Path(r"C:\Users\wave6\AppData\Roaming\PortableMC")
    launch_minecraft(jdk_bin=_JDK_BIN, main_dir=_MAIN_DIR, version="fabric:1.21.4")
