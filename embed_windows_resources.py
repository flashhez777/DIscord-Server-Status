#!/usr/bin/env python3
"""Apply an RC-compiled .res file to a Windows PE binary using UpdateResourceW.

This is a deterministic post-link resource step for CFFI-generated DLLs. It
works even when a setuptools/CFFI linker backend does not forward .res inputs
as native MSVC resource objects. Authenticode signing must happen afterwards.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
import struct
import sys
from typing import Union

ResourceId = Union[int, str]


@dataclass(frozen=True)
class Resource:
    resource_type: ResourceId
    name: ResourceId
    language: int
    data: bytes


def _align4(value: int) -> int:
    return (value + 3) & ~3


def _read_identifier(blob: bytes, offset: int, limit: int) -> tuple[ResourceId, int]:
    if offset + 2 > limit:
        raise ValueError("Truncated .res resource identifier")
    first = struct.unpack_from("<H", blob, offset)[0]
    if first == 0xFFFF:
        if offset + 4 > limit:
            raise ValueError("Truncated ordinal resource identifier")
        ordinal = struct.unpack_from("<H", blob, offset + 2)[0]
        return ordinal, offset + 4

    # Non-ordinal identifiers are NUL-terminated UTF-16LE strings.
    cursor = offset
    while cursor + 2 <= limit:
        if blob[cursor:cursor + 2] == b"\x00\x00":
            raw = blob[offset:cursor]
            try:
                value = raw.decode("utf-16le")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Invalid UTF-16 resource identifier: {exc}") from exc
            return value, cursor + 2
        cursor += 2
    raise ValueError("Unterminated UTF-16 resource identifier in .res file")


def parse_res_file(path: Path) -> list[Resource]:
    """Parse the concatenated RESOURCEHEADER records emitted by rc.exe."""
    blob = path.read_bytes()
    if len(blob) < 32:
        raise ValueError("Resource file is too small to contain a valid .res header")

    resources: list[Resource] = []
    offset = 0
    record_index = 0
    while offset < len(blob):
        if offset + 8 > len(blob):
            raise ValueError(f"Truncated .res record header at offset {offset}")
        data_size, header_size = struct.unpack_from("<II", blob, offset)
        if header_size < 8 or header_size > len(blob) - offset:
            raise ValueError(
                f"Invalid .res header size {header_size} at offset {offset}"
            )

        header_end = offset + header_size
        cursor = offset + 8
        resource_type, cursor = _read_identifier(blob, cursor, header_end)
        name, cursor = _read_identifier(blob, cursor, header_end)
        cursor = _align4(cursor)
        if cursor + 16 > header_end:
            raise ValueError(
                f"Truncated additional resource header at record {record_index}"
            )
        # DataVersion (DWORD), MemoryFlags (WORD), LanguageId (WORD),
        # Version (DWORD), Characteristics (DWORD).
        _data_version, _memory_flags, language, _version, _characteristics = (
            struct.unpack_from("<IHHII", blob, cursor)
        )
        data_start = header_end
        data_end = data_start + data_size
        if data_end > len(blob):
            raise ValueError(
                f"Resource data overruns .res file at record {record_index}"
            )

        # rc.exe emits an empty ordinal-zero dummy resource as the first entry.
        if data_size and not (resource_type == 0 and name == 0):
            resources.append(
                Resource(resource_type, name, int(language), blob[data_start:data_end])
            )

        offset = _align4(data_end)
        record_index += 1

    if not resources:
        raise ValueError("No actual resources were found in the .res file")
    return resources



def validate_required_resources(resources: list[Resource]) -> None:
    """Require standard Win32 resource identities before modifying the PE."""
    if not any(item.resource_type == 16 and item.name == 1 for item in resources):
        raise ValueError(
            "The .res file has no standard RT_VERSION resource with numeric "
            "name ordinal 1. Use '1 VERSIONINFO' in the RC script; an "
            "unexpanded VS_VERSION_INFO token becomes a string name and is "
            "invisible to normal Windows version-resource lookup."
        )
    if not any(item.resource_type == 14 and item.name == 1 for item in resources):
        raise ValueError("The .res file has no RT_GROUP_ICON resource with numeric name ordinal 1")
    if not any(item.resource_type == 3 for item in resources):
        raise ValueError("The .res file contains no RT_ICON image resources")

def _identifier_pointer(value: ResourceId):
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"Resource ordinal out of range: {value}")
        return ctypes.c_void_p(value), None
    if not isinstance(value, str):
        raise TypeError(f"Unsupported resource identifier type: {type(value)!r}")
    storage = ctypes.create_unicode_buffer(value)
    return ctypes.cast(storage, ctypes.c_void_p), storage


def apply_resources(dll_path: Path, resources: list[Resource]) -> None:
    """Add or replace resources in a PE file using Win32 UpdateResource APIs."""
    if sys.platform != "win32":
        raise OSError("PE resource injection requires Windows and Win32 UpdateResourceW")
    if not dll_path.is_file():
        raise FileNotFoundError(dll_path)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    begin = kernel32.BeginUpdateResourceW
    begin.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    begin.restype = wintypes.HANDLE
    update = kernel32.UpdateResourceW
    update.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.WORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    update.restype = wintypes.BOOL
    end = kernel32.EndUpdateResourceW
    end.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    end.restype = wintypes.BOOL

    handle = begin(str(dll_path.resolve()), False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    committed = False
    try:
        for index, resource in enumerate(resources, start=1):
            type_ptr, type_storage = _identifier_pointer(resource.resource_type)
            name_ptr, name_storage = _identifier_pointer(resource.name)
            payload = (ctypes.c_ubyte * len(resource.data)).from_buffer_copy(resource.data)
            payload_ptr = ctypes.cast(payload, ctypes.c_void_p)
            ok = update(
                handle,
                type_ptr,
                name_ptr,
                resource.language,
                payload_ptr,
                len(resource.data),
            )
            # Keep Unicode identifier buffers alive until UpdateResource returns.
            _ = type_storage, name_storage
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
        if not end(handle, False):
            raise ctypes.WinError(ctypes.get_last_error())
        committed = True
    finally:
        if not committed:
            # Discard pending updates; never leave a partially modified DLL.
            end(handle, True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", required=True, type=Path)
    parser.add_argument("--resource-file", required=True, type=Path)
    parser.add_argument(
        "--parse-only", action="store_true",
        help="Validate and report the .res records without modifying a PE file.",
    )
    args = parser.parse_args()

    try:
        resources = parse_res_file(args.resource_file)
        validate_required_resources(resources)
        print(f"Parsed {len(resources)} Windows resources from {args.resource_file}")
        for item in resources:
            print(
                f"  type={item.resource_type!r} name={item.name!r} "
                f"lang={item.language} bytes={len(item.data)}"
            )
        if args.parse_only:
            return 0
        apply_resources(args.dll, resources)
        print(f"Applied {len(resources)} resources to {args.dll}")
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"Resource embedding failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())