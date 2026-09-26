import hashlib
import json
import struct
import uuid
import zipfile
from pathlib import Path

import pytest

from cdumm.engine.mod_manager import ModManager
from cdumm.engine.macos_runtime_plugins import MacRuntimePluginManager
from cdumm.storage.database import Database


GAME_UUID = "01234567-89AB-CDEF-0123-456789ABCDEF"


def _manifest(payload: bytes, **overrides) -> dict:
    data = {
        "format": "cdumm-macos-runtime-v1",
        "name": "Perfect Defense 5x",
        "version": "1.0.1",
        "game_version": "2.03.02",
        "game_binary_uuid": GAME_UUID,
        "architecture": "arm64",
        "load_method": "DYLD_INSERT_LIBRARIES",
        "payload": "ParryWindow.dylib",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "changes": {
            "just_window_fraction": {"vanilla": 0.5, "patched": 2.5},
            "evaluator_range": {"vanilla": 2.5, "patched": 12.5},
        },
    }
    data.update(overrides)
    return data


def _package(
    path: Path,
    *,
    payload: bytes = b"arm64 dylib",
    **manifest_overrides,
) -> Path:
    manifest = _manifest(payload, **manifest_overrides)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("ParryWindow.dylib", payload)
    return path


def _fake_game(tmp_path: Path, game_uuid: str = GAME_UUID) -> Path:
    app = tmp_path / "Crimson Desert.app"
    executable = app / "Contents" / "MacOS" / "CrimsonDesert"
    executable.parent.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<plist version=\"1.0\"><dict><key>CFBundleExecutable</key>"
        "<string>CrimsonDesert</string></dict></plist>",
        encoding="utf-8",
    )
    header = struct.pack(
        "<IiiIIIII",
        0xFEEDFACF,
        0x0100000C,
        0,
        2,
        1,
        24,
        0,
        0,
    )
    command = struct.pack(
        "<II16s", 0x1B, 24, uuid.UUID(game_uuid).bytes
    )
    executable.write_bytes(header + command)
    executable.chmod(0o755)
    game_dir = app / "Contents" / "Resources" / "packages"
    game_dir.mkdir(parents=True)
    return game_dir


def test_runtime_package_install_toggle_and_uninstall(tmp_path):
    package = _package(tmp_path / "parry.zip")
    manager = MacRuntimePluginManager(tmp_path / "runtime")

    assert manager.is_package(package)
    plugin = manager.install(package)
    assert plugin.enabled
    assert manager.enabled_libraries() == [plugin.payload]
    assert manager.dyld_environment() == {
        "DYLD_INSERT_LIBRARIES": str(plugin.payload),
        "CDUMM_RUNTIME_STATUS_DIR": str(tmp_path / "runtime" / "status"),
    }

    plugin = manager.set_enabled(plugin, False)
    assert not plugin.enabled
    assert manager.enabled_libraries() == []

    plugin = manager.set_enabled(plugin, True)
    assert plugin.enabled
    manager.uninstall(plugin)
    assert manager.scan() == []


def test_runtime_package_rejects_payload_hash_mismatch(tmp_path):
    package = _package(
        tmp_path / "parry.zip", payload_sha256="0" * 64
    )

    manager = MacRuntimePluginManager(tmp_path / "runtime")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        manager.install(package)


def test_runtime_package_rejects_parent_traversal(tmp_path):
    package = _package(tmp_path / "parry.zip")
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr("../outside", b"no")

    manager = MacRuntimePluginManager(tmp_path / "runtime")
    with pytest.raises(ValueError, match="unsafe path"):
        manager.install(package)


def test_registered_runtime_uses_regular_group_and_waits_for_apply(tmp_path):
    package = _package(tmp_path / "parry.zip")
    database = Database(tmp_path / "CDMods" / "cdumm.db")
    database.initialize()
    manager = MacRuntimePluginManager(tmp_path / "CDMods" / "runtime")

    plugin, first_id = manager.install_registered(
        package, database, group_name="2.03"
    )
    _, second_id = manager.install_registered(
        package, database, group_name="2.03"
    )

    assert first_id == second_id
    assert not plugin.enabled
    row = database.connection.execute(
        "SELECT m.name, m.enabled, m.applied, g.name, "
        "m.runtime_plugin_path, m.runtime_game_uuid FROM mods m "
        "JOIN mod_groups g ON g.id = m.group_id WHERE m.id = ?",
        (first_id,),
    ).fetchone()
    assert row == (
        "Perfect Defense 5x",
        1,
        0,
        "2.03",
        str(plugin.directory),
        GAME_UUID,
    )
    assert database.connection.execute(
        "SELECT COUNT(*) FROM mods WHERE runtime_plugin_path IS NOT NULL"
    ).fetchone()[0] == 1
    database.close()


def test_runtime_package_rejects_different_game_uuid_before_install(tmp_path):
    package = _package(tmp_path / "parry.zip")
    game_dir = _fake_game(
        tmp_path, "11111111-2222-3333-4444-555555555555"
    )
    database = Database(tmp_path / "cdumm.db")
    database.initialize()
    manager = MacRuntimePluginManager(tmp_path / "runtime")

    with pytest.raises(ValueError, match="does not support this game build"):
        manager.install_registered(
            package,
            database,
            group_name="2.03",
            game_dir=game_dir,
        )
    assert manager.scan() == []
    assert database.connection.execute(
        "SELECT COUNT(*) FROM mods"
    ).fetchone()[0] == 0
    database.close()


def test_registered_runtime_uninstall_removes_payload_and_mod_row(tmp_path):
    package = _package(tmp_path / "parry.zip")
    cdmods = tmp_path / "CDMods"
    database = Database(cdmods / "cdumm.db")
    database.initialize()
    runtime = MacRuntimePluginManager(cdmods / "runtime")
    plugin, mod_id = runtime.install_registered(
        package, database, group_name="2.03"
    )

    ModManager(database, cdmods / "deltas").remove_mod(mod_id)

    assert not plugin.directory.exists()
    assert database.connection.execute(
        "SELECT COUNT(*) FROM mods WHERE id = ?", (mod_id,)
    ).fetchone()[0] == 0
    database.close()


def test_runtime_handshake_requires_uuid_and_exact_patched_values(tmp_path):
    package = _package(tmp_path / "parry.zip")
    manager = MacRuntimePluginManager(tmp_path / "runtime")
    manager.install(package)
    status_dir = manager.runtime_dir / "status"
    status_dir.mkdir(parents=True)

    bad = {
        "status": "patched",
        "pid": 123,
        "game_binary_uuid": GAME_UUID,
        "actual_uuid": GAME_UUID,
        "window_bits": 0x3F800000,
        "range_bits": 0x41480000,
    }
    (status_dir / "bad.json").write_text(
        json.dumps(bad), encoding="utf-8"
    )
    assert manager.latest_handshake() is None

    good = {
        **bad,
        "window_bits": 0x40200000,
        "range_bits": 0x41480000,
    }
    (status_dir / "good.json").write_text(
        json.dumps(good), encoding="utf-8"
    )
    result = manager.latest_handshake()
    assert result is not None
    assert result["window"] == 2.5
    assert result["range"] == 12.5

    good["pid"] = 0
    (status_dir / "zero-pid.json").write_text(
        json.dumps(good), encoding="utf-8"
    )
    (status_dir / "good.json").unlink()
    assert manager.latest_handshake() is None

    good["pid"] = 123
    good["actual_uuid"] = "11111111-2222-3333-4444-555555555555"
    (status_dir / "wrong-uuid.json").write_text(
        json.dumps(good), encoding="utf-8"
    )
    (status_dir / "zero-pid.json").unlink()
    assert manager.latest_handshake() is None


def test_runtime_handshake_supports_manifest_defined_receipt(tmp_path):
    package = _package(
        tmp_path / "just-take.zip",
        name="Theft Without Crime",
        changes={"steal_side_effect_result": {"patched": 0}},
        handshake={
            "plugin_id": "cdumm-just-take-macos",
            "required_fields": {
                "patched_result": 0,
            },
        },
    )
    manager = MacRuntimePluginManager(tmp_path / "runtime")
    plugin = manager.install(package)
    assert plugin.directory.name == "cdumm-just-take-macos"
    status_dir = manager.runtime_dir / "status"
    status_dir.mkdir(parents=True)

    receipt = {
        "status": "patched",
        "plugin_id": "cdumm-just-take-macos",
        "pid": 321,
        "game_binary_uuid": GAME_UUID,
        "actual_uuid": GAME_UUID,
        "patched_result": 0,
        "hit_count": 0,
    }
    path = status_dir / "just-take.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert manager.latest_handshake()["plugin_id"] == receipt["plugin_id"]

    receipt["patched_result"] = 1
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert manager.latest_handshake() is None


def test_native_patch_scans_all_dyld_images_for_game_uuid():
    source = (
        Path(__file__).resolve().parents[1]
        / "native"
        / "macos"
        / "parry_window"
        / "parry_window.c"
    ).read_text(encoding="utf-8")

    assert "_dyld_image_count()" in source
    assert "_dyld_get_image_header(image_index)" in source
    assert "main_image_uuid_matches(candidate64)" in source
    assert "_dyld_get_image_header(0)" not in source
