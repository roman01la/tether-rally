/*
 * Video Decoder - Platform abstraction for H.264 decoding
 */

#ifndef FPV_DECODER_H
#define FPV_DECODER_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

/* Decoded frame */
typedef struct
{
    void *native_handle; /* Platform-specific (CVPixelBufferRef on macOS) */
    int width;
    int height;
    uint32_t frame_id;
    uint32_t ts_ms;
} fpv_decoded_frame_t;

/* Decoder state */
typedef struct fpv_decoder fpv_decoder_t;

/* Decoder statistics */
typedef struct
{
    uint64_t frames_decoded;
    uint64_t decode_errors;
    uint64_t keyframes_decoded;
} fpv_decoder_stats_t;

/* Create decoder */
fpv_decoder_t *fpv_decoder_create(void);

/* Destroy decoder */
void fpv_decoder_destroy(fpv_decoder_t *dec);

/*
 * Decode an Access Unit (H.264 Annex B format)
 *
 * Returns 0 on success, negative on error.
 * On success, caller must call fpv_decoder_release_frame() when done.
 */
int fpv_decoder_decode(fpv_decoder_t *dec,
                       const uint8_t *data, size_t len,
                       uint32_t frame_id, uint32_t ts_ms,
                       bool is_keyframe,
                       fpv_decoded_frame_t *out_frame);

/* Release a decoded frame */
void fpv_decoder_release_frame(fpv_decoded_frame_t *frame);

/* Check if decoder needs a keyframe to continue */
bool fpv_decoder_needs_keyframe(fpv_decoder_t *dec);

/* Reset decoder state (call after errors) */
void fpv_decoder_reset(fpv_decoder_t *dec);

/* Get decoder statistics */
fpv_decoder_stats_t fpv_decoder_get_stats(fpv_decoder_t *dec);

#endif /* FPV_DECODER_H */
