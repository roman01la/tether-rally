/*
 * FPV Sender - H.264 Encoder using V4L2 M2M
 *
 * Architecture:
 * 1. Camera module captures raw YUV420 frames
 * 2. V4L2 M2M encoder converts raw → H.264 in hardware
 * 3. Callback delivers encoded NAL units
 *
 * On Raspberry Pi:
 * - Encoder: /dev/video11 (bcm2835-codec) or /dev/video31
 */

#include "encoder.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <pthread.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <linux/videodev2.h>
#include <time.h>

/* V4L2 buffer count */
#define NUM_OUTPUT_BUFFERS 4  /* Raw input */
#define NUM_CAPTURE_BUFFERS 4 /* Encoded output */

/* Maximum encoded frame size */
#define MAX_ENCODED_SIZE (512 * 1024)

/* NAL unit types */
#define NAL_TYPE_IDR 5
#define NAL_TYPE_SPS 7
#define NAL_TYPE_PPS 8

/* Buffer structure */
typedef struct
{
    void *start;
    size_t length;
    bool queued;
} buffer_t;

struct fpv_encoder
{
    fpv_encoder_config_t config;

    /* V4L2 encoder (M2M device) */
    int enc_fd;
    buffer_t output_buffers[NUM_OUTPUT_BUFFERS];   /* Raw input */
    buffer_t capture_buffers[NUM_CAPTURE_BUFFERS]; /* Encoded output */

    /* State */
    volatile bool running;
    volatile bool streaming_started;
    volatile bool idr_requested;
    uint32_t frame_id;

    /* Callback */
    fpv_encoder_callback_t callback;
    void *user_data;

    /* Thread for reading encoded output */
    pthread_t capture_thread;
    pthread_mutex_t mutex;
    pthread_cond_t cond;

    /* Input queue - free buffer indices */
    int free_output_buf;

    /* Stats */
    fpv_encoder_stats_t stats;
};

static uint64_t get_time_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

/* Check if NAL unit data contains SPS or PPS */
static bool check_spspps(const uint8_t *data, size_t len)
{
    for (size_t i = 0; i + 4 < len; i++)
    {
        if (data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 0 && data[i + 3] == 1)
        {
            uint8_t nal_type = data[i + 4] & 0x1F;
            if (nal_type == NAL_TYPE_SPS || nal_type == NAL_TYPE_PPS)
            {
                return true;
            }
        }
        if (data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1)
        {
            uint8_t nal_type = data[i + 3] & 0x1F;
            if (nal_type == NAL_TYPE_SPS || nal_type == NAL_TYPE_PPS)
            {
                return true;
            }
        }
    }
    return false;
}

/* Check if NAL unit data contains IDR */
static bool check_idr(const uint8_t *data, size_t len)
{
    for (size_t i = 0; i + 4 < len; i++)
    {
        if (data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 0 && data[i + 3] == 1)
        {
            uint8_t nal_type = data[i + 4] & 0x1F;
            if (nal_type == NAL_TYPE_IDR)
            {
                return true;
            }
        }
        if (data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1)
        {
            uint8_t nal_type = data[i + 3] & 0x1F;
            if (nal_type == NAL_TYPE_IDR)
            {
                return true;
            }
        }
    }
    return false;
}

static int xioctl(int fd, unsigned long request, void *arg)
{
    int r;
    do
    {
        r = ioctl(fd, request, arg);
    } while (r == -1 && errno == EINTR);
    return r;
}

static int setup_encoder(fpv_encoder_t *enc)
{
    /* Open V4L2 M2M encoder device */
    enc->enc_fd = open("/dev/video11", O_RDWR | O_NONBLOCK);
    if (enc->enc_fd < 0)
    {
        enc->enc_fd = open("/dev/video31", O_RDWR | O_NONBLOCK);
        if (enc->enc_fd < 0)
        {
            perror("Failed to open encoder device");
            return -1;
        }
    }

    /* Query capabilities */
    struct v4l2_capability cap;
    if (xioctl(enc->enc_fd, VIDIOC_QUERYCAP, &cap) < 0)
    {
        perror("VIDIOC_QUERYCAP");
        return -1;
    }

    if (!(cap.capabilities & V4L2_CAP_VIDEO_M2M_MPLANE))
    {
        fprintf(stderr, "Device does not support M2M\n");
        return -1;
    }

    /* Set output format (raw YUV input to encoder) */
    struct v4l2_format fmt = {0};
    fmt.type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    fmt.fmt.pix_mp.width = enc->config.width;
    fmt.fmt.pix_mp.height = enc->config.height;
    fmt.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_YUV420;
    fmt.fmt.pix_mp.num_planes = 1;
    fmt.fmt.pix_mp.plane_fmt[0].bytesperline = enc->config.width;
    fmt.fmt.pix_mp.plane_fmt[0].sizeimage =
        enc->config.width * enc->config.height * 3 / 2;

    if (xioctl(enc->enc_fd, VIDIOC_S_FMT, &fmt) < 0)
    {
        perror("VIDIOC_S_FMT (output)");
        return -1;
    }

    /* Set capture format (H.264 output from encoder) */
    memset(&fmt, 0, sizeof(fmt));
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    fmt.fmt.pix_mp.width = enc->config.width;
    fmt.fmt.pix_mp.height = enc->config.height;
    fmt.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_H264;
    fmt.fmt.pix_mp.num_planes = 1;
    fmt.fmt.pix_mp.plane_fmt[0].sizeimage = MAX_ENCODED_SIZE;

    if (xioctl(enc->enc_fd, VIDIOC_S_FMT, &fmt) < 0)
    {
        perror("VIDIOC_S_FMT (capture)");
        return -1;
    }

    /* Set frame rate */
    struct v4l2_streamparm parm = {0};
    parm.type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    parm.parm.output.timeperframe.numerator = 1;
    parm.parm.output.timeperframe.denominator = enc->config.fps;
    xioctl(enc->enc_fd, VIDIOC_S_PARM, &parm);

    /* Set bitrate */
    struct v4l2_control ctrl = {0};
    ctrl.id = V4L2_CID_MPEG_VIDEO_BITRATE;
    ctrl.value = enc->config.bitrate_kbps * 1000;
    xioctl(enc->enc_fd, VIDIOC_S_CTRL, &ctrl);

    /* Set H.264 profile */
    ctrl.id = V4L2_CID_MPEG_VIDEO_H264_PROFILE;
    switch (enc->config.profile)
    {
    case FPV_PROFILE_BASELINE:
        ctrl.value = V4L2_MPEG_VIDEO_H264_PROFILE_BASELINE;
        break;
    case FPV_PROFILE_MAIN:
        ctrl.value = V4L2_MPEG_VIDEO_H264_PROFILE_MAIN;
        break;
    case FPV_PROFILE_HIGH:
        ctrl.value = V4L2_MPEG_VIDEO_H264_PROFILE_HIGH;
        break;
    }
    xioctl(enc->enc_fd, VIDIOC_S_CTRL, &ctrl);

    /* Set H.264 level */
    ctrl.id = V4L2_CID_MPEG_VIDEO_H264_LEVEL;
    switch (enc->config.level)
    {
    case FPV_LEVEL_31:
        ctrl.value = V4L2_MPEG_VIDEO_H264_LEVEL_3_1;
        break;
    case FPV_LEVEL_40:
        ctrl.value = V4L2_MPEG_VIDEO_H264_LEVEL_4_0;
        break;
    case FPV_LEVEL_41:
        ctrl.value = V4L2_MPEG_VIDEO_H264_LEVEL_4_1;
        break;
    case FPV_LEVEL_42:
        ctrl.value = V4L2_MPEG_VIDEO_H264_LEVEL_4_2;
        break;
    }
    xioctl(enc->enc_fd, VIDIOC_S_CTRL, &ctrl);

    /* Set IDR period */
    ctrl.id = V4L2_CID_MPEG_VIDEO_H264_I_PERIOD;
    ctrl.value = enc->config.idr_interval;
    xioctl(enc->enc_fd, VIDIOC_S_CTRL, &ctrl);

    /* Enable inline SPS/PPS with every IDR */
    ctrl.id = V4L2_CID_MPEG_VIDEO_REPEAT_SEQ_HEADER;
    ctrl.value = 1;
    xioctl(enc->enc_fd, VIDIOC_S_CTRL, &ctrl);

    /* Request output buffers (raw input) */
    struct v4l2_requestbuffers reqbuf = {0};
    reqbuf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    reqbuf.memory = V4L2_MEMORY_MMAP;
    reqbuf.count = NUM_OUTPUT_BUFFERS;

    if (xioctl(enc->enc_fd, VIDIOC_REQBUFS, &reqbuf) < 0)
    {
        perror("VIDIOC_REQBUFS (output)");
        return -1;
    }

    /* Map output buffers */
    for (int i = 0; i < NUM_OUTPUT_BUFFERS; i++)
    {
        struct v4l2_buffer buf = {0};
        struct v4l2_plane planes[1] = {0};
        buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;
        buf.length = 1;
        buf.m.planes = planes;

        if (xioctl(enc->enc_fd, VIDIOC_QUERYBUF, &buf) < 0)
        {
            perror("VIDIOC_QUERYBUF (output)");
            return -1;
        }

        enc->output_buffers[i].length = buf.m.planes[0].length;
        enc->output_buffers[i].start = mmap(NULL, buf.m.planes[0].length,
                                            PROT_READ | PROT_WRITE, MAP_SHARED,
                                            enc->enc_fd, buf.m.planes[0].m.mem_offset);

        if (enc->output_buffers[i].start == MAP_FAILED)
        {
            perror("mmap (output)");
            return -1;
        }
        enc->output_buffers[i].queued = false;
    }

    /* Request capture buffers (encoded output) */
    memset(&reqbuf, 0, sizeof(reqbuf));
    reqbuf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    reqbuf.memory = V4L2_MEMORY_MMAP;
    reqbuf.count = NUM_CAPTURE_BUFFERS;

    if (xioctl(enc->enc_fd, VIDIOC_REQBUFS, &reqbuf) < 0)
    {
        perror("VIDIOC_REQBUFS (capture)");
        return -1;
    }

    /* Map capture buffers and queue them */
    for (int i = 0; i < NUM_CAPTURE_BUFFERS; i++)
    {
        struct v4l2_buffer buf = {0};
        struct v4l2_plane planes[1] = {0};
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i;
        buf.length = 1;
        buf.m.planes = planes;

        if (xioctl(enc->enc_fd, VIDIOC_QUERYBUF, &buf) < 0)
        {
            perror("VIDIOC_QUERYBUF (capture)");
            return -1;
        }

        enc->capture_buffers[i].length = buf.m.planes[0].length;
        enc->capture_buffers[i].start = mmap(NULL, buf.m.planes[0].length,
                                             PROT_READ | PROT_WRITE, MAP_SHARED,
                                             enc->enc_fd, buf.m.planes[0].m.mem_offset);

        if (enc->capture_buffers[i].start == MAP_FAILED)
        {
            perror("mmap (capture)");
            return -1;
        }

        /* Queue the capture buffer */
        if (xioctl(enc->enc_fd, VIDIOC_QBUF, &buf) < 0)
        {
            perror("VIDIOC_QBUF (capture)");
            return -1;
        }
        enc->capture_buffers[i].queued = true;
    }

    /* NOTE: Streaming will be started when first frame is queued */
    enc->streaming_started = false;

    printf("Encoder: %dx%d @ %dfps, %d kbps, IDR every %d frames\n",
           enc->config.width, enc->config.height, enc->config.fps,
           enc->config.bitrate_kbps, enc->config.idr_interval);

    return 0;
}

static void cleanup_encoder(fpv_encoder_t *enc)
{
    if (enc->enc_fd >= 0)
    {
        int type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
        xioctl(enc->enc_fd, VIDIOC_STREAMOFF, &type);
        type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        xioctl(enc->enc_fd, VIDIOC_STREAMOFF, &type);

        for (int i = 0; i < NUM_OUTPUT_BUFFERS; i++)
        {
            if (enc->output_buffers[i].start &&
                enc->output_buffers[i].start != MAP_FAILED)
            {
                munmap(enc->output_buffers[i].start, enc->output_buffers[i].length);
            }
        }

        for (int i = 0; i < NUM_CAPTURE_BUFFERS; i++)
        {
            if (enc->capture_buffers[i].start &&
                enc->capture_buffers[i].start != MAP_FAILED)
            {
                munmap(enc->capture_buffers[i].start, enc->capture_buffers[i].length);
            }
        }

        close(enc->enc_fd);
        enc->enc_fd = -1;
    }
}

/* Capture thread - reads encoded frames and invokes callback */
static void *capture_thread(void *arg)
{
    fpv_encoder_t *enc = (fpv_encoder_t *)arg;

    while (enc->running)
    {
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(enc->enc_fd, &fds);

        struct timeval tv = {.tv_sec = 0, .tv_usec = 100000};
        int r = select(enc->enc_fd + 1, &fds, NULL, NULL, &tv);

        if (r < 0)
        {
            if (errno == EINTR)
                continue;
            perror("select");
            break;
        }
        if (r == 0)
            continue;

        /* Try to dequeue an output buffer (raw frame we sent) */
        struct v4l2_buffer outbuf = {0};
        struct v4l2_plane out_planes[1] = {0};
        outbuf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
        outbuf.memory = V4L2_MEMORY_MMAP;
        outbuf.length = 1;
        outbuf.m.planes = out_planes;

        if (xioctl(enc->enc_fd, VIDIOC_DQBUF, &outbuf) == 0)
        {
            pthread_mutex_lock(&enc->mutex);
            enc->output_buffers[outbuf.index].queued = false;
            pthread_cond_signal(&enc->cond);
            pthread_mutex_unlock(&enc->mutex);
        }

        /* Dequeue encoded buffer */
        struct v4l2_buffer buf = {0};
        struct v4l2_plane planes[1] = {0};
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.length = 1;
        buf.m.planes = planes;

        if (xioctl(enc->enc_fd, VIDIOC_DQBUF, &buf) < 0)
        {
            if (errno == EAGAIN)
                continue;
            perror("VIDIOC_DQBUF (capture)");
            break;
        }

        /* Build frame structure */
        uint8_t *data = enc->capture_buffers[buf.index].start;
        size_t len = buf.m.planes[0].bytesused;

        bool is_keyframe = check_idr(data, len);

        fpv_encoded_frame_t frame = {
            .data = data,
            .len = len,
            .frame_id = enc->frame_id++,
            .timestamp_us = get_time_us(),
            .is_keyframe = is_keyframe,
            .has_spspps = check_spspps(data, len)};

        /* Update stats */
        pthread_mutex_lock(&enc->mutex);
        enc->stats.frames_out++;
        enc->stats.bytes_out += len;
        if (is_keyframe)
            enc->stats.idr_count++;
        pthread_mutex_unlock(&enc->mutex);

        /* Invoke callback */
        if (enc->callback)
        {
            enc->callback(&frame, enc->user_data);
        }

        /* Requeue buffer */
        if (xioctl(enc->enc_fd, VIDIOC_QBUF, &buf) < 0)
        {
            perror("VIDIOC_QBUF (capture requeue)");
            break;
        }

        /* Handle IDR request */
        pthread_mutex_lock(&enc->mutex);
        if (enc->idr_requested)
        {
            struct v4l2_control ctrl = {
                .id = V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME,
                .value = 1};
            xioctl(enc->enc_fd, VIDIOC_S_CTRL, &ctrl);
            enc->idr_requested = false;
        }
        pthread_mutex_unlock(&enc->mutex);
    }

    return NULL;
}

fpv_encoder_t *fpv_encoder_create(const fpv_encoder_config_t *config,
                                  fpv_encoder_callback_t callback, void *user_data)
{
    fpv_encoder_t *enc = calloc(1, sizeof(fpv_encoder_t));
    if (!enc)
        return NULL;

    enc->enc_fd = -1;

    if (config)
    {
        enc->config = *config;
    }
    else
    {
        enc->config = (fpv_encoder_config_t)FPV_ENCODER_CONFIG_DEFAULT;
    }

    enc->callback = callback;
    enc->user_data = user_data;

    pthread_mutex_init(&enc->mutex, NULL);
    pthread_cond_init(&enc->cond, NULL);

    if (setup_encoder(enc) < 0)
    {
        fpv_encoder_destroy(enc);
        return NULL;
    }

    /* Start capture thread */
    enc->running = true;
    if (pthread_create(&enc->capture_thread, NULL, capture_thread, enc) != 0)
    {
        perror("pthread_create");
        fpv_encoder_destroy(enc);
        return NULL;
    }

    return enc;
}

void fpv_encoder_destroy(fpv_encoder_t *enc)
{
    if (!enc)
        return;

    enc->running = false;
    pthread_join(enc->capture_thread, NULL);

    cleanup_encoder(enc);
    pthread_mutex_destroy(&enc->mutex);
    pthread_cond_destroy(&enc->cond);
    free(enc);
}

int fpv_encoder_encode(fpv_encoder_t *enc, const fpv_camera_frame_t *frame)
{
    if (!enc || !frame || !enc->running)
        return -1;

    pthread_mutex_lock(&enc->mutex);

    /* Find a free output buffer */
    int buf_idx = -1;
    for (int i = 0; i < NUM_OUTPUT_BUFFERS; i++)
    {
        if (!enc->output_buffers[i].queued)
        {
            buf_idx = i;
            break;
        }
    }

    /* Wait for a free buffer if none available */
    while (buf_idx < 0 && enc->running)
    {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        ts.tv_nsec += 50000000; /* 50ms timeout */
        if (ts.tv_nsec >= 1000000000)
        {
            ts.tv_sec++;
            ts.tv_nsec -= 1000000000;
        }

        pthread_cond_timedwait(&enc->cond, &enc->mutex, &ts);

        for (int i = 0; i < NUM_OUTPUT_BUFFERS; i++)
        {
            if (!enc->output_buffers[i].queued)
            {
                buf_idx = i;
                break;
            }
        }
    }

    if (buf_idx < 0)
    {
        pthread_mutex_unlock(&enc->mutex);
        return -1;
    }

    enc->stats.frames_in++;
    pthread_mutex_unlock(&enc->mutex);

    /* Copy YUV data into output buffer */
    size_t y_size = frame->y_stride * frame->height;
    size_t uv_size = (frame->uv_stride * frame->height) / 2;
    size_t total_size = y_size + uv_size * 2;

    if (total_size > enc->output_buffers[buf_idx].length)
    {
        fprintf(stderr, "Frame too large for buffer\n");
        return -1;
    }

    uint8_t *dst = enc->output_buffers[buf_idx].start;
    memcpy(dst, frame->y_plane, y_size);
    memcpy(dst + y_size, frame->u_plane, uv_size);
    memcpy(dst + y_size + uv_size, frame->v_plane, uv_size);

    /* Queue buffer to encoder */
    struct v4l2_buffer buf = {0};
    struct v4l2_plane planes[1] = {0};
    buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    buf.memory = V4L2_MEMORY_MMAP;
    buf.index = buf_idx;
    buf.length = 1;
    buf.m.planes = planes;
    planes[0].bytesused = total_size;
    planes[0].length = enc->output_buffers[buf_idx].length;

    if (xioctl(enc->enc_fd, VIDIOC_QBUF, &buf) < 0)
    {
        perror("VIDIOC_QBUF (output)");
        return -1;
    }

    pthread_mutex_lock(&enc->mutex);
    enc->output_buffers[buf_idx].queued = true;

    /* Start streaming on first buffer queued */
    if (!enc->streaming_started)
    {
        int type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
        if (xioctl(enc->enc_fd, VIDIOC_STREAMON, &type) < 0)
        {
            perror("VIDIOC_STREAMON (output)");
            pthread_mutex_unlock(&enc->mutex);
            return -1;
        }

        type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        if (xioctl(enc->enc_fd, VIDIOC_STREAMON, &type) < 0)
        {
            perror("VIDIOC_STREAMON (capture)");
            pthread_mutex_unlock(&enc->mutex);
            return -1;
        }

        enc->streaming_started = true;
        printf("Encoder streaming started\n");
    }
    pthread_mutex_unlock(&enc->mutex);

    return 0;
}

void fpv_encoder_request_idr(fpv_encoder_t *enc)
{
    if (!enc)
        return;

    pthread_mutex_lock(&enc->mutex);
    enc->idr_requested = true;
    pthread_mutex_unlock(&enc->mutex);
}

fpv_encoder_stats_t fpv_encoder_get_stats(fpv_encoder_t *enc)
{
    if (!enc)
    {
        fpv_encoder_stats_t empty = {0};
        return empty;
    }

    pthread_mutex_lock(&enc->mutex);
    fpv_encoder_stats_t stats = enc->stats;
    pthread_mutex_unlock(&enc->mutex);

    return stats;
}
