"""Install and stage native macOS runtime plugins outside the game bundle.

Runtime plugins are dylibs loaded by ``DYLD_INSERT_LIBRARIES``.  They live
under CDMods/runtime and never replace a file in the signed game app.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import re
import shutil
import struct
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cdumm.storage.database import Database


FORMAT = "cdumm-macos-runtime-v1"
DISABLED_SUFFIX = ".disabled"


@dataclass(frozen=True)
class MacRuntimePlugin:
    name: str
    version: str
    directory: Path
    payload: Path
    enabled: bool
    manifest: dict


def _safe_directory_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")
    return value or "runtime-plugin"


def _plugin_directory_name(manifest: dict, fallback: str) -> str:
    handshake = manifest.get("handshake")
    plugin_id = (
        handshake.get("plugin_id") if isinstance(handshake, dict) else None
    )
    return _safe_directory_name(
        plugin_id if isinstance(plugin_id, str) and plugin_id else fallback
    )


def _runtime_description(manifest: dict) -> str:
    description = manifest.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    changes = manifest.get("changes") or {}
    if (
        "just_window_fraction" in changes
        and "evaluator_range" in changes
    ):
        return (
            "Native macOS runtime mod that expands the perfect guard and "
            "evade window without modifying installed game files."
        )
    return (
        "Native macOS runtime mod loaded only for CDUMM-managed launches. "
        "Installed game files are not modified."
    )


def _normalized_uuid(value: object) -> str:
    return str(value or "").replace("-", "").upper()


class MacRuntimePluginManager:
    """Manage verified runtime packages in a dedicated CDMods directory."""

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = Path(runtime_dir)

    @staticmethod
    def is_package(path: Path) -> bool:
        if not path.is_file() or path.suffix.lower() != ".zip":
            return False
        try:
            with zipfile.ZipFile(path) as archive:
                if "manifest.json" not in archive.namelist():
                    return False
                manifest = json.loads(archive.read("manifest.json"))
                return manifest.get("format") == FORMAT
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
            return False

    @staticmethod
    def _read_manifest(archive: zipfile.ZipFile) -> dict:
        names = archive.namelist()
        if "manifest.json" not in names:
            raise ValueError("runtime package is missing manifest.json")
        if any(
            Path(name).is_absolute() or ".." in Path(name).parts
            for name in names
        ):
            raise ValueError("runtime package contains an unsafe path")

        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != FORMAT:
            raise ValueError("unsupported macOS runtime package format")
        payload = manifest.get("payload")
        if not isinstance(payload, str) or Path(payload).name != payload:
            raise ValueError("runtime payload must be a root-level file")
        if not payload.lower().endswith(".dylib") or payload not in names:
            raise ValueError("runtime payload dylib is missing")
        if manifest.get("architecture") != "arm64":
            raise ValueError("runtime payload architecture must be arm64")
        if manifest.get("load_method") != "DYLD_INSERT_LIBRARIES":
            raise ValueError("unsupported runtime payload load method")
        expected = manifest.get("payload_sha256")
        actual = hashlib.sha256(archive.read(payload)).hexdigest()
        if not isinstance(expected, str) or actual.lower() != expected.lower():
            raise ValueError("runtime payload SHA-256 mismatch")
        try:
            manifest["game_binary_uuid"] = str(
                uuid.UUID(str(manifest["game_binary_uuid"]))
            ).upper()
        except (KeyError, ValueError, AttributeError) as exc:
            raise ValueError(
                "runtime manifest has no valid game binary UUID"
            ) from exc
        return manifest

    def install(self, package: Path) -> MacRuntimePlugin:
        """Verify and install one package. Existing same-name installs update."""
        package = Path(package)
        with zipfile.ZipFile(package) as archive:
            manifest = self._read_manifest(archive)
            name = str(manifest.get("name") or package.stem)
            target = self.runtime_dir / _plugin_directory_name(manifest, name)
            self.runtime_dir.mkdir(parents=True, exist_ok=True)

            with tempfile.TemporaryDirectory(
                prefix=".runtime-install-", dir=self.runtime_dir
            ) as temporary:
                staging = Path(temporary)
                payload_name = str(manifest["payload"])
                for member in ("manifest.json", payload_name, "README.txt"):
                    if member in archive.namelist():
                        (staging / member).write_bytes(archive.read(member))
                if target.exists():
                    shutil.rmtree(target)
                staging.rename(target)

        return self._load(target)

    def install_registered(
        self,
        package: Path,
        db: "Database",
        *,
        group_name: str,
        enabled: bool = True,
        game_dir: Path | None = None,
    ) -> tuple[MacRuntimePlugin, int]:
        """Install a package and register it on CDUMM's regular Mods page.

        Re-importing the same runtime package updates its existing row instead
        of creating a duplicate.  The previous enabled/group state is kept
        unless the row is new.
        """
        package = Path(package).resolve()
        with zipfile.ZipFile(package) as archive:
            manifest = self._read_manifest(archive)
            name = str(manifest.get("name") or package.stem)
            directory = self.runtime_dir / _plugin_directory_name(manifest, name)
        if game_dir is not None:
            self.validate_manifest_for_game(manifest, game_dir)

        existing = db.connection.execute(
            "SELECT id, enabled, group_id, applied FROM mods "
            "WHERE runtime_plugin_path = ?",
            (str(directory),),
        ).fetchone()

        group = db.connection.execute(
            "SELECT id FROM mod_groups WHERE name = ? ORDER BY id LIMIT 1",
            (group_name,),
        ).fetchone()
        if group:
            group_id = int(group[0])
        else:
            next_order = db.connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM mod_groups"
            ).fetchone()[0]
            cursor = db.connection.execute(
                "INSERT INTO mod_groups (name, sort_order) VALUES (?, ?)",
                (group_name, int(next_order)),
            )
            group_id = int(cursor.lastrowid)

        plugin = self.install(package)
        row_enabled = bool(existing[1]) if existing else bool(enabled)
        payload_enabled = bool(
            existing and row_enabled and bool(existing[3])
        )
        row_group = (
            int(existing[2])
            if existing and existing[2] is not None
            else group_id
        )
        plugin = self.set_enabled(plugin, payload_enabled)

        description = _runtime_description(manifest)
        game_uuid = str(manifest["game_binary_uuid"]).upper()
        if existing:
            mod_id = int(existing[0])
            db.connection.execute(
                "UPDATE mods SET name = ?, source_path = ?, version = ?, "
                "description = ?, group_id = ?, runtime_game_uuid = ?, "
                "drop_name = ? WHERE id = ?",
                (
                    name,
                    str(package),
                    str(manifest.get("version") or ""),
                    description,
                    row_group,
                    game_uuid,
                    package.name,
                    mod_id,
                ),
            )
        else:
            priority = int(
                db.connection.execute(
                    "SELECT COALESCE(MAX(priority), 0) + 1 FROM mods"
                ).fetchone()[0]
            )
            cursor = db.connection.execute(
                "INSERT INTO mods "
                "(name, mod_type, enabled, priority, source_path, author, "
                " version, description, group_id, drop_name, "
                " runtime_plugin_path, runtime_game_uuid) "
                "VALUES (?, 'paz', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    1 if row_enabled else 0,
                    priority,
                    str(package),
                    "CDUMM",
                    str(manifest.get("version") or ""),
                    description,
                    row_group,
                    package.name,
                    str(plugin.directory),
                    game_uuid,
                ),
            )
            mod_id = int(cursor.lastrowid)
        db.connection.commit()
        return plugin, mod_id

    def _load(self, directory: Path) -> MacRuntimePlugin:
        directory = Path(directory)
        manifest = json.loads(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
        payload = directory / str(manifest["payload"])
        disabled = payload.with_name(payload.name + DISABLED_SUFFIX)
        if not payload.exists() and not disabled.exists():
            raise ValueError(f"runtime payload missing from {directory}")
        active_payload = payload if payload.exists() else disabled
        return MacRuntimePlugin(
            name=str(manifest.get("name") or directory.name),
            version=str(manifest.get("version") or ""),
            directory=directory,
            payload=active_payload,
            enabled=payload.exists(),
            manifest=manifest,
        )

    def scan(self) -> list[MacRuntimePlugin]:
        if not self.runtime_dir.is_dir():
            return []
        plugins: list[MacRuntimePlugin] = []
        for child in sorted(self.runtime_dir.iterdir()):
            if not child.is_dir() or not (child / "manifest.json").exists():
                continue
            try:
                plugins.append(self._load(child))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return plugins

    def set_enabled(
        self, plugin: MacRuntimePlugin, enabled: bool
    ) -> MacRuntimePlugin:
        manifest_payload = plugin.directory / str(plugin.manifest["payload"])
        disabled = manifest_payload.with_name(
            manifest_payload.name + DISABLED_SUFFIX
        )
        if enabled and disabled.exists():
            disabled.replace(manifest_payload)
        elif not enabled and manifest_payload.exists():
            manifest_payload.replace(disabled)
        return self._load(plugin.directory)

    def uninstall(self, plugin: MacRuntimePlugin) -> None:
        shutil.rmtree(plugin.directory)

    def enabled_libraries(self) -> list[Path]:
        return [plugin.payload for plugin in self.scan() if plugin.enabled]

    def dyld_environment(self) -> dict[str, str]:
        libraries = self.enabled_libraries()
        if not libraries:
            return {}
        status_dir = self.runtime_dir / "status"
        status_dir.mkdir(parents=True, exist_ok=True)
        return {
            "DYLD_INSERT_LIBRARIES": ":".join(str(path) for path in libraries),
            "CDUMM_RUNTIME_STATUS_DIR": str(status_dir),
        }

    @staticmethod
    def find_game_executable(game_dir: Path) -> Path:
        """Find the native executable above a packages directory."""
        current = Path(game_dir).resolve()
        for _ in range(6):
            if current.suffix == ".app":
                macos_dir = current / "Contents" / "MacOS"
                info_path = current / "Contents" / "Info.plist"
                if info_path.is_file():
                    try:
                        with info_path.open("rb") as stream:
                            executable_name = plistlib.load(stream).get(
                                "CFBundleExecutable"
                            )
                    except (OSError, plistlib.InvalidFile, ValueError):
                        executable_name = None
                    if isinstance(executable_name, str):
                        declared = macos_dir / executable_name
                        if declared.is_file():
                            return declared
                candidates = [
                    path for path in macos_dir.iterdir()
                    if path.is_file() and path.stat().st_mode & 0o111
                ] if macos_dir.is_dir() else []
                if candidates:
                    return sorted(candidates)[0]
                break
            if current.parent == current:
                break
            current = current.parent
        raise FileNotFoundError("Crimson Desert macOS executable was not found")

    @staticmethod
    def macho_uuid(executable: Path) -> str:
        """Read LC_UUID from a native little-endian 64-bit Mach-O."""
        data = Path(executable).read_bytes()
        if len(data) < 32 or data[:4] != b"\xcf\xfa\xed\xfe":
            raise ValueError(f"unsupported Mach-O format: {executable}")
        ncmds = struct.unpack_from("<I", data, 16)[0]
        cursor = 32
        for _ in range(ncmds):
            if cursor + 8 > len(data):
                break
            command, size = struct.unpack_from("<II", data, cursor)
            if size < 8 or cursor + size > len(data):
                break
            if command == 0x1B and size >= 24:
                return str(uuid.UUID(bytes=data[cursor + 8:cursor + 24])).upper()
            cursor += size
        raise ValueError(f"LC_UUID was not found in {executable}")

    def validate_for_game(
        self, plugin: MacRuntimePlugin, game_dir: Path
    ) -> None:
        self.validate_manifest_for_game(plugin.manifest, game_dir)

    def validate_manifest_for_game(
        self, manifest: dict, game_dir: Path
    ) -> None:
        expected = str(manifest.get("game_binary_uuid") or "").upper()
        if not expected:
            raise ValueError("runtime manifest has no game binary UUID")
        executable = self.find_game_executable(game_dir)
        actual = self.macho_uuid(executable)
        if actual != expected:
            raise ValueError(
                "runtime package does not support this game build "
                f"(expected {expected}, found {actual})"
            )

    def synchronize_database(
        self, db: "Database", game_dir: Path
    ) -> list[Path]:
        """Make installed payload state exactly match enabled DB rows."""
        rows = db.connection.execute(
            "SELECT id, name, enabled, runtime_plugin_path FROM mods "
            "WHERE runtime_plugin_path IS NOT NULL "
            "AND runtime_plugin_path != '' ORDER BY priority"
        ).fetchall()
        runtime_root = self.runtime_dir.resolve()
        registered: dict[Path, object] = {}
        for row in rows:
            path = Path(row[3]).resolve()
            try:
                path.relative_to(runtime_root)
            except ValueError as exc:
                raise ValueError(
                    f"runtime plugin path is outside CDMods/runtime: {path}"
                ) from exc
            registered[path] = row
        enabled_libraries: list[Path] = []

        for plugin in self.scan():
            row = registered.get(plugin.directory.resolve())
            should_enable = bool(row and row[2])
            if should_enable:
                self.validate_for_game(plugin, game_dir)
            plugin = self.set_enabled(plugin, should_enable)
            if plugin.enabled:
                enabled_libraries.append(plugin.payload)

        missing = [
            (row[0], row[1], path)
            for path, row in registered.items()
            if not path.is_dir() and bool(row[2])
        ]
        if missing:
            details = ", ".join(f"{name} ({path})" for _, name, path in missing)
            raise FileNotFoundError(f"runtime plugin payload missing: {details}")
        return enabled_libraries

    def disable_all(self) -> int:
        """Disable every installed payload without deleting its package."""
        count = 0
        for plugin in self.scan():
            if plugin.enabled:
                self.set_enabled(plugin, False)
                count += 1
        return count

    @staticmethod
    def _decode_handshake_values(data: dict) -> None:
        if "window" not in data and "window_bits" in data:
            data["window"] = struct.unpack(
                "<f", struct.pack("<I", int(data["window_bits"]))
            )[0]
        if "range" not in data and "range_bits" in data:
            data["range"] = struct.unpack(
                "<f", struct.pack("<I", int(data["range_bits"]))
            )[0]

    def _handshake_matches_enabled_plugin(self, data: dict) -> bool:
        declared = _normalized_uuid(data.get("game_binary_uuid"))
        actual = _normalized_uuid(data.get("actual_uuid"))
        try:
            pid = int(data["pid"])
        except (KeyError, TypeError, ValueError):
            return False
        if pid <= 0:
            return False

        for plugin in self.scan():
            if not plugin.enabled:
                continue
            manifest = plugin.manifest
            expected_uuid = _normalized_uuid(manifest.get("game_binary_uuid"))
            if (
                not expected_uuid
                or declared != expected_uuid
                or actual != expected_uuid
            ):
                continue

            handshake = manifest.get("handshake")
            if isinstance(handshake, dict):
                plugin_id = handshake.get("plugin_id")
                required = handshake.get("required_fields")
                if (
                    not isinstance(plugin_id, str)
                    or data.get("plugin_id") != plugin_id
                    or not isinstance(required, dict)
                ):
                    continue
                if all(
                    data.get(key) == value
                    for key, value in required.items()
                ):
                    return True
                continue

            changes = manifest.get("changes") or {}
            expected_window = (
                (changes.get("just_window_fraction") or {}).get("patched")
            )
            expected_range = (
                (changes.get("evaluator_range") or {}).get("patched")
            )
            try:
                window = float(data["window"])
                evaluator_range = float(data["range"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                expected_window is not None
                and expected_range is not None
                and window == float(expected_window)
                and evaluator_range == float(expected_range)
            ):
                return True
        return False

    def latest_handshake(self, *, since: float = 0.0) -> dict | None:
        """Return the newest fully verified ``patched`` runtime receipt."""
        status_dir = self.runtime_dir / "status"
        if not status_dir.is_dir():
            return None
        candidates = [
            path for path in status_dir.glob("*.json")
            if path.stat().st_mtime >= since
        ]
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime,
                           reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._decode_handshake_values(data)
            except (
                OSError,
                KeyError,
                ValueError,
                TypeError,
                struct.error,
                json.JSONDecodeError,
            ):
                continue
            if (
                data.get("status") == "patched"
                and self._handshake_matches_enabled_plugin(data)
            ):
                data["_path"] = str(path)
                return data
        return None

    def wait_for_handshake(
        self, *, since: float, timeout: float = 30.0
    ) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.latest_handshake(since=since)
            if result:
                return result
            time.sleep(0.25)
        return None
