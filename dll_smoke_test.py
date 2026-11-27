#!/usr/bin/env python3
"""Validate the DGSM C ABI exports of a freshly built native library."""

from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument("--expected-version", default=None)
    args = parser.parse_args()

    library_path = args.library.resolve()
    if not library_path.is_file():
        parser.error(f"Native library not found: {library_path}")

    try:
        library = ctypes.CDLL(str(library_path))
        get_version = library.dgsm_get_version
        get_version.argtypes = [ctypes.POINTER(ctypes.c_char), ctypes.c_uint]
        get_version.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(128)
        written = get_version(buffer, len(buffer))
        if written < 0:
            raise RuntimeError(f"dgsm_get_version returned {written}")
        version = buffer.value.decode("utf-8", errors="replace")
        if not version:
            raise RuntimeError("dgsm_get_version returned an empty version")
        if written != len(version.encode("utf-8")):
            raise RuntimeError(
                f"dgsm_get_version length mismatch: returned {written}, "
                f"encoded version length {len(version.encode('utf-8'))}"
            )
        if args.expected_version and version != args.expected_version:
            raise RuntimeError(
                f"DLL version mismatch: expected {args.expected_version}, received {version}"
            )

        get_error = library.dgsm_get_last_error
        get_error.argtypes = [ctypes.POINTER(ctypes.c_char), ctypes.c_uint]
        get_error.restype = ctypes.c_int
        error_buffer = ctypes.create_string_buffer(512)
        error_written = get_error(error_buffer, len(error_buffer))
        if error_written < 0:
            raise RuntimeError(f"dgsm_get_last_error returned {error_written}")

        # Validate function presence and signatures without launching the
        # monitor or starting network activity during a build smoke test.
        for export_name in ("dgsm_run_once", "dgsm_run_monitor"):
            export = getattr(library, export_name)
            export.argtypes = []
            export.restype = ctypes.c_int

        print(f"DGSM native library: {library_path}")
        print(f"C ABI version: {version}")
        print("Exports: dgsm_get_version, dgsm_get_last_error, dgsm_run_once, dgsm_run_monitor")
        print("DLL smoke test: PASS")
        return 0
    except (OSError, AttributeError, RuntimeError) as exc:
        print(f"DLL smoke test: FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
