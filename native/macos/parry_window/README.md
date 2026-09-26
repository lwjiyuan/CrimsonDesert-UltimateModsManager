# Perfect Guard and Evade 5x for macOS

This native arm64 runtime adapter expands two perfect-defense parameters:

- Action-window ratio: `0.5` to `2.5`.
- Evaluator range: `2.5` to `12.5`.

The library is loaded only for a CDUMM-managed launch. It changes process
memory and does not alter the installed executable or game archives.

## Compatibility gates

The adapter writes values only when all checks pass:

1. The target is an arm64 Mach-O image.
2. Its UUID matches the locally configured supported build.
3. The evaluator instruction signature matches.
4. Both parameters contain either their known defaults or target values.

An unsupported update or unknown value causes the adapter to stop without
writing.

## Local configuration

Game-specific fingerprints must stay in a local, untracked JSON file:

```json
{
  "game_version": "supported-version",
  "game_binary_uuid": "00000000-0000-0000-0000-000000000000",
  "window_value_offset": "0x0",
  "range_value_offset": "0x0",
  "evaluator_signature_offset": "0x0",
  "evaluator_signature": "00000000"
}
```

Build with:

```bash
python3 scripts/build_macos_parry_window.py --config local-config.json
```

Generated libraries, archives, manifests, and real compatibility
fingerprints must not be committed.
