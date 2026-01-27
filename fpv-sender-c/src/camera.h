/*
 * FPV Sender - Camera Capture using libcamera
 */

#ifndef FPV_CAMERA_H
#define FPV_CAMERA_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

/* Forward declaration */
typedef struct fpv_camera fpv_camera_t;

/* Camera configuration */
typedef struct
{
    int width;
    int height;
    int fps;
    int rotation; /* 0, 90, 180, 270 */
    bool hflip;
    bool vflip;
    const char *camera_name; /* NULL for first available */
} fpv_camera_config_t;

/* Default config */
#define FPV_CAMERA_CONFIG_DEFAULT { \
    .width = 1280,                  \
    .height = 720,                  \
    .fps = 60,                      \
    .rotation = 0,                  \
    .hflip = false,                 \
    .vflip = false,                 \
    .camera_name = NULL}

/* Captured frame in YUV420 format */
typedef struct
{
    uint8_t *y_plane;
    uint8_t *u_plane;
    uint8_t *v_plane;
    int y_stride;
    int uv_stride;
    int width;
    int height;
    uint64_t timestamp_us;
    int dma_fd;   /* DMA buffer fd for V4L2 M2M zero-copy */
    void *opaque; /* Internal use */
} fpv_camera_frame_t;

/* Frame callback */
typedef void (*fpv_camera_callback_t)(const fpv_camera_frame_t *frame, void *userdata);

/*
 * Create and start camera capture
 * callback will be invoked from camera thread for each frame
 * Returns: camera handle or NULL on error
 */
fpv_camera_t *fpv_camera_create(const fpv_camera_config_t *config,
                                fpv_camera_callback_t callback,
                                void *userdata);

/*
 * Stop and destroy camera
 */
void fpv_camera_destroy(fpv_camera_t *cam);

/*
 * Release a frame back to the camera pool
 * Must be called after processing each frame
 */
void fpv_camera_release_frame(fpv_camera_t *cam, fpv_camera_frame_t *frame);

/*
 * Get camera info
 */
int fpv_camera_get_width(fpv_camera_t *cam);
int fpv_camera_get_height(fpv_camera_t *cam);
int fpv_camera_get_fps(fpv_camera_t *cam);

#endif /* FPV_CAMERA_H */
