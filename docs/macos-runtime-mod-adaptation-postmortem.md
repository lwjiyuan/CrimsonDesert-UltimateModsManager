# macOS Runtime Mod Adaptation Postmortem

## Executive summary

Windows ASI modules cannot be loaded directly on macOS. They use a different
binary format, processor instruction set, loader, and hooking environment.
This work adds a native arm64 runtime-package path to CDUMM so selected
runtime behaviors can be reimplemented for macOS without modifying installed
game files.

The implementation covers package validation, database registration, normal
group assignment, Apply and Revert state, launch-time library injection, and
runtime receipts. Game-specific compatibility fingerprints are intentionally
excluded from the repository and must be supplied through local untracked
configuration.

Two adaptations were investigated:

- Perfect Guard and Evade: a native runtime value adapter was successful.
- Theft Without Crime: a native runtime hook proved the approach for one
  ownership path, but did not cover every theft source. A data-table solution
  is preferable when the relevant ownership data is available.

## Why a native adaptation is required

An ASI file is a Windows dynamic library. macOS cannot execute it because the
platform uses Mach-O libraries, arm64 calling conventions, a different
dynamic loader, and stricter signed-page enforcement.

The correct macOS equivalent is not file renaming or binary conversion.
The behavior must be understood and reimplemented as a native arm64 library.
That library is then loaded only for a CDUMM-managed game launch.

## Runtime package design

The `cdumm-macos-runtime-v1` package contains:

- A native arm64 dynamic library.
- A JSON manifest describing the payload and supported game build.
- A SHA-256 digest for payload integrity.
- Optional receipt requirements used to confirm runtime activation.

Before installation, CDUMM verifies:

- Archive paths cannot escape the destination directory.
- The declared payload exists and its digest matches.
- The package format is supported.
- The installed game executable is a Mach-O image.
- The local game UUID matches the package compatibility declaration.

The package is registered as a normal mod. It can use the existing version
groups and enabled state instead of a separate hidden configuration.

## Apply, Revert, and launch lifecycle

Apply synchronizes enabled runtime mods into the runtime manager. A runtime
mod is marked applied only when its payload is enabled. Revert disables all
runtime payloads and clears their applied state.

When an enabled runtime payload exists, the macOS launcher:

1. Builds the enabled native-library list.
2. Creates a private status directory for runtime receipts.
3. Starts the application with the runtime environment for that launch.
4. Waits for a receipt written by the native adapter.
5. Verifies the receipt against the package manifest and process state.

No persistent launcher configuration is edited. No installed executable or
game archive is modified.

## Perfect Guard and Evade adaptation

### Investigation

The desired timing behavior was not represented by a stable writable resource
table suitable for the ordinary data-mod pipeline. The effective values were
runtime globals consumed by the perfect-defense evaluator.

The first implementation risk was assuming the executable would always be the
first image returned by the dynamic loader. An injected library may occupy
that position. The final adapter scans every loaded arm64 image and selects
the one whose Mach-O UUID matches the locally configured supported build.

### Safety gates

The adapter requires all of the following before writing:

- Exact executable UUID match.
- Exact evaluator instruction signature match.
- Expected default values or already-patched target values.

The values are pinned after initialization because startup constructors may
restore defaults after the adapter is loaded. Unknown values are never
overwritten.

### Verification

Technical verification has two distinct outcomes:

- `armed`: the correct executable and evaluator were found.
- `patched`: the expected runtime values were observed and replaced.

A valid receipt and live process prove that the adapter loaded and passed its
technical gates. Gameplay validation is still required to confirm that the
chosen values produce the intended guard and evade experience.

## Theft Without Crime adaptation

### Failed signed-page approach

The initial prototype replaced instructions in the executable `__TEXT`
section. Virtual-memory APIs appeared to accept the write, but macOS
terminated the process when the modified page was executed. The failure was
reported as `CODESIGNING / Invalid Page`.

This is an important platform distinction: a successful memory-protection
call does not make signed executable-page modification safe under the macOS
Hardened Runtime.

### Hardware-breakpoint approach

The next adapter used an ARM64 hardware execution breakpoint. A `SIGTRAP`
handler recognized the configured target and returned the supported result
without changing signed executable bytes.

The adapter:

- Finds the target only after UUID and instruction-signature checks.
- Arms existing threads and periodically covers newly created threads.
- Records an `armed` receipt after breakpoint installation.
- Records a `patched` receipt after a real breakpoint hit.

This avoided the signed-page crash and worked for the investigated direct
ownership path.

### Coverage limitation

The runtime hook was narrower than the user-visible feature name suggested.
The game has multiple theft and crime paths. Direct pickup, interior
ownership, merchant-owned items, and related crime events do not necessarily
share one function.

The hook therefore demonstrated a safe native interception technique but was
not sufficient evidence of complete theft suppression. The preferred final
solution is a data-table or native buff adaptation when a common ownership
policy can be represented there. It is easier to merge, inspect, revert, and
validate than several runtime hooks.

## Validation strategy

Validation is intentionally layered:

1. Package validation checks archive paths, payload digest, manifest fields,
   and compatibility metadata.
2. Synthetic Mach-O fixtures check UUID parsing without commercial binaries.
3. An isolated native harness checks hook behavior and receipt output.
4. Apply and Revert checks verify database and payload state transitions.
5. Launch checks verify environment construction without persistent launcher
   changes.
6. Receipt checks require matching plugin identity, UUID, expected fields,
   timestamp, and a live process.
7. Gameplay tests confirm the actual player-facing behavior and detect
   incomplete coverage.

A package passing the first six layers is technically integrated. It is not
declared functionally complete until gameplay testing covers every promised
scenario.

## New macOS capabilities added to CDUMM

- Recognition and safe extraction of native runtime packages.
- Payload digest and game-build compatibility validation.
- Database registration in ordinary mod groups.
- Apply, Revert, enable, disable, and uninstall lifecycle support.
- Launch-scoped native library loading.
- Manifest-defined runtime receipt validation.
- A generic CLI import path for native runtime packages.
- A native executable lookup based on the application bundle metadata.
- Build templates that keep real UUIDs, offsets, and instruction signatures
  outside the repository.

## Security, privacy, and copyright boundaries

The repository does not include game executables, resources, extracted
assets, commercial binary fragments, third-party payloads, or real
game-build fingerprints. Native build scripts require a local untracked JSON
configuration and generate the compatibility header outside source control.

Generated libraries, archives, manifests containing real fingerprints, and
runtime receipts must remain untracked. Any third-party implementation used
as behavioral reference must be independently reimplemented unless its
license explicitly permits source reuse.

## Lessons for future adaptations

- Treat an ASI mod as a behavioral specification, not a portable binary.
- Prefer data-level changes when they can express the complete feature.
- Never assume the main executable is dynamic-loader image zero.
- Use both build identity and local instruction signatures as compatibility
  gates.
- Do not write signed executable pages in production.
- Separate technical activation from gameplay correctness.
- Document partial coverage explicitly instead of presenting a narrow hook as
  a complete feature.

## Iteration record

### Native runtime package lifecycle

Why:
Windows ASI payloads cannot run natively on macOS, and CDUMM had no managed
equivalent for launch-scoped native libraries.

What changed:
Added package validation, database registration, ordinary version-group
assignment, Apply and Revert synchronization, uninstall cleanup, CLI import,
GUI import, launch-scoped loading, and manifest-defined runtime receipts.

What it resolved:
Native macOS adapters can now use CDUMM's normal mod lifecycle without
persistently changing launcher configuration or installed game files.

Test result:
Static diagnostics report no errors and repository hygiene checks pass. The
macOS application build completed successfully, its code signature was
verified, and the candidate was installed as CDUMM 3.17.8. Automated tests
were not run. User gameplay validation accepted the corrected launch path and
the 5x perfect-defense behavior; other active mods showed no obvious
regression.

### Compatibility fingerprint isolation

Why:
Real executable UUIDs, offsets, instruction signatures, third-party payload
hashes, and build metadata must not be published.

What changed:
Converted both native adapters into templates. Build scripts now require a
local untracked JSON configuration and generate the compatibility header under
an ignored build directory.

What it resolved:
The public branch contains implementation logic without commercial binary
fingerprints, redistributed payloads, or machine-specific paths.

Test result:
The staged diff was scanned for known fingerprints, credentials, absolute
user paths, store-specific metadata, Chinese text, and binary files. No
matching additions were found. The CDUMM application build succeeded. Native
runtime payload builds still require local untracked compatibility
configuration and were not rebuilt during this iteration.

### Perfect Guard and Evade

Why:
The behavior depends on runtime evaluator values rather than a stable
resource-table field.

What changed:
Added an arm64 adapter that scans loaded images, verifies a locally configured
UUID and evaluator signature, accepts only known values, pins the target
values, and emits a verified receipt.

What it resolved:
CDUMM can represent and launch a native macOS equivalent of the wider
perfect-defense window.

Test result:
An earlier private prototype was gameplay-tested, but that does not validate
the sanitized public template. Exact-branch gameplay testing confirmed that
the runtime loads, but the 2.5x values still produce a low perceived perfect
guard and evade trigger rate. The behavior is not yet accepted.

### Theft Without Crime

Why:
Direct signed-code modification caused macOS to terminate the process, while
the investigated ownership path still required runtime interception.

What changed:
Replaced the production signed-page write with an ARM64 hardware-breakpoint
adapter guarded by local UUID and instruction-signature configuration.

What it resolved:
The supported direct ownership path can be intercepted without modifying a
signed executable page.

Test result:
Earlier private testing confirmed only partial coverage. Interior and
merchant-related crime paths require the separate data-level solution
described above. The exact public template remains pending gameplay
validation and must not be described as complete theft suppression.

### Contribution safeguards

Why:
The contribution must protect personal information, third-party rights, and
upstream communication quality.

What changed:
Added an always-applied project rule requiring English-only repository
changes, local-only game fingerprints, ongoing iteration records, gameplay
validation before runtime-mod commits, and contributor approval before any
online CDUMM message is published.

What it resolved:
Future implementation and communication steps now have explicit privacy,
copyright, validation, and approval gates.

Test result:
The rule is present in the feature branch and its approval and iteration
requirements are active.

### Store-managed runtime launch context

Why:
The sanitized candidate launched the application bundle with runtime injection
variables but omitted the dynamically resolved store launch identity. The game
started far enough to show its own dialog, then rejected the launch because it
could not associate the process with the running client. No runtime receipt
was produced because the process exited early.

What changed:
For an identified store-managed installation, the launcher now resolves the
application identifier at runtime and passes the required launch-context
variables together with the native-library environment. No fixed application
identifier, account value, local client configuration, or machine path is
stored in source.

What it resolved:
The injected application launch can retain the ownership context expected by
the running client while keeping the integration launch-scoped and avoiding
persistent client configuration changes.

Test result:
The failure was reproduced by user testing. The revised macOS application
build completed, its code signature was verified, and the candidate was
installed. The revised launch succeeded in user testing, and the other active
mods showed no obvious regression. Perfect guard and evade tuning remains
open.

### Perfect-defense 5x tuning

Why:
Gameplay testing confirmed that the 2.5x runtime loaded correctly but still
felt too restrictive during normal combat.

What changed:
Raised the action-window ratio from `1.25` to `2.5` and the evaluator range
from `6.25` to `12.5`, which is five times each original value. The package
name, version, receipt requirements, tests, and documentation were updated to
match. The build configuration can supply a stable local plugin identifier so
an existing installation can be upgraded without creating a duplicate row.

What it resolved:
The next candidate should provide a clearly wider timing window while still
requiring an actual guard or evade input.

Test result:
The native package built successfully and was upgraded through CDUMM in place.
The database contains one enabled and applied runtime row at version `1.1.0`;
no duplicate mod was created. User gameplay validation accepted the 5x
behavior.
