#!/usr/bin/env python3
"""Build the DGSM C ABI DLL/SO wrapper using CFFI embedding.

The library exports a small stable C ABI. On Windows, --resource-file accepts
an RC-compiled .res file so product icon/version metadata is embedded in the
DLL before Authenticode signing. The CFFI library does not bundle CPython:
a compatible runtime and the companion monitor source are required.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from cffi import FFI
from dgsm_version import __version__

MODULE_NAME = "_dgsm_embed"


def make_builder(resource_file: Path | None = None) -> FFI:
    """Create the CFFI embedding builder and optionally link a Windows .res."""
    extra_objects: list[str] = []
    if resource_file is not None:
        resource_file = resource_file.expanduser().resolve()
        if not resource_file.is_file():
            raise FileNotFoundError(f"Windows resource file does not exist: {resource_file}")
        if os.name != "nt":
            raise ValueError("A Windows .res resource can only be linked on Windows.")
        if resource_file.suffix.lower() != ".res":
            raise ValueError(f"Expected a compiled .res file, received: {resource_file}")
        # Distutils' MSVC linker accepts compiled .res files as link inputs.
        extra_objects.append(str(resource_file))

    ffibuilder = FFI()
    ffibuilder.embedding_api(
        """
        int dgsm_get_version(char *buffer, unsigned int buffer_size);
        int dgsm_get_last_error(char *buffer, unsigned int buffer_size);
        int dgsm_run_once(void);
        int dgsm_run_monitor(void);
        """
    )
    ffibuilder.set_source(
        MODULE_NAME,
        "#include <string.h>\n",
        extra_objects=extra_objects,
    )
    embedding_code = r'''
import os
import sys
import traceback
from pathlib import Path
from _dgsm_embed import ffi

_DGSM_LAST_ERROR = ""
_DGSM_VERSION = "__DGSM_VERSION__"


def _find_app_dir():
    candidates = []
    env_home = os.environ.get("DGSM_HOME")
    if env_home:
        candidates.append(Path(env_home).expanduser())
    try:
        module = sys.modules.get("_dgsm_embed")
        module_file = getattr(module, "__file__", None)
        if module_file:
            candidates.append(Path(module_file).resolve().parent)
    except Exception:
        pass
    try:
        if sys.argv and sys.argv[0]:
            candidates.append(Path(sys.argv[0]).resolve().parent)
    except Exception:
        pass
    candidates.append(Path.cwd())
    visited = set()
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
            if str(candidate) in visited:
                continue
            visited.add(str(candidate))
            if (candidate / "discord_server_global_status.py").is_file():
                return candidate
        except Exception:
            continue
    return None


def _copy_text(buffer, buffer_size, text):
    if buffer == ffi.NULL or int(buffer_size) <= 0:
        return -1
    data = str(text).encode("utf-8", errors="replace")
    size = min(len(data), int(buffer_size) - 1)
    if size:
        ffi.memmove(buffer, data, size)
    buffer[size] = b"\x00"
    return size


def _run_monitor(arguments):
    global _DGSM_LAST_ERROR
    old_argv = list(sys.argv)
    try:
        app_dir = _find_app_dir()
        if app_dir is None:
            raise FileNotFoundError(
                "Could not locate discord_server_global_status.py. Place it next to the library or set DGSM_HOME."
            )
        if str(app_dir) not in sys.path:
            sys.path.insert(0, str(app_dir))
        import discord_server_global_status as monitor
        sys.argv = [str(app_dir / "discord_server_global_status.py")] + list(arguments)
        _DGSM_LAST_ERROR = ""
        return int(monitor.main())
    except BaseException as exc:
        _DGSM_LAST_ERROR = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        return -1
    finally:
        sys.argv = old_argv


@ffi.def_extern()
def dgsm_get_version(buffer, buffer_size):
    return _copy_text(buffer, buffer_size, _DGSM_VERSION)


@ffi.def_extern()
def dgsm_get_last_error(buffer, buffer_size):
    return _copy_text(buffer, buffer_size, _DGSM_LAST_ERROR)


@ffi.def_extern()
def dgsm_run_once():
    return _run_monitor(["--once", "--no-clear"])


@ffi.def_extern()
def dgsm_run_monitor():
    return _run_monitor([])
'''.replace("__DGSM_VERSION__", __version__)
    ffibuilder.embedding_init_code(embedding_code)
    return ffibuilder


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the DGSM C ABI DLL/SO wrapper")
    parser.add_argument("--output-dir", type=Path, default=Path("dist/native"))
    parser.add_argument(
        "--resource-file",
        type=Path,
        default=None,
        help="Optional Windows RC-compiled .res file to embed in the DLL.",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = Path(__file__).resolve().parent
    source_file = source_dir / "discord_server_global_status.py"
    version_file = source_dir / "dgsm_version.py"
    if not source_file.is_file():
        parser.error(f"Python monitor source is missing: {source_file}")
    if not version_file.is_file():
        parser.error(f"Canonical version module is missing: {version_file}")

    try:
        ffibuilder = make_builder(args.resource_file)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    old_cwd = Path.cwd()
    try:
        os.chdir(output_dir)
        artifact = ffibuilder.compile(target="discord_global_status.*", verbose=True)
    finally:
        os.chdir(old_cwd)

    print(f"DGSM {__version__} native library built: {artifact}")
    print(
        f"Place {source_file.name}, {version_file.name}, and the backends/ directory "
        "beside the library or set DGSM_HOME."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
