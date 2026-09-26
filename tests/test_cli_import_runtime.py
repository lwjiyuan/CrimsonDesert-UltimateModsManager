import hashlib
import json
import struct
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

from cdumm import cli
from cdumm.storage.database import Database


GAME_UUID = "01234567-89AB-CDEF-0123-456789ABCDEF"


def test_cli_import_runtime_registers_mod_in_requested_group(
    tmp_path: Path, monkeypatch, capsys
):
    app = tmp_path / "Crimson Desert.app"
    executable = app / "Contents" / "MacOS" / "CrimsonDesert"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(
        struct.pack(
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
        + struct.pack("<II16s", 0x1B, 24, uuid.UUID(GAME_UUID).bytes)
    )
    executable.chmod(0o755)
    game_dir = app / "Contents" / "Resources" / "packages"
    game_dir.mkdir(parents=True)
    database = Database(game_dir / "CDMods" / "cdumm.db")
    database.initialize()
    database.close()

    payload = b"test dylib"
    package = tmp_path / "runtime.zip"
    manifest = {
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
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("ParryWindow.dylib", payload)

    monkeypatch.setattr(cli, "_resolve_game_dir", lambda value: game_dir)
    cli.cmd_import_runtime(
        SimpleNamespace(
            game_dir=str(game_dir),
            package=str(package),
            group="2.03",
        )
    )

    result = json.loads(capsys.readouterr().out)
    assert result["version"] == "1.0.1"
    assert result["group"] == "2.03"
    database = Database(game_dir / "CDMods" / "cdumm.db")
    database.initialize()
    row = database.connection.execute(
        "SELECT m.enabled, m.applied, g.name, m.runtime_plugin_path "
        "FROM mods m JOIN mod_groups g ON g.id = m.group_id"
    ).fetchone()
    assert row[0:3] == (1, 0, "2.03")
    assert Path(row[3]).is_dir()
    database.connection.execute(
        "UPDATE mods SET enabled = 0, applied = 0 WHERE id = ?",
        (result["id"],),
    )
    database.connection.commit()
    database.close()

    cli.cmd_import_runtime(
        SimpleNamespace(
            game_dir=str(game_dir),
            package=str(package),
            group="2.03",
        )
    )
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["id"] == result["id"]
    assert repeated["enabled"] is False
    assert repeated["applied"] is False
