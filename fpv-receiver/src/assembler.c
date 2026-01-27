/*
 * Frame Assembler - Implementation
 */

#include "assembler.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <time.h>

#ifdef __APPLE__
#include <mach/mach_time.h>
static mach_timebase_info_data_t timebase_info;
#endif

uint64_t fpv_get_time_us(void)
{
#ifdef __APPLE__
    if (timebase_info.denom == 0)
    {
        mach_timebase_info(&timebase_info);
    }
    uint64_t mach_time = mach_absolute_time();
    return (mach_time * timebase_info.numer / timebase_info.denom) / 1000;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
#endif
}

int fpv_assembler_init(fpv_assembler_t *asm_)
{
    memset(asm_, 0, sizeof(*asm_));

    if (pthread_mutex_init(&asm_->latest_au_mutex, NULL) != 0)
    {
        return -1;
    }

    /* Allocate AU data buffer */
    asm_->latest_au.data = malloc(FPV_MAX_AU_SIZE);
    if (!asm_->latest_au.data)
    {
        pthread_mutex_destroy(&asm_->latest_au_mutex);
        return -1;
    }

    return 0;
}

void fpv_assembler_destroy(fpv_assembler_t *asm_)
{
    pthread_mutex_destroy(&asm_->latest_au_mutex);
    free(asm_->latest_au.data);
}

/* Find or allocate slot for frame assembly */
static fpv_frame_assembly_t *find_or_create_slot(fpv_assembler_t *asm_, uint32_t frame_id)
{
    /* Look for existing slot */
    for (int i = 0; i < FPV_MAX_INFLIGHT_FRAMES; i++)
    {
        if (asm_->frames[i].active && asm_->frames[i].frame_id == frame_id)
        {
            return &asm_->frames[i];
        }
    }

    /* Find empty slot */
    for (int i = 0; i < FPV_MAX_INFLIGHT_FRAMES; i++)
    {
        if (!asm_->frames[i].active)
        {
            return &asm_->frames[i];
        }
    }

    /* No empty slot - find oldest and drop it */
    fpv_frame_assembly_t *oldest = &asm_->frames[0];
    for (int i = 1; i < FPV_MAX_INFLIGHT_FRAMES; i++)
    {
        if (fpv_is_older(asm_->frames[i].frame_id, oldest->frame_id))
        {
            oldest = &asm_->frames[i];
        }
    }

    if (oldest->active)
    {
        asm_->stats.frames_dropped_overflow++;
    }
    oldest->active = false;
    return oldest;
}

/* Drop all frames older than given frame_id */
static void drop_older_frames(fpv_assembler_t *asm_, uint32_t frame_id)
{
    for (int i = 0; i < FPV_MAX_INFLIGHT_FRAMES; i++)
    {
        if (asm_->frames[i].active && fpv_is_older(asm_->frames[i].frame_id, frame_id))
        {
            asm_->frames[i].active = false;
            asm_->stats.frames_dropped_superseded++;
            /* NOTE: Don't set needs_idr here - superseding is normal for
             * variable network latency. Only request IDR on actual packet loss
             * that causes decode errors or long timeout. */
        }
    }
}

/* Complete a frame and make it the latest AU */
static void complete_frame(fpv_assembler_t *asm_, fpv_frame_assembly_t *frame)
{
    uint64_t now = fpv_get_time_us();

    pthread_mutex_lock(&asm_->latest_au_mutex);

    /* Copy frame data to AU buffer */
    /* Fragments are stored in order, concatenate them */
    size_t total_len = 0;
    for (uint16_t i = 0; i < frame->frag_count && i < FPV_MAX_FRAGMENTS; i++)
    {
        if (frame->received_mask & (1ULL << i))
        {
            memcpy(asm_->latest_au.data + total_len,
                   frame->data + frame->frag_offsets[i],
                   frame->frag_lengths[i]);
            total_len += frame->frag_lengths[i];
        }
        else
        {
            /* Missing fragment - should not happen for complete frames */
        }
    }

    asm_->latest_au.len = total_len;
    asm_->latest_au.frame_id = frame->frame_id;
    asm_->latest_au.ts_ms = frame->ts_ms;
    asm_->latest_au.is_keyframe = (frame->flags & FPV_FLAG_KEYFRAME) != 0;
    asm_->latest_au.has_spspps = (frame->flags & FPV_FLAG_SPSPPS) != 0;
    /* Timing telemetry */
    asm_->latest_au.first_packet_time_us = frame->first_seen_us;
    asm_->latest_au.assembly_complete_us = now;
    asm_->has_latest_au = true;

    pthread_mutex_unlock(&asm_->latest_au_mutex);

    asm_->stats.frames_completed++;
    frame->active = false;
}

int fpv_assembler_add_fragment(fpv_assembler_t *asm_, const fpv_video_fragment_t *frag)
{
    asm_->stats.fragments_received++;

    /* Drop if too old */
    if (asm_->have_newest && fpv_is_older(frag->frame_id, asm_->newest_frame_id))
    {
        /* Allow 1 frame behind for reordering */
        if ((int32_t)(asm_->newest_frame_id - frag->frame_id) > 1)
        {
            return 0; /* Silently drop */
        }
    }

    /* Update newest seen frame_id */
    if (!asm_->have_newest || fpv_is_newer(frag->frame_id, asm_->newest_frame_id))
    {
        /* New frame arrived - drop older incomplete frames */
        if (asm_->have_newest)
        {
            drop_older_frames(asm_, frag->frame_id);
        }
        asm_->newest_frame_id = frag->frame_id;
        asm_->have_newest = true;
    }

    /* Validate fragment */
    if (frag->frag_count > FPV_MAX_FRAGMENTS)
    {
        return -1; /* Too many fragments */
    }
    if (frag->frag_index >= frag->frag_count)
    {
        return -1; /* Invalid index */
    }

    /* Find or create assembly slot */
    fpv_frame_assembly_t *frame = find_or_create_slot(asm_, frag->frame_id);

    if (!frame->active)
    {
        /* Initialize new frame assembly */
        memset(frame, 0, sizeof(*frame));
        frame->frame_id = frag->frame_id;
        frame->ts_ms = frag->ts_ms;
        frame->first_seen_us = fpv_get_time_us();
        frame->frag_count = frag->frag_count;
        frame->flags = frag->flags;
        frame->active = true;
    }

    /* Check for duplicate fragment */
    if (frame->received_mask & (1ULL << frag->frag_index))
    {
        asm_->stats.duplicate_fragments++;
        return 0;
    }

    /* Store fragment data */
    size_t offset = frame->data_len;
    if (offset + frag->payload_len > FPV_MAX_AU_SIZE)
    {
        return -2; /* AU too large */
    }

    memcpy(frame->data + offset, frag->payload, frag->payload_len);
    frame->frag_offsets[frag->frag_index] = (uint16_t)offset;
    frame->frag_lengths[frag->frag_index] = frag->payload_len;
    frame->data_len += frag->payload_len;
    frame->received_mask |= (1ULL << frag->frag_index);
    frame->frags_received++;

    /* Update flags from any fragment (all should have same flags) */
    frame->flags |= frag->flags;

    /* Check if complete */
    if (frame->frags_received == frame->frag_count)
    {
        complete_frame(asm_, frame);
    }

    return 0;
}

void fpv_assembler_check_timeouts(fpv_assembler_t *asm_)
{
    uint64_t now = fpv_get_time_us();
    uint64_t timeout_us = FPV_FRAME_TIMEOUT_MS * 1000;

    for (int i = 0; i < FPV_MAX_INFLIGHT_FRAMES; i++)
    {
        fpv_frame_assembly_t *frame = &asm_->frames[i];
        if (frame->active)
        {
            if (now - frame->first_seen_us > timeout_us)
            {
                frame->active = false;
                asm_->stats.frames_dropped_timeout++;
                /* Timeout indicates real packet loss - request IDR */
                asm_->needs_idr = true;
            }
        }
    }
}

fpv_access_unit_t *fpv_assembler_get_au(fpv_assembler_t *asm_)
{
    fpv_access_unit_t *result = NULL;

    pthread_mutex_lock(&asm_->latest_au_mutex);

    if (asm_->has_latest_au)
    {
        /* Allocate and copy */
        result = malloc(sizeof(fpv_access_unit_t));
        if (result)
        {
            result->data = malloc(asm_->latest_au.len);
            if (result->data)
            {
                memcpy(result->data, asm_->latest_au.data, asm_->latest_au.len);
                result->len = asm_->latest_au.len;
                result->frame_id = asm_->latest_au.frame_id;
                result->ts_ms = asm_->latest_au.ts_ms;
                result->is_keyframe = asm_->latest_au.is_keyframe;
                result->has_spspps = asm_->latest_au.has_spspps;
                /* Copy timing telemetry */
                result->first_packet_time_us = asm_->latest_au.first_packet_time_us;
                result->assembly_complete_us = asm_->latest_au.assembly_complete_us;
            }
            else
            {
                free(result);
                result = NULL;
            }
        }
        asm_->has_latest_au = false;
    }

    pthread_mutex_unlock(&asm_->latest_au_mutex);

    return result;
}

void fpv_access_unit_free(fpv_access_unit_t *au)
{
    if (au)
    {
        free(au->data);
        free(au);
    }
}

fpv_assembler_stats_t fpv_assembler_get_stats(fpv_assembler_t *asm_)
{
    return asm_->stats;
}

bool fpv_assembler_needs_idr(fpv_assembler_t *asm_)
{
    return asm_->needs_idr;
}

void fpv_assembler_clear_idr_request(fpv_assembler_t *asm_)
{
    asm_->needs_idr = false;
}
