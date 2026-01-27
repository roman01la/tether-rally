/*
 * FPV Sender - Camera Capture using V4L2 (Raspberry Pi Camera)
 *
 * On Raspberry Pi, we use /dev/video0 (or another) with the V4L2 API
 * to capture YUV420 frames. This is simpler than libcamera and works
 * well with the hardware encoder.
 */

#include "camera.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <pthread.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <linux/videodev2.h>

#define NUM_BUFFERS 4
#define CAMERA_DEVICE "/dev/video0"

struct buffer
{
    void *start;
    size_t length;
    int dma_fd;
};

struct fpv_camera
{
    int fd;
    fpv_camera_config_t config;
    fpv_camera_callback_t callback;
    void *userdata;

    pthread_t thread;
    volatile bool running;

    struct buffer buffers[NUM_BUFFERS];
    int num_buffers;

    int width;
    int height;
    int fps;
};

static int xioctl(int fd, int request, void *arg)
{
    int r;
    do
    {
        r = ioctl(fd, request, arg);
    } while (r == -1 && errno == EINTR);
    return r;
}

static bool setup_camera(fpv_camera_t *cam)
{
    /* Query capabilities */
    struct v4l2_capability cap;
    if (xioctl(cam->fd, VIDIOC_QUERYCAP, &cap) < 0)
    {
        perror("VIDIOC_QUERYCAP");
        return false;
    }

    if (!(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE))
    {
        fprintf(stderr, "Device does not support video capture\n");
        return false;
    }

    if (!(cap.capabilities & V4L2_CAP_STREAMING))
    {
        fprintf(stderr, "Device does not support streaming\n");
        return false;
    }

    /* Set format - YUV420 planar */
    struct v4l2_format fmt = {0};
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    fmt.fmt.pix.width = cam->config.width;
    fmt.fmt.pix.height = cam->config.height;
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_YUV420;
    fmt.fmt.pix.field = V4L2_FIELD_NONE;

    if (xioctl(cam->fd, VIDIOC_S_FMT, &fmt) < 0)
    {
        perror("VIDIOC_S_FMT");
        return false;
    }

    cam->width = fmt.fmt.pix.width;
    cam->height = fmt.fmt.pix.height;

    printf("Camera format: %dx%d\n", cam->width, cam->height);

    /* Set frame rate */
    struct v4l2_streamparm parm = {0};
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    parm.parm.capture.timeperframe.numerator = 1;
    parm.parm.capture.timeperframe.denominator = cam->config.fps;

    if (xioctl(cam->fd, VIDIOC_S_PARM, &parm) < 0)
    {
        perror("VIDIOC_S_PARM (non-fatal)");
        /* Continue anyway, fps might be fixed */
    }

    cam->fps = parm.parm.capture.timeperframe.denominator;
    printf("Camera FPS: %d\n", cam->fps);

    /* Request buffers */
    struct v4l2_requestbuffers req = {0};
    req.count = NUM_BUFFERS;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;

    if (xioctl(cam->fd, VIDIOC_REQBUFS, &req) < 0)
    {
        perror("VIDIOC_REQBUFS");
        return false;
    }

    if (req.count < 2)
    {
        fprintf(stderr, "Insufficient buffer memory\n");
        return false;
    }

    cam->num_buffers = req.count;

    /* Map buffers */
    for (int i = 0; i < cam->num_buffers; i++)
    {
        struct v4l2_buffer buf = {0};
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;

        if (xioctl(cam->fd, VIDIOC_QUERYBUF, &buf) < 0)
        {
            perror("VIDIOC_QUERYBUF");
            return false;
        }

        cam->buffers[i].length = buf.length;
        cam->buffers[i].start = mmap(NULL, buf.length, PROT_READ | PROT_WRITE,
                                     MAP_SHARED, cam->fd, buf.m.offset);

        if (cam->buffers[i].start == MAP_FAILED)
        {
            perror("mmap");
            return false;
        }
        cam->buffers[i].dma_fd = -1;
    }

    /* Queue buffers */
    for (int i = 0; i < cam->num_buffers; i++)
    {
        struct v4l2_buffer buf = {0};
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;

        if (xioctl(cam->fd, VIDIOC_QBUF, &buf) < 0)
        {
            perror("VIDIOC_QBUF");
            return false;
        }
    }

    /* Start streaming */
    enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (xioctl(cam->fd, VIDIOC_STREAMON, &type) < 0)
    {
        perror("VIDIOC_STREAMON");
        return false;
    }

    return true;
}

static void *camera_thread(void *arg)
{
    fpv_camera_t *cam = arg;

    while (cam->running)
    {
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(cam->fd, &fds);

        struct timeval tv = {.tv_sec = 1, .tv_usec = 0};
        int r = select(cam->fd + 1, &fds, NULL, NULL, &tv);

        if (r == -1)
        {
            if (errno == EINTR)
                continue;
            perror("select");
            break;
        }

        if (r == 0)
            continue; /* Timeout */

        /* Dequeue buffer */
        struct v4l2_buffer buf = {0};
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;

        if (xioctl(cam->fd, VIDIOC_DQBUF, &buf) < 0)
        {
            if (errno == EAGAIN)
                continue;
            perror("VIDIOC_DQBUF");
            break;
        }

        /* Build frame structure */
        uint8_t *data = cam->buffers[buf.index].start;
        int y_size = cam->width * cam->height;
        int uv_size = y_size / 4;

        fpv_camera_frame_t frame = {
            .y_plane = data,
            .u_plane = data + y_size,
            .v_plane = data + y_size + uv_size,
            .y_stride = cam->width,
            .uv_stride = cam->width / 2,
            .width = cam->width,
            .height = cam->height,
            .timestamp_us = buf.timestamp.tv_sec * 1000000ULL + buf.timestamp.tv_usec,
            .dma_fd = -1,
            .opaque = (void *)(intptr_t)buf.index};

        /* Invoke callback */
        if (cam->callback)
        {
            cam->callback(&frame, cam->userdata);
        }

        /* Re-queue buffer */
        if (xioctl(cam->fd, VIDIOC_QBUF, &buf) < 0)
        {
            perror("VIDIOC_QBUF");
            break;
        }
    }

    return NULL;
}

fpv_camera_t *fpv_camera_create(const fpv_camera_config_t *config,
                                fpv_camera_callback_t callback,
                                void *userdata)
{
    fpv_camera_t *cam = calloc(1, sizeof(fpv_camera_t));
    if (!cam)
        return NULL;

    if (config)
    {
        cam->config = *config;
    }
    else
    {
        cam->config = (fpv_camera_config_t)FPV_CAMERA_CONFIG_DEFAULT;
    }

    cam->callback = callback;
    cam->userdata = userdata;

    /* Open camera device */
    const char *device = cam->config.camera_name ? cam->config.camera_name : CAMERA_DEVICE;
    cam->fd = open(device, O_RDWR | O_NONBLOCK);
    if (cam->fd < 0)
    {
        perror("open camera");
        free(cam);
        return NULL;
    }

    if (!setup_camera(cam))
    {
        close(cam->fd);
        free(cam);
        return NULL;
    }

    /* Start capture thread */
    cam->running = true;
    if (pthread_create(&cam->thread, NULL, camera_thread, cam) != 0)
    {
        perror("pthread_create");
        close(cam->fd);
        free(cam);
        return NULL;
    }

    printf("Camera started: %dx%d @ %dfps\n", cam->width, cam->height, cam->fps);

    return cam;
}

void fpv_camera_destroy(fpv_camera_t *cam)
{
    if (!cam)
        return;

    cam->running = false;
    pthread_join(cam->thread, NULL);

    /* Stop streaming */
    enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    xioctl(cam->fd, VIDIOC_STREAMOFF, &type);

    /* Unmap buffers */
    for (int i = 0; i < cam->num_buffers; i++)
    {
        if (cam->buffers[i].start && cam->buffers[i].start != MAP_FAILED)
        {
            munmap(cam->buffers[i].start, cam->buffers[i].length);
        }
    }

    close(cam->fd);
    free(cam);
}

void fpv_camera_release_frame(fpv_camera_t *cam, fpv_camera_frame_t *frame)
{
    /* In the V4L2 flow, buffer is already re-queued in the thread */
    (void)cam;
    (void)frame;
}

int fpv_camera_get_width(fpv_camera_t *cam)
{
    return cam ? cam->width : 0;
}

int fpv_camera_get_height(fpv_camera_t *cam)
{
    return cam ? cam->height : 0;
}

int fpv_camera_get_fps(fpv_camera_t *cam)
{
    return cam ? cam->fps : 0;
}
