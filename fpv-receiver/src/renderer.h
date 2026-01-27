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
} fpv_renderer_stats_t;

/* Create renderer (call after OpenGL context is current) */
fpv_renderer_t *fpv_renderer_create(void);

/* Destroy renderer */
void fpv_renderer_destroy(fpv_renderer_t *r);

/* Update with new decoded frame (single slot - always uses latest) */
void fpv_renderer_update_frame(fpv_renderer_t *r, fpv_decoded_frame_t *frame);

/* Render current frame to viewport */
void fpv_renderer_draw(fpv_renderer_t *r, int viewport_width, int viewport_height);

/* Check if renderer has a frame */
bool fpv_renderer_has_frame(fpv_renderer_t *r);

/* Get statistics */
fpv_renderer_stats_t fpv_renderer_get_stats(fpv_renderer_t *r);

#endif /* FPV_RENDERER_H */
