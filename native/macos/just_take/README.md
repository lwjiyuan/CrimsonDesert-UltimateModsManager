# Theft Without Crime for macOS

This is a native Apple Silicon runtime adapter for a supported theft
ownership path.

An early prototype rewrote a signed `__TEXT` page. The macOS Hardened
Runtime terminated the process with `CODESIGNING / Invalid Page` when that
page was executed. The production adapter instead uses an ARM64 hardware
execution breakpoint and a `SIGTRAP` handler to return the supported result
without rewriting executable code.

The adapter:

- Does not write to the game installation or signed executable pages.
- Rejects binaries whose UUID or target instruction signature differs.
- Arms existing and newly created threads.
- Writes an `armed` runtime receipt, then a `patched` receipt after a hit.

## Local configuration

Game-specific fingerprints must stay in a local, untracked JSON file:

```json
{
  "game_version": "supported-version",
  "game_binary_uuid": "00000000-0000-0000-0000-000000000000",
  "target_offset": "0x0",
  "target_signature": "00000000"
}
```

Build with:

```bash
python3 scripts/build_macos_just_take.py --config local-config.json
```

Generated libraries, archives, manifests, and real compatibility
fingerprints must not be committed.
