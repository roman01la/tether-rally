/*
 * FPV Sender - H.264 Encoder using V4L2 M2M
 *
 * Uses the Raspberry Pi's hardware H.264 encoder via V4L2 memory-to-memory API.
 * This avoids the pipe overhead of rpicam-vid by directly interfacing with
 * the encoder hardware.
 */

#ifndef FPV_SENDER_ENCODER_H
#define FPV_SENDER_ENCODER_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "camera.h"

/* H.264 Profile */
typedef enum
{
    FPV_PROFILE_BASELINE = 0,
    FPV_PROFILE_MAIN,
    FPV_PROFILE_HIGH
} fpv_encoder_profile_t;

/* H.264 Level */
typedef enum
{
    FPV_LEVEL_31 = 31,
    FPV_LEVEL_40 = 40,
    FPV_LEVEL_41 = 41,
    FPV_LEVEL_42 = 42
} fpv_encoder_level_t;

/* Encoder configuration */
typedef struct
{
    int width;
    int height;
    int fps;
    int bitrate_kbps; /* kbits per second */
    int idr_interval; /* frames between IDR */
    fpv_encoder_profile_t profile;
    fpv_encoder_level_t level;
} fpv_encoder_config_t;

/* Default configuration for 720p60 FPV */
#define FPV_ENCODER_CONFIG_DEFAULT { \
    .width = 1280,                   \
    .height = 720,                   \
    .fps = 60,                       \
    .bitrate_kbps = 2000,            \
    .idr_interval = 30,              \
    .profile = FPV_PROFILE_BASELINE, \
    .level = FPV_LEVEL_31}

/* Encoded frame */
typedef struct
{
    uint8_t *data;         /* H.264 Annex B data (NAL units with start codes) */
    size_t len;            /* Data length */
    uint32_t frame_id;     /* Monotonic frame counter */
    uint64_t timestamp_us; /* Capture timestamp (monotonic) */
    bool is_keyframe;      /* IDR frame */
    bool has_spspps;       /* Contains SPS/PPS */
} fpv_encoded_frame_t;

/* Encoder stats */
typedef struct
{
    uint64_t frames_in;  /* Frames queued to encoder */
    uint64_t frames_out; /* Frames received from encoder */
    uint64_t bytes_out;  /* Total encoded bytes */
    uint64_t idr_count;  /* Number of IDR frames */
} fpv_encoder_stats_t;

/* Encoder state (opaque) */
typedef struct fpv_encoder fpv_encoder_t;

/* Callback for encoded frames */
typedef void (*fpv_encoder_callback_t)(const fpv_encoded_frame_t *frame, void *user_data);

/* Create encoder with configuration. Callback is invoked for each encoded frame. */
fpv_encoder_t *fpv_encoder_create(const fpv_encoder_config_t *config,
                                  fpv_encoder_callback_t callback, void *user_data);

/* Destroy encoder */
void fpv_encoder_destroy(fpv_encoder_t *enc);

/* Encode a camera frame (thread-safe, can be called from camera callback) */
int fpv_encoder_encode(fpv_encoder_t *enc, const fpv_camera_frame_t *frame);

/* Request an IDR frame (will be emitted ASAP) */
void fpv_encoder_request_idr(fpv_encoder_t *enc);

/* Get encoder statistics */
fpv_encoder_stats_t fpv_encoder_get_stats(fpv_encoder_t *enc);

#endif /* FPV_SENDER_ENCODER_H */
