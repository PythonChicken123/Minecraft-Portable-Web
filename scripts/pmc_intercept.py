"""
pmc_intercept.py
════════════════
Subprocess interceptor for portablemc's Java launch.

Run as:
    python pmc_intercept.py <output_json> <main_dir> <version> <username>

Patches subprocess.Popen (and subprocess.run) before importing portablemc so
that when portablemc tries to launch Java we capture the full argument list and
write it to <output_json>, then exit immediately instead of running the game.

Works with any portablemc Python version (4.x pure-Python or 5.x PyO3 wrapper)
because we intercept at the OS process boundary, not at the Python API level.

Output JSON schema
──────────────────
On success:
    {"found": true, "args": ["C:/path/to/java.exe", "-Djava.home=...", ...], "cwd": "..."}

On failure:
    {"found": false, "error": "description"}
"""

from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path


def _main() -> None:
    if len(sys.argv) < 5:
        _fail("Usage: pmc_intercept.py <output_json> <main_dir> <version> <username>")

    output_path = Path(sys.argv[1])
    main_dir    = sys.argv[2]
    version     = sys.argv[3]
    username    = sys.argv[4]

    # ── Remove scripts/ from sys.path so 'portablemc' resolves to the installed
    # pip package, not scripts/portablemc.py (the Flask web UI launcher). ──────
    _scripts = str(Path(__file__).resolve().parent)
    sys.path = [
        p for p in sys.path
        if Path(p).resolve() != Path(_scripts).resolve()
    ]

    _captured = False
    _real_Popen = subprocess.Popen
    _real_run   = subprocess.run

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
                "cwd":  str(cwd or ""),
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
    subprocess.run   = _capture_run

    # ── Run portablemc ────────────────────────────────────────────────────────

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
    finally:
        subprocess.Popen = _real_Popen
        subprocess.run   = _capture_run  # intentionally left patched until after except

    if not _captured:
        _fail("portablemc completed without launching java")


_main()
