"""
pmc_intercept.py
════════════════
Subprocess interceptor for portablemc's Java launch.

Run as:
    python pmc_intercept.py <output_json> <main_dir> <version> <username>

Patches subprocess.Popen (and subprocess.run) before importing portablemc so
that when portablemc tries to launch Java we capture the full argument list and
write it to <output_json>, then exit immediately instead of running the game.

Works with portablemc 5.x (PyO3 API via Installer) and falls back to 4.x
(pure-Python portablemc.cli) because we intercept at the OS process boundary.

Output JSON schema
──────────────────
On success:
    {"found": true, "args": ["C:/path/to/java.exe", "-Djava.home=...", ...], "cwd": "..."}

On failure:
    {"found": false, "error": "description"}
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# ── Bootstrap local portablemc FIRST, before any portablemc import ────────
_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"

# Ensure scripts/ is on sys.path so local_portablemc is importable
_ss = str(_SCRIPTS.resolve())
if _ss not in sys.path:
    sys.path.insert(0, _ss)

from local_portablemc import bootstrap as _bootstrap  # noqa: E402
_bootstrap(scripts_dir=_SCRIPTS)


def _main() -> None:
    # ── Argument check (before output_path is known) ──────────────────────
    if len(sys.argv) < 5:
        print(
            "Usage: pmc_intercept.py <output_json> <main_dir> <version> <username>",
            file=sys.stderr,
        )
        sys.exit(1)

    output_path = Path(sys.argv[1])
    main_dir = sys.argv[2]
    version = sys.argv[3]
    username = sys.argv[4]

    # Drop the bare scripts/ entry from sys.path so portablemc's own imports
    # can't accidentally re-import from there, but leave the vendor root intact.
    _scripts_resolved = str(Path(__file__).resolve().parent)
    sys.path = [
        p for p in sys.path
        if Path(p).resolve() != Path(_scripts_resolved).resolve()
    ]

    _captured = False
    _real_Popen = subprocess.Popen
    _real_run = subprocess.run

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _is_java(args_seq) -> bool:
        """Return True if args_seq[0] looks like a Java executable."""
        if not args_seq:
            return False
        name = Path(str(args_seq[0])).name.lower()
        return name in ("java.exe", "java", "javaw.exe", "javaw")

    def _write_and_exit(args_seq, cwd) -> None:
        nonlocal _captured
        if _captured:
            return
        _captured = True
        output_path.write_text(
            json.dumps({
                "found": True,
                "args": [str(a) for a in args_seq],
                "cwd": str(cwd or ""),
            }),
            encoding="utf-8",
        )
        sys.exit(0)

    def _fail(msg: str, code: int = 1) -> None:
        output_path.write_text(
            json.dumps({"found": False, "error": msg}),
            encoding="utf-8",
        )
        sys.exit(code)

    # ── Patch subprocess.Popen ────────────────────────────────────────────────

    class _CapturePopen(_real_Popen):
        def __init__(self, args, **kwargs):
            al = args if not isinstance(args, str) else args.split()
            if _is_java(al):
                _write_and_exit(al, kwargs.get("cwd"))
            super().__init__(args, **kwargs)

    # ── Patch subprocess.run ──────────────────────────────────────────────────

    def _capture_run(args, **kwargs):
        al = args if not isinstance(args, str) else args.split()
        if _is_java(al):
            _write_and_exit(al, kwargs.get("cwd"))
        return _real_run(args, **kwargs)

    subprocess.Popen = _CapturePopen
    subprocess.run = _capture_run

    # ── Run portablemc ────────────────────────────────────────────────────────
    # Try v5.x PyO3 API first, fall back to v4.x CLI if unavailable.

    try:
        # Parse version prefix (fabric:, quilt:, forge:, etc.) into the
        # correct v5.x Installer class.
        colon = version.find(":")
        if colon >= 0:
            _prefix = version[:colon].lower()
            _suffix = version[colon + 1:].strip()
        else:
            _prefix, _suffix = None, version

        if _prefix in ("fabric", "quilt", "legacyfabric", "babric"):
            from portablemc.fabric import Installer as _Inst  # type: ignore[import]
            from portablemc.fabric import Loader, GameVersion  # type: ignore[import]
            _loader_map = {
                "fabric": Loader.Fabric,
                "quilt": Loader.Quilt,
                "legacyfabric": Loader.LegacyFabric,
                "babric": Loader.Babric,
            }
            _gv = GameVersion.Stable if (not _suffix or _suffix == "latest") else _suffix
            installer = _Inst(_loader_map[_prefix], _gv)
        elif _prefix in ("forge", "neoforge"):
            from portablemc.forge import Installer as _Inst  # type: ignore[import]
            from portablemc.forge import Loader, Version as FVersion  # type: ignore[import]
            _loader = Loader.Forge if _prefix == "forge" else Loader.NeoForge
            if not _suffix or _suffix == "latest":
                _fail(f"Forge/NeoForge requires a game version (got '{version}')")
            installer = _Inst(_loader, FVersion.Stable(_suffix))
        else:
            from portablemc.mojang import Installer as _Inst  # type: ignore[import]
            installer = _Inst(_suffix if _suffix else version)

        installer.set_main_dir(main_dir)
        installer.launcher_name = "portablemc"
        game = installer.install()
        game.command()
    except SystemExit:
        pass
    except ImportError:
        # v5.x API not available, try v4.x CLI as fallback
        sys.argv = [
            "portablemc",
            "--main-dir", main_dir,
            "start",
            "-u", username,
            version,
        ]
        try:
            from portablemc.cli import main as _pmc_main  # type: ignore[import]
            _pmc_main()
        except SystemExit:
            pass
        except ImportError as exc:
            _fail(f"portablemc not importable: {exc}")
        except Exception as exc:
            _fail(f"portablemc raised: {type(exc).__name__}: {exc}")
    except Exception as exc:
        _fail(f"portablemc raised: {type(exc).__name__}: {exc}")
    finally:
        subprocess.Popen = _real_Popen
        subprocess.run = _real_run

    if not _captured:
        _fail("portablemc completed without launching java")


_main()
