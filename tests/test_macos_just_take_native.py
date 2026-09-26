import json
import platform
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "macos" / "just_take" / "just_take.c"


@pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64",
    reason="native arm64 macOS harness",
)
def test_just_take_native_hook_and_receipt(tmp_path):
    clang = shutil.which("clang")
    assert clang is not None
    harness = tmp_path / "harness.c"
    executable = tmp_path / "just-take-harness"
    dylib = tmp_path / "JustTakeTest.dylib"
    status_dir = tmp_path / "status"
    harness.write_text(
        r"""
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>

__attribute__((naked, noinline, visibility("default"), aligned(16384)))
uint32_t cdumm_just_take_test_target(void *a, void *b, void *c) {
    __asm__("mov w0, #1\n"
            "ret\n"
            "nop\n"
            "nop\n");
}

int main(void) {
    uint32_t result = 1;
    for (int i = 0; i < 100 && result != 0; ++i) {
        result = cdumm_just_take_test_target(0, 0, 0);
        usleep(20000);
    }
    printf("result=%u\n", result);
    usleep(500000);
    return result == 0 ? 0 : 1;
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            clang,
            "-dynamiclib",
            "-arch",
            "arm64",
            "-std=c11",
            "-O0",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-DCDUMM_JUST_TAKE_TESTING",
            "-undefined",
            "dynamic_lookup",
            str(SOURCE),
            "-o",
            str(dylib),
        ],
        check=True,
    )
    subprocess.run(
        [
            clang,
            "-arch",
            "arm64",
            "-std=c11",
            "-O0",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-parameter",
            "-Wl,-exported_symbol,_cdumm_just_take_test_target",
            str(harness),
            "-o",
            str(executable),
        ],
        check=True,
    )
    result = subprocess.run(
        [executable],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": str(Path(clang).parent),
            "CDUMM_RUNTIME_STATUS_DIR": str(status_dir),
            "DYLD_INSERT_LIBRARIES": str(dylib),
        },
    )
    assert "result=0" in result.stdout

    receipts = sorted(status_dir.glob("*.json"))
    assert receipts
    data = json.loads(receipts[-1].read_text(encoding="utf-8"))
    assert data["status"] == "patched"
    assert data["plugin_id"] == "cdumm-just-take-macos"
    assert data["target_offset"] == 0
    assert data["patched_result"] == 0
    assert data["hit_count"] >= 1


def test_just_take_source_has_build_and_instruction_guards():
    source = SOURCE.read_text(encoding="utf-8")
    assert "CDUMM_TARGET_OFFSET" in source
    assert "k_supported_uuid" in source
    assert "k_target_signature" in source
    assert 'failure_reason = "signature"' in source
    assert "_dyld_image_count()" in source
    assert "ARM_DEBUG_STATE64" in source
    assert "hardware_breakpoint_handler" in source
