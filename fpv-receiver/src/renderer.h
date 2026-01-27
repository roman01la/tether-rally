/*
 * OpenGL Renderer - Renders decoded video frames via texture upload
 *
 * Uses legacy OpenGL for simplicity (works on macOS with compatibility profile)
 */

#ifndef FPV_RENDERER_H
#define FPV_RENDERER_H

#include "decoder.h"
#include <stdbool.h>

/* Renderer state */
typedef struct fpv_renderer fpv_renderer_t;

/* Renderer statistics */
typedef struct
{
    uint64_t frames_rendered;
    uint64_t frames_skipped;
    double last_frame_time_ms;
    /* Pipeline timing (microseconds, exponential moving average) */
    double avg_assembly_us; /* Packet arrival → assembly complete */
    double avg_decode_us;   /* Assembly complete → decode complete */
    double avg_upload_us;   /* Decode complete → texture upload complete */
    double avg_total_us;    /* First packet → texture ready */
    /* Frame timing jitter (microseconds) */
    double avg_frame_interval_us; /* Average time between frames */
    double frame_jitter_us;       /* Jitter (stddev) of frame intervals */
} fpv_renderer_stats_t;

/* Create renderer (call after OpenGL context is current) */
fpv_renderer_t *fpv_renderer_create(void);

/* Destroy renderer */
void fpv_renderer_destroy(fpv_renderer_t *r);

/* Update with new decoded frame (single slot - always uses latest)
 * timing_us array: [0]=first_packet, [1]=assembly_complete, [2]=decode_complete */
void fpv_renderer_update_frame_with_timing(fpv_renderer_t *r, fpv_decoded_frame_t *frame,
                                           const uint64_t *timing_us);

/* Update with new decoded frame (no timing) */
void fpv_renderer_update_frame(fpv_renderer_t *r, fpv_decoded_frame_t *frame);

/* Render current frame to viewport */
void fpv_renderer_draw(fpv_renderer_t *r, int viewport_width, int viewport_height);

/* Check if renderer has a frame */
bool fpv_renderer_has_frame(fpv_renderer_t *r);

/* Get statistics */
fpv_renderer_stats_t fpv_renderer_get_stats(fpv_renderer_t *r);

#endif /* FPV_RENDERER_H */
