#!/usr/bin/env python3
"""Build the arm64 macOS perfect guard/evade runtime module."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "macos" / "parry_window" / "parry_window.c"
BUILD_DIR = ROOT / "build" / "macos-parry-window"
DIST_DIR = ROOT / "dist"
README = ROOT / "native" / "macos" / "parry_window" / "README.md"
DYLIB_NAME = "CDUMM-ParryWindow-5x-macOS.dylib"
ZIP_NAME = "CDUMM-ParryWindow-5x-macOS.zip"
PLUGIN_ID = "cdumm-parry-window-macos"


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


def _write_runtime_header(config: dict, plugin_id: str) -> Path:
    game_uuid = uuid.UUID(str(config["game_binary_uuid"]))
    signature = _signature(config, "evaluator_signature")
    header = BUILD_DIR / "runtime_config.h"
    header.write_text(
        "\n".join(
            [
                "#pragma once",
                f"#define CDUMM_SUPPORTED_UUID_BYTES "
                f"{{ {_byte_initializer(game_uuid.bytes)} }}",
                f'#define CDUMM_SUPPORTED_UUID_STRING "{str(game_uuid).upper()}"',
                f"#define CDUMM_WINDOW_VALUE_OFFSET "
                f"UINT64_C(0x{_required_int(config, 'window_value_offset'):X})",
                f"#define CDUMM_RANGE_VALUE_OFFSET "
                f"UINT64_C(0x{_required_int(config, 'range_value_offset'):X})",
                f"#define CDUMM_EVALUATOR_SIGNATURE_OFFSET "
                f"UINT64_C(0x{_required_int(config, 'evaluator_signature_offset'):X})",
                f"#define CDUMM_EVALUATOR_SIGNATURE_BYTES "
                f"{{ {_byte_initializer(signature)} }}",
                f"#define CDUMM_EVALUATOR_SIGNATURE_SIZE {len(signature)}",
                f'#define CDUMM_PLUGIN_ID_STRING "{plugin_id}"',
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
    plugin_id = str(config.get("plugin_id") or PLUGIN_ID)
    display_name = str(
        config.get("display_name") or "Perfect Guard and Evade Window 5x"
    )
    if not game_version:
        raise SystemExit("game_version is required")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", plugin_id):
        raise SystemExit("plugin_id contains unsupported characters")

    clang = shutil.which("clang")
    codesign = shutil.which("codesign")
    if sys.platform != "darwin" or not clang or not codesign:
        raise SystemExit("arm64 macOS with clang and codesign is required")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    _write_runtime_header(config, plugin_id)

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
        "name": display_name,
        "version": "1.1.0",
        "description": (
            "Native arm64 runtime adaptation for a wider perfect defense "
            "window. Installed game files are not modified."
        ),
        "game_version": game_version,
        "game_binary_uuid": game_uuid,
        "architecture": "arm64",
        "load_method": "DYLD_INSERT_LIBRARIES",
        "payload": DYLIB_NAME,
        "payload_sha256": sha256,
        "changes": {
            "just_window_fraction": {"vanilla": 0.5, "patched": 2.5},
            "evaluator_range": {"vanilla": 2.5, "patched": 12.5},
        },
        "safety": {
            "writes_game_files": False,
            "requires_exact_uuid": True,
            "requires_evaluator_signature": True,
            "unknown_values_are_overwritten": False,
        },
        "handshake": {
            "plugin_id": plugin_id,
            "required_fields": {
                "window_bits": 0x40200000,
                "range_bits": 0x41480000,
            },
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
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in (dylib, manifest_path, readme_path):
            zf.write(path, path.name)

    final_dylib = DIST_DIR / DYLIB_NAME
    shutil.copy2(dylib, final_dylib)
    print(archive)
    print(final_dylib)
    print(f"sha256={sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
