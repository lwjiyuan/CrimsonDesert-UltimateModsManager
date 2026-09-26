#!/usr/bin/env python3
"""Build the arm64 macOS Just Take runtime module."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "macos" / "just_take" / "just_take.c"
README = ROOT / "native" / "macos" / "just_take" / "README.md"
BUILD_DIR = ROOT / "build" / "macos-just-take"
DIST_DIR = ROOT / "dist"
DYLIB_NAME = "CDUMM-JustTake-macOS.dylib"
ZIP_NAME = "CDUMM-JustTake-macOS.zip"
PLUGIN_ID = "cdumm-just-take-macos"


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def _required_int(config: dict, key: str) -> int:
    value = config.get(key)
    if isinstance(value, str):
        return int(value, 0)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be an integer or integer string")


def _signature(config: dict, key: str) -> bytes:
    value = config.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a hexadecimal string")
    result = bytes.fromhex(value)
    if not result:
        raise ValueError(f"{key} must not be empty")
    return result


def _byte_initializer(data: bytes) -> str:
    return ", ".join(f"0x{value:02X}" for value in data)


def _write_runtime_header(config: dict) -> Path:
    game_uuid = uuid.UUID(str(config["game_binary_uuid"]))
    signature = _signature(config, "target_signature")
    header = BUILD_DIR / "runtime_config.h"
    header.write_text(
        "\n".join(
            [
                "#pragma once",
                f"#define CDUMM_SUPPORTED_UUID_BYTES "
                f"{{ {_byte_initializer(game_uuid.bytes)} }}",
                f'#define CDUMM_SUPPORTED_UUID_STRING "{str(game_uuid).upper()}"',
                f"#define CDUMM_TARGET_OFFSET "
                f"UINT64_C(0x{_required_int(config, 'target_offset'):X})",
                f"#define CDUMM_TARGET_SIGNATURE_BYTES "
                f"{{ {_byte_initializer(signature)} }}",
                f"#define CDUMM_TARGET_SIGNATURE_SIZE {len(signature)}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return header


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    game_version = str(config.get("game_version") or "").strip()
    game_uuid = str(uuid.UUID(str(config["game_binary_uuid"]))).upper()
    if not game_version:
        raise SystemExit("game_version is required")

    clang = shutil.which("clang")
    codesign = shutil.which("codesign")
    if sys.platform != "darwin" or not clang or not codesign:
        raise SystemExit("arm64 macOS with clang and codesign is required")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    _write_runtime_header(config)

    dylib = BUILD_DIR / DYLIB_NAME
    run(
        clang,
        "-dynamiclib",
        "-arch",
        "arm64",
        "-std=c11",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-mmacosx-version-min=14.0",
        "-install_name",
        f"@rpath/{DYLIB_NAME}",
        "-I",
        str(BUILD_DIR),
        str(SOURCE),
        "-o",
        str(dylib),
    )
    run(codesign, "--force", "--sign", "-", str(dylib))

    sha256 = hashlib.sha256(dylib.read_bytes()).hexdigest()
    manifest = {
        "format": "cdumm-macos-runtime-v1",
        "name": "Theft Without Crime",
        "description": (
            "Uses an ARM64 hardware breakpoint for a supported theft path "
            "without rewriting signed code pages or installed game files."
        ),
        "version": "1.1.0",
        "game_version": game_version,
        "game_binary_uuid": game_uuid,
        "architecture": "arm64",
        "load_method": "DYLD_INSERT_LIBRARIES",
        "payload": DYLIB_NAME,
        "payload_sha256": sha256,
        "changes": {
            "steal_side_effect_result": {
                "patched": 0,
            }
        },
        "handshake": {
            "plugin_id": PLUGIN_ID,
            "required_fields": {
                "patched_result": 0,
            },
        },
        "safety": {
            "writes_game_files": False,
            "requires_exact_uuid": True,
            "requires_instruction_signature": True,
            "unknown_builds_are_overwritten": False,
            "production_text_patch_enabled": False,
            "uses_hardware_breakpoint": True,
        },
    }

    manifest_path = BUILD_DIR / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    readme_path = BUILD_DIR / "README.txt"
    readme_path.write_text(README.read_text(encoding="utf-8"), encoding="utf-8")

    archive = DIST_DIR / ZIP_NAME
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for path in (dylib, manifest_path, readme_path):
            package.write(path, path.name)

    final_dylib = DIST_DIR / DYLIB_NAME
    shutil.copy2(dylib, final_dylib)
    print(archive)
    print(final_dylib)
    print(f"sha256={sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
