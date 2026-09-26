#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "runtime_config.h"

#if !defined(__APPLE__) || !defined(__aarch64__)
#error "This runtime patch only supports native arm64 macOS."
#endif

#define VANILLA_WINDOW_BITS UINT32_C(0x3F000000)
#define PATCHED_WINDOW_BITS UINT32_C(0x40200000)
#define VANILLA_RANGE_BITS  UINT32_C(0x40200000)
#define PATCHED_RANGE_BITS  UINT32_C(0x41480000)

static const uint8_t k_supported_uuid[16] =
    CDUMM_SUPPORTED_UUID_BYTES;
static uint8_t g_actual_uuid[16] = {0};

static const uint8_t k_evaluator_signature[] =
    CDUMM_EVALUATOR_SIGNATURE_BYTES;

static bool main_image_uuid_matches(const struct mach_header_64 *header) {
    const uint8_t *cursor = (const uint8_t *)(header + 1);
    for (uint32_t i = 0; i < header->ncmds; ++i) {
        const struct load_command *command =
            (const struct load_command *)cursor;
        if (command->cmdsize < sizeof(*command)) {
            return false;
        }
        if (command->cmd == LC_UUID &&
            command->cmdsize >= sizeof(struct uuid_command)) {
            const struct uuid_command *uuid =
                (const struct uuid_command *)command;
            memcpy(g_actual_uuid, uuid->uuid, sizeof(g_actual_uuid));
            return memcmp(uuid->uuid, k_supported_uuid,
                          sizeof(k_supported_uuid)) == 0;
        }
        cursor += command->cmdsize;
    }
    return false;
}

static bool replace_known_value(_Atomic uint32_t *target,
                                uint32_t vanilla,
                                uint32_t patched) {
    uint32_t current = atomic_load_explicit(target, memory_order_acquire);
    if (current == patched) {
        return true;
    }
    if (current != vanilla) {
        return false;
    }
    return atomic_compare_exchange_strong_explicit(
        target, &current, patched, memory_order_release, memory_order_relaxed);
}

static void write_handshake(const char *status,
                            uint32_t window_bits,
                            uint32_t range_bits) {
    const char *directory = getenv("CDUMM_RUNTIME_STATUS_DIR");
    if (directory == NULL || directory[0] == '\0') {
        return;
    }
    (void)mkdir(directory, 0700);

    char temporary[4096];
    char final[4096];
    const pid_t pid = getpid();
    const long timestamp = (long)time(NULL);
    const int final_len = snprintf(
        final, sizeof(final), "%s/parry-window-%d-%ld-%s.json",
        directory, pid, timestamp, status);
    const int temp_len = snprintf(
        temporary, sizeof(temporary), "%s/.parry-window-%d-%ld.tmp",
        directory, pid, timestamp);
    if (final_len <= 0 || temp_len <= 0 ||
        (size_t)final_len >= sizeof(final) ||
        (size_t)temp_len >= sizeof(temporary)) {
        return;
    }

    FILE *file = fopen(temporary, "w");
    if (file == NULL) {
        return;
    }
    (void)fprintf(
        file,
        "{\"status\":\"%s\",\"pid\":%d,"
        "\"plugin_id\":\"" CDUMM_PLUGIN_ID_STRING "\","
        "\"game_binary_uuid\":\"" CDUMM_SUPPORTED_UUID_STRING "\","
        "\"actual_uuid\":\""
        "%02X%02X%02X%02X-%02X%02X-%02X%02X-%02X%02X-"
        "%02X%02X%02X%02X%02X%02X\","
        "\"window_bits\":%u,\"range_bits\":%u,\"timestamp\":%ld}\n",
        status, pid,
        g_actual_uuid[0], g_actual_uuid[1], g_actual_uuid[2],
        g_actual_uuid[3], g_actual_uuid[4], g_actual_uuid[5],
        g_actual_uuid[6], g_actual_uuid[7], g_actual_uuid[8],
        g_actual_uuid[9], g_actual_uuid[10], g_actual_uuid[11],
        g_actual_uuid[12], g_actual_uuid[13], g_actual_uuid[14],
        g_actual_uuid[15],
        window_bits, range_bits, timestamp);
    (void)fflush(file);
    (void)fsync(fileno(file));
    (void)fclose(file);
    (void)rename(temporary, final);
}

static bool resolve_targets(_Atomic uint32_t **window,
                            _Atomic uint32_t **range,
                            const char **failure_reason) {
#if defined(CDUMM_PARRY_TESTING)
    *window = (_Atomic uint32_t *)dlsym(
        RTLD_DEFAULT, "cdumm_parry_test_window");
    *range = (_Atomic uint32_t *)dlsym(
        RTLD_DEFAULT, "cdumm_parry_test_range");
    if (*window == NULL || *range == NULL) {
        *failure_reason = "test-symbols";
        return false;
    }
    return true;
#else
    const struct mach_header_64 *header = NULL;
    const uint32_t image_count = _dyld_image_count();
    for (uint32_t image_index = 0;
         image_index < image_count;
         ++image_index) {
        const struct mach_header *candidate =
            _dyld_get_image_header(image_index);
        if (candidate == NULL || candidate->magic != MH_MAGIC_64 ||
            candidate->cputype != CPU_TYPE_ARM64) {
            continue;
        }
        const struct mach_header_64 *candidate64 =
            (const struct mach_header_64 *)candidate;
        if (main_image_uuid_matches(candidate64)) {
            header = candidate64;
            break;
        }
    }
    if (header == NULL) {
        *failure_reason = "uuid";
        return false;
    }

    const uintptr_t image_base = (uintptr_t)header;
    const uint8_t *signature =
        (const uint8_t *)(
            image_base + CDUMM_EVALUATOR_SIGNATURE_OFFSET
        );
    if (memcmp(signature, k_evaluator_signature,
               sizeof(k_evaluator_signature)) != 0) {
        *failure_reason = "signature";
        return false;
    }

    *window = (_Atomic uint32_t *)(
        image_base + CDUMM_WINDOW_VALUE_OFFSET
    );
    *range = (_Atomic uint32_t *)(
        image_base + CDUMM_RANGE_VALUE_OFFSET
    );
    return true;
#endif
}

static void *patch_worker(void *unused) {
    (void)unused;

    _Atomic uint32_t *window = NULL;
    _Atomic uint32_t *range = NULL;
    const char *failure_reason = "unknown";
    if (!resolve_targets(&window, &range, &failure_reason)) {
        char status[64];
        (void)snprintf(
            status, sizeof(status), "rejected-%s", failure_reason);
        write_handshake(status, 0, 0);
        return NULL;
    }
    write_handshake(
        "armed",
        atomic_load_explicit(window, memory_order_acquire),
        atomic_load_explicit(range, memory_order_acquire));

    bool handshake_written = false;
    for (;;) {
        const bool window_ok = replace_known_value(
            window, VANILLA_WINDOW_BITS, PATCHED_WINDOW_BITS);
        const bool range_ok = replace_known_value(
            range, VANILLA_RANGE_BITS, PATCHED_RANGE_BITS);
        if (window_ok && range_ok && !handshake_written) {
            write_handshake(
                "patched",
                atomic_load_explicit(window, memory_order_acquire),
                atomic_load_explicit(range, memory_order_acquire));
            handshake_written = true;
        }
        usleep(250000);
    }
}

__attribute__((constructor))
static void cdumm_parry_window_start(void) {
    pthread_t thread;
    if (pthread_create(&thread, NULL, patch_worker, NULL) == 0) {
        pthread_detach(thread);
    }
}
