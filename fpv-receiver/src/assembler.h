/*
 * Frame Assembler - Reassembles video fragments into complete Access Units
 *
 * Implements the "no-queue" rules from FPV_PLAN.md Appendix E:
 * - MAX_INFLIGHT_FRAMES = 4
 * - FRAME_TIMEOUT_MS = 20ms
 * - latest_complete_AU = single slot (overwrite)
 * - Drop old frames when newer arrives
 */

#ifndef FPV_ASSEMBLER_H
#define FPV_ASSEMBLER_H

#include "protocol.h"
#include <stdint.h>
#include <stdbool.h>
#include <pthread.h>

/* Maximum fragments per frame (enough for ~70KB frame at 1200 byte payload) */
#define FPV_MAX_FRAGMENTS 64

/* Maximum AU data size */
#define FPV_MAX_AU_SIZE (128 * 1024) /* 128 KB */

/* In-flight frame assembly state */
typedef struct
{
    uint32_t frame_id;
    uint32_t ts_ms;
    uint64_t first_seen_us; /* Monotonic time when first fragment arrived */
    uint16_t frag_count;
    uint16_t frags_received;
    uint8_t flags;
    bool active;

    /* Fragment bitmap and data */
    uint64_t received_mask; /* Bitmap of received fragments (up to 64) */
    uint16_t frag_offsets[FPV_MAX_FRAGMENTS];
    uint16_t frag_lengths[FPV_MAX_FRAGMENTS];
    uint8_t data[FPV_MAX_AU_SIZE];
    size_t data_len;
} fpv_frame_assembly_t;

/* Complete AU ready for decode */
typedef struct
{
    uint8_t *data;
    size_t len;
    uint32_t frame_id;
    uint32_t ts_ms;
    bool is_keyframe;
    bool has_spspps;
} fpv_access_unit_t;

/* Assembler statistics */
typedef struct
{
    uint64_t fragments_received;
    uint64_t frames_completed;
    uint64_t frames_dropped_timeout;
    uint64_t frames_dropped_superseded;
    uint64_t frames_dropped_overflow;
    uint64_t duplicate_fragments;
} fpv_assembler_stats_t;

/* Assembler state */
typedef struct
{
    fpv_frame_assembly_t frames[FPV_MAX_INFLIGHT_FRAMES];
    uint32_t newest_frame_id;
    bool have_newest;

    /* Latest complete AU (single slot per spec) */
    fpv_access_unit_t latest_au;
    bool has_latest_au;
    pthread_mutex_t latest_au_mutex;

    /* Error recovery */
    bool needs_idr; /* Set when frames are dropped, cleared on keyframe */

    /* Stats */
    fpv_assembler_stats_t stats;
} fpv_assembler_t;

/* Initialize assembler */
int fpv_assembler_init(fpv_assembler_t *asm_);

/* Destroy assembler */
void fpv_assembler_destroy(fpv_assembler_t *asm_);

/* Process incoming video fragment */
int fpv_assembler_add_fragment(fpv_assembler_t *asm_, const fpv_video_fragment_t *frag);

/* Check for timed-out frames (call periodically) */
void fpv_assembler_check_timeouts(fpv_assembler_t *asm_);

/* Get latest complete AU (transfers ownership, returns NULL if none) */
fpv_access_unit_t *fpv_assembler_get_au(fpv_assembler_t *asm_);

/* Free an access unit */
void fpv_access_unit_free(fpv_access_unit_t *au);

/* Check if IDR is needed (returns true once, then clears flag) */
bool fpv_assembler_needs_idr(fpv_assembler_t *asm_);

/* Clear needs_idr flag (call when keyframe is successfully decoded) */
void fpv_assembler_clear_idr_request(fpv_assembler_t *asm_);

/* Get statistics */
fpv_assembler_stats_t fpv_assembler_get_stats(fpv_assembler_t *asm_);

/* Get current time in microseconds */
uint64_t fpv_get_time_us(void);

#endif /* FPV_ASSEMBLER_H */
