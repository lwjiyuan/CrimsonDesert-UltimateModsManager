#define _DARWIN_C_SOURCE
#define _XOPEN_SOURCE 700

#include <libkern/OSCacheControl.h>
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>

#if !defined(__APPLE__) || !defined(__aarch64__)
#error "This runtime patch only supports native arm64 macOS."
#endif

#define PATCHED_RESULT UINT32_C(0)
#define PLUGIN_ID "cdumm-just-take-macos"

#if defined(CDUMM_JUST_TAKE_TESTING)
#define TARGET_OFFSET UINT64_C(0)
#define SUPPORTED_UUID_STRING "00000000-0000-0000-0000-000000000000"
static const uint8_t k_supported_uuid[16] = {0};
#else
#include "runtime_config.h"
#define TARGET_OFFSET CDUMM_TARGET_OFFSET
#define SUPPORTED_UUID_STRING CDUMM_SUPPORTED_UUID_STRING
static const uint8_t k_supported_uuid[16] =
    CDUMM_SUPPORTED_UUID_BYTES;
#endif

static uint8_t g_actual_uuid[16] = {0};
static _Atomic uint64_t g_hit_count = 0;
static uint64_t g_pointer_count = 0;
static uintptr_t g_first_pointer_address = 0;
static const char *g_hook_mode = "none";

#if defined(CDUMM_JUST_TAKE_TESTING)
extern uint32_t cdumm_just_take_test_target(void *, void *, void *);
static const uint8_t k_target_signature[16] = {
    0x20, 0x00, 0x80, 0x52, 0xC0, 0x03, 0x5F, 0xD6,
    0x1F, 0x20, 0x03, 0xD5, 0x1F, 0x20, 0x03, 0xD5,
};
#else
static const uint8_t k_target_signature[] =
    CDUMM_TARGET_SIGNATURE_BYTES;
#endif

#if !defined(CDUMM_JUST_TAKE_TESTING)
static bool image_uuid_matches(const struct mach_header_64 *header) {
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
            if (memcmp(uuid->uuid, k_supported_uuid,
                       sizeof(k_supported_uuid)) != 0) {
                return false;
            }
            memcpy(g_actual_uuid, uuid->uuid, sizeof(g_actual_uuid));
            return true;
        }
        cursor += command->cmdsize;
    }
    return false;
}
#endif

static void write_handshake(const char *status, uintptr_t target) {
    const char *directory = getenv("CDUMM_RUNTIME_STATUS_DIR");
    if (directory == NULL || directory[0] == '\0') {
        return;
    }
    (void)mkdir(directory, 0700);

    char temporary[4096];
    char final[4096];
    const pid_t pid = getpid();
    const long timestamp = (long)time(NULL);
    const uint64_t hits =
        atomic_load_explicit(&g_hit_count, memory_order_acquire);
    const int final_len = snprintf(
        final, sizeof(final), "%s/just-take-%d-%ld-%llu-%s.json",
        directory, pid, timestamp, (unsigned long long)hits, status);
    const int temp_len = snprintf(
        temporary, sizeof(temporary), "%s/.just-take-%d-%ld.tmp",
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
        "{\"status\":\"%s\",\"plugin_id\":\"%s\",\"pid\":%d,"
        "\"game_binary_uuid\":\"" SUPPORTED_UUID_STRING "\","
        "\"actual_uuid\":\""
        "%02X%02X%02X%02X-%02X%02X-%02X%02X-%02X%02X-"
        "%02X%02X%02X%02X%02X%02X\","
        "\"target_offset\":%llu,\"target_address\":%llu,"
        "\"patched_result\":%u,\"hit_count\":%llu,"
        "\"pointer_count\":%llu,\"first_pointer_address\":%llu,"
        "\"hook_mode\":\"%s\","
        "\"timestamp\":%ld}\n",
        status, PLUGIN_ID, pid,
        g_actual_uuid[0], g_actual_uuid[1], g_actual_uuid[2],
        g_actual_uuid[3], g_actual_uuid[4], g_actual_uuid[5],
        g_actual_uuid[6], g_actual_uuid[7], g_actual_uuid[8],
        g_actual_uuid[9], g_actual_uuid[10], g_actual_uuid[11],
        g_actual_uuid[12], g_actual_uuid[13], g_actual_uuid[14],
        g_actual_uuid[15], (unsigned long long)TARGET_OFFSET,
        (unsigned long long)target, PATCHED_RESULT,
        (unsigned long long)hits, (unsigned long long)g_pointer_count,
        (unsigned long long)g_first_pointer_address, g_hook_mode, timestamp);
    (void)fflush(file);
    (void)fsync(fileno(file));
    (void)fclose(file);
    (void)rename(temporary, final);
}

#if defined(CDUMM_JUST_TAKE_TESTING)
__attribute__((noinline))
static uint32_t just_take_hook(void *context, void *actor, void *target) {
    (void)context;
    (void)actor;
    (void)target;
    (void)atomic_fetch_add_explicit(
        &g_hit_count, UINT64_C(1), memory_order_release);
    return PATCHED_RESULT;
}

static bool install_hook(uint8_t *target, const char **failure_reason) {
    if (memcmp(target, k_target_signature, sizeof(k_target_signature)) != 0) {
        *failure_reason = "signature";
        return false;
    }

    uint8_t patch[16] = {
        0x50, 0x00, 0x00, 0x58, 0x00, 0x02, 0x1F, 0xD6,
        0, 0, 0, 0, 0, 0, 0, 0,
    };
    const uintptr_t hook = (uintptr_t)&just_take_hook;
    memcpy(patch + 8, &hook, sizeof(hook));

    const vm_size_t page_size = (vm_size_t)sysconf(_SC_PAGESIZE);
    const uintptr_t page = (uintptr_t)target & ~(page_size - 1);
    const mach_vm_size_t span =
        (mach_vm_size_t)(((uintptr_t)target + sizeof(patch) - page +
                          page_size - 1) &
                         ~(page_size - 1));
    kern_return_t result = mach_vm_protect(
        mach_task_self(), (mach_vm_address_t)page, span, false,
        VM_PROT_READ | VM_PROT_WRITE | VM_PROT_COPY);
    if (result != KERN_SUCCESS) {
        *failure_reason = "protection";
        return false;
    }

    memcpy(target, patch, sizeof(patch));
    sys_icache_invalidate(target, sizeof(patch));
    result = mach_vm_protect(
        mach_task_self(), (mach_vm_address_t)page, span, false,
        VM_PROT_READ | VM_PROT_EXECUTE);
    if (result != KERN_SUCCESS || memcmp(target, patch, sizeof(patch)) != 0) {
        *failure_reason = "write";
        return false;
    }
    return true;
}
#endif

static uint8_t *resolve_target(const char **failure_reason) {
#if defined(CDUMM_JUST_TAKE_TESTING)
    (void)failure_reason;
    memcpy(g_actual_uuid, k_supported_uuid, sizeof(g_actual_uuid));
    return (uint8_t *)(uintptr_t)&cdumm_just_take_test_target;
#else
    const struct mach_header_64 *header = NULL;
    const uint32_t image_count = _dyld_image_count();
    for (uint32_t image_index = 0; image_index < image_count; ++image_index) {
        const struct mach_header *candidate =
            _dyld_get_image_header(image_index);
        if (candidate == NULL || candidate->magic != MH_MAGIC_64 ||
            candidate->cputype != CPU_TYPE_ARM64) {
            continue;
        }
        const struct mach_header_64 *candidate64 =
            (const struct mach_header_64 *)candidate;
        if (image_uuid_matches(candidate64)) {
            header = candidate64;
            break;
        }
    }
    if (header == NULL) {
        *failure_reason = "uuid";
        return NULL;
    }
    return (uint8_t *)((uintptr_t)header + TARGET_OFFSET);
#endif
}

static uintptr_t g_target_address = 0;
static struct sigaction g_previous_sigtrap;

#define CDUMM_BCR_ENABLE UINT64_C(1)
#define CDUMM_BCR_USER (UINT64_C(2) << 1)
#define CDUMM_BCR_BAS_ALL (UINT64_C(0xF) << 5)

static void forward_sigtrap(int signal_number, siginfo_t *info, void *context) {
    if ((g_previous_sigtrap.sa_flags & SA_SIGINFO) != 0 &&
        g_previous_sigtrap.sa_sigaction != NULL) {
        g_previous_sigtrap.sa_sigaction(signal_number, info, context);
        return;
    }
    if (g_previous_sigtrap.sa_handler == SIG_IGN) {
        return;
    }
    if (g_previous_sigtrap.sa_handler != NULL &&
        g_previous_sigtrap.sa_handler != SIG_DFL) {
        g_previous_sigtrap.sa_handler(signal_number);
        return;
    }
    (void)signal(SIGTRAP, SIG_DFL);
    (void)raise(SIGTRAP);
}

static void hardware_breakpoint_handler(
    int signal_number, siginfo_t *info, void *context
) {
    ucontext_t *user_context = (ucontext_t *)context;
    if (user_context == NULL || user_context->uc_mcontext == NULL) {
        forward_sigtrap(signal_number, info, context);
        return;
    }

    arm_thread_state64_t *state = &user_context->uc_mcontext->__ss;
    const uintptr_t pc =
        (uintptr_t)arm_thread_state64_get_pc(*state);
    if (pc != g_target_address) {
        forward_sigtrap(signal_number, info, context);
        return;
    }

    state->__x[0] = PATCHED_RESULT;
    void *return_address = arm_thread_state64_get_lr_fptr(*state);
    if (return_address == NULL) {
        return_address = (void *)(uintptr_t)
            arm_thread_state64_get_lr(*state);
    }
    arm_thread_state64_set_pc_fptr(*state, return_address);
    (void)atomic_fetch_add_explicit(
        &g_hit_count, UINT64_C(1), memory_order_release);
}

static bool arm_breakpoint_for_thread(thread_t thread) {
    arm_debug_state64_t debug_state = {0};
    mach_msg_type_number_t count = ARM_DEBUG_STATE64_COUNT;
    kern_return_t result = thread_get_state(
        thread,
        ARM_DEBUG_STATE64,
        (thread_state_t)&debug_state,
        &count
    );
    if (result != KERN_SUCCESS) {
        return false;
    }

    int free_slot = -1;
    for (int index = 0; index < 16; ++index) {
        if (
            (debug_state.__bcr[index] & CDUMM_BCR_ENABLE) != 0 &&
            debug_state.__bvr[index] == g_target_address
        ) {
            return true;
        }
        if (
            free_slot < 0 &&
            (debug_state.__bcr[index] & CDUMM_BCR_ENABLE) == 0
        ) {
            free_slot = index;
        }
    }
    if (free_slot < 0) {
        return false;
    }

    debug_state.__bvr[free_slot] = g_target_address;
    debug_state.__bcr[free_slot] =
        CDUMM_BCR_ENABLE | CDUMM_BCR_USER | CDUMM_BCR_BAS_ALL;
    return thread_set_state(
        thread,
        ARM_DEBUG_STATE64,
        (thread_state_t)&debug_state,
        ARM_DEBUG_STATE64_COUNT
    ) == KERN_SUCCESS;
}

static void *hardware_breakpoint_worker(void *unused) {
    (void)unused;
    bool reported_armed = false;
    uint64_t last_hits = 0;
    for (;;) {
        thread_act_array_t threads = NULL;
        mach_msg_type_number_t thread_count = 0;
        bool armed_any = false;
        if (
            task_threads(mach_task_self(), &threads, &thread_count) ==
            KERN_SUCCESS
        ) {
            for (mach_msg_type_number_t index = 0;
                 index < thread_count;
                 ++index) {
                if (arm_breakpoint_for_thread(threads[index])) {
                    armed_any = true;
                }
                (void)mach_port_deallocate(
                    mach_task_self(), threads[index]);
            }
            (void)vm_deallocate(
                mach_task_self(),
                (vm_address_t)threads,
                (vm_size_t)(thread_count * sizeof(thread_t))
            );
        }

        if (armed_any && !reported_armed) {
            write_handshake("armed", g_target_address);
            reported_armed = true;
        }
        const uint64_t hits =
            atomic_load_explicit(&g_hit_count, memory_order_acquire);
        if (hits != last_hits) {
            write_handshake("patched", g_target_address);
            last_hits = hits;
        }
        usleep(250000);
    }
}

static bool install_hardware_breakpoint_hook(
    uint8_t *target, const char **failure_reason
) {
    if (memcmp(target, k_target_signature, sizeof(k_target_signature)) != 0) {
        *failure_reason = "signature";
        return false;
    }

    struct sigaction action = {0};
    action.sa_sigaction = hardware_breakpoint_handler;
    action.sa_flags = SA_SIGINFO | SA_NODEFER;
    (void)sigemptyset(&action.sa_mask);
    if (sigaction(SIGTRAP, &action, &g_previous_sigtrap) != 0) {
        *failure_reason = "sigtrap";
        return false;
    }

    g_target_address = (uintptr_t)target;
    g_hook_mode = "hardware-breakpoint";
    pthread_t thread;
    if (
        pthread_create(
            &thread, NULL, hardware_breakpoint_worker, NULL
        ) != 0
    ) {
        (void)sigaction(SIGTRAP, &g_previous_sigtrap, NULL);
        *failure_reason = "thread";
        return false;
    }
    pthread_detach(thread);
    return true;
}

#if !defined(CDUMM_JUST_TAKE_TESTING)
static void probe_writable_pointer_references(void) {
    const uint32_t image_count = _dyld_image_count();
    for (uint32_t image_index = 0; image_index < image_count; ++image_index) {
        const struct mach_header *candidate =
            _dyld_get_image_header(image_index);
        if (candidate == NULL || candidate->magic != MH_MAGIC_64 ||
            candidate->cputype != CPU_TYPE_ARM64) {
            continue;
        }
        const struct mach_header_64 *header =
            (const struct mach_header_64 *)candidate;
        if (!image_uuid_matches(header)) {
            continue;
        }

        const intptr_t slide = _dyld_get_image_vmaddr_slide(image_index);
        const uintptr_t target = (uintptr_t)header + TARGET_OFFSET;
        const uint8_t *cursor = (const uint8_t *)(header + 1);
        for (uint32_t command_index = 0;
             command_index < header->ncmds;
             ++command_index) {
            const struct load_command *command =
                (const struct load_command *)cursor;
            if (command->cmd == LC_SEGMENT_64 &&
                command->cmdsize >= sizeof(struct segment_command_64)) {
                const struct segment_command_64 *segment =
                    (const struct segment_command_64 *)command;
                const bool data_segment =
                    strncmp(segment->segname, "__DATA", 6) == 0 ||
                    strncmp(segment->segname, "__AUTH", 6) == 0;
                if (data_segment &&
                    (segment->initprot & VM_PROT_READ) != 0 &&
                    (segment->initprot & VM_PROT_EXECUTE) == 0) {
                    uintptr_t start =
                        (uintptr_t)((intptr_t)segment->vmaddr + slide);
                    const uintptr_t end = start + segment->vmsize;
                    start = (start + sizeof(uintptr_t) - 1) &
                            ~(sizeof(uintptr_t) - 1);
                    for (uintptr_t address = start;
                         address + sizeof(uintptr_t) <= end;
                         address += sizeof(uintptr_t)) {
                        uintptr_t value = 0;
                        memcpy(&value, (const void *)address, sizeof(value));
                        if (value == target) {
                            if (g_pointer_count == 0) {
                                g_first_pointer_address = address;
                            }
                            ++g_pointer_count;
                        }
                    }
                }
            }
            cursor += command->cmdsize;
        }
        write_handshake("probe-complete", target);
        return;
    }
    write_handshake("probe-no-image", 0);
}
#endif

#if defined(CDUMM_JUST_TAKE_TESTING)
static void *receipt_worker(void *unused) {
    (void)unused;
    uint64_t last_hits = 0;
    for (;;) {
        const uint64_t hits =
            atomic_load_explicit(&g_hit_count, memory_order_acquire);
        if (hits != last_hits) {
            write_handshake("patched", g_target_address);
            last_hits = hits;
        }
        usleep(250000);
    }
}
#endif

__attribute__((constructor))
static void cdumm_just_take_start(void) {
    const char *failure_reason = "unknown";
    uint8_t *target = resolve_target(&failure_reason);
    if (target == NULL) {
        char status[64];
        (void)snprintf(
            status, sizeof(status), "rejected-%s", failure_reason);
        write_handshake(status, (uintptr_t)target);
        return;
    }

#if defined(CDUMM_JUST_TAKE_TESTING)
    const char *unsafe_test = getenv("CDUMM_ALLOW_UNSAFE_TEXT_PATCH");
    if (
        unsafe_test != NULL &&
        strcmp(unsafe_test, "isolated-test-only") == 0
    ) {
        g_hook_mode = "unsafe-text-isolated-test";
        if (!install_hook(target, &failure_reason)) {
            char status[64];
            (void)snprintf(
                status, sizeof(status), "rejected-%s", failure_reason);
            write_handshake(status, (uintptr_t)target);
            return;
        }
        g_target_address = (uintptr_t)target;
        write_handshake("patched", g_target_address);
        pthread_t thread;
        if (pthread_create(&thread, NULL, receipt_worker, NULL) == 0) {
            pthread_detach(thread);
        }
        return;
    }
#endif

    if (!install_hardware_breakpoint_hook(target, &failure_reason)) {
        char status[64];
        (void)snprintf(
            status, sizeof(status), "rejected-%s", failure_reason);
        write_handshake(status, (uintptr_t)target);
#if !defined(CDUMM_JUST_TAKE_TESTING)
        probe_writable_pointer_references();
#endif
    }
}
