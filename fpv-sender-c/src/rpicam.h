/*
 * FPV Sender - Camera+Encoder using rpicam-vid
 *
 * On Raspberry Pi with libcamera stack, direct V4L2 camera access isn't
 * available for YUV capture. This module spawns rpicam-vid which handles
 * both camera capture and H.264 encoding via the hardware encoder.
 *
 * We read H.264 output from rpicam-vid's stdout with optimized buffering.
 */

#ifndef FPV_RPICAM_H
#define FPV_RPICAM_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct fpv_rpicam fpv_rpicam_t;

/* Configuration */
typedef struct
{
    int width;
    int height;
    int fps;
    int bitrate_kbps;
    int idr_interval;
    int shutter_us; /* 0 = auto */
    int gain;       /* 0 = auto, in 0.01 units */
    bool hflip;
    bool vflip;
    int rotation; /* 0, 90, 180, 270 */
} fpv_rpicam_config_t;

#define FPV_RPICAM_CONFIG_DEFAULT \
    {                             \
        .width = 1280, .height = 720, .fps = 60, .bitrate_kbps = 2000, .idr_interval = 30, .shutter_us = 0, .gain = 0, .hflip = false, .vflip = false, .rotation = 0}

/* Encoded frame from rpicam-vid */
typedef struct
{
    uint8_t *data;
    size_t len;
    uint32_t frame_id;
    uint64_t timestamp_us;
    bool is_keyframe;
    bool has_spspps;
} fpv_rpicam_frame_t;

/* Statistics */
typedef struct
{
    uint64_t frames_read;
    uint64_t bytes_read;
    uint64_t keyframes;
    uint64_t read_errors;
} fpv_rpicam_stats_t;

/* Callback for encoded frames */
typedef void (*fpv_rpicam_callback_t)(const fpv_rpicam_frame_t *frame,
                                      void *userdata);

/*
 * Create and start rpicam-vid capture
 * Returns handle or NULL on error
 */
fpv_rpicam_t *fpv_rpicam_create(const fpv_rpicam_config_t *config,
                                fpv_rpicam_callback_t callback, void *userdata);

/*
 * Stop and destroy
 */
void fpv_rpicam_destroy(fpv_rpicam_t *cam);

/*
 * Request an IDR frame (sends SIGUSR1 to rpicam-vid)
 */
void fpv_rpicam_request_idr(fpv_rpicam_t *cam);

/*
 * Get statistics
 */
fpv_rpicam_stats_t fpv_rpicam_get_stats(fpv_rpicam_t *cam);

#endif /* FPV_RPICAM_H */
