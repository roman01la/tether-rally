/*
 * FPV Sender - Camera+Encoder using rpicam-vid
 *
 * Spawns rpicam-vid and reads H.264 NAL units from its stdout.
 * Uses direct read() with large buffer to minimize syscall overhead.
 */

#include "rpicam.h"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* Buffer size for reading H.264 data - large to reduce syscalls */
#define READ_BUFFER_SIZE (256 * 1024)

/* NAL unit accumulator buffer */
#define NAL_BUFFER_SIZE (512 * 1024)

/* NAL unit types */
#define NAL_TYPE_IDR 5
#define NAL_TYPE_SPS 7
#define NAL_TYPE_PPS 8

struct fpv_rpicam
{
    fpv_rpicam_config_t config;
    fpv_rpicam_callback_t callback;
    void *userdata;

    pid_t child_pid;
    int read_fd;

    pthread_t thread;
    volatile bool running;

    /* Frame counter */
    uint32_t frame_id;

    /* Stats */
    fpv_rpicam_stats_t stats;
    pthread_mutex_t stats_mutex;
};

static uint64_t get_time_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

/* Check NAL unit type */
static int get_nal_type(const uint8_t *data, size_t len)
{
    if (len < 5)
        return -1;

    /* Find start code */
    if (data[0] == 0 && data[1] == 0 && data[2] == 0 && data[3] == 1)
    {
        return data[4] & 0x1F;
    }
    if (data[0] == 0 && data[1] == 0 && data[2] == 1)
    {
        return data[3] & 0x1F;
    }
    return -1;
}

/* Check if buffer contains SPS/PPS */
static bool has_spspps(const uint8_t *data, size_t len)
{
    for (size_t i = 0; i + 4 < len; i++)
    {
        if (data[i] == 0 && data[i + 1] == 0)
        {
            int nal_type = -1;
            if (data[i + 2] == 0 && data[i + 3] == 1 && i + 4 < len)
            {
                nal_type = data[i + 4] & 0x1F;
            }
            else if (data[i + 2] == 1 && i + 3 < len)
            {
                nal_type = data[i + 3] & 0x1F;
            }
            if (nal_type == NAL_TYPE_SPS || nal_type == NAL_TYPE_PPS)
            {
                return true;
            }
        }
    }
    return false;
}

/* Check if buffer contains IDR */
static bool has_idr(const uint8_t *data, size_t len)
{
    for (size_t i = 0; i + 4 < len; i++)
    {
        if (data[i] == 0 && data[i + 1] == 0)
        {
            int nal_type = -1;
            if (data[i + 2] == 0 && data[i + 3] == 1 && i + 4 < len)
            {
                nal_type = data[i + 4] & 0x1F;
            }
            else if (data[i + 2] == 1 && i + 3 < len)
            {
                nal_type = data[i + 3] & 0x1F;
            }
            if (nal_type == NAL_TYPE_IDR)
            {
                return true;
            }
        }
    }
    return false;
}

/* Find next NAL start code position */
static ssize_t find_start_code(const uint8_t *data, size_t len, size_t start)
{
    for (size_t i = start; i + 3 < len; i++)
    {
        if (data[i] == 0 && data[i + 1] == 0)
        {
            if (data[i + 2] == 1)
            {
                return i;
            }
            if (data[i + 2] == 0 && i + 3 < len && data[i + 3] == 1)
            {
                return i;
            }
        }
    }
    return -1;
}

/* Reader thread */
static void *reader_thread(void *arg)
{
    fpv_rpicam_t *cam = arg;

    uint8_t *read_buf = malloc(READ_BUFFER_SIZE);
    uint8_t *nal_buf = malloc(NAL_BUFFER_SIZE);
    size_t nal_len = 0;

    if (!read_buf || !nal_buf)
    {
        free(read_buf);
        free(nal_buf);
        return NULL;
    }

    printf("[RPICAM] Reader thread started\n");

    while (cam->running)
    {
        /* Read chunk from rpicam-vid */
        ssize_t n = read(cam->read_fd, read_buf, READ_BUFFER_SIZE);

        if (n < 0)
        {
            if (errno == EINTR)
                continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK)
            {
                usleep(1000); /* 1ms */
                continue;
            }
            perror("read from rpicam-vid");
            pthread_mutex_lock(&cam->stats_mutex);
            cam->stats.read_errors++;
            pthread_mutex_unlock(&cam->stats_mutex);
            break;
        }

        if (n == 0)
        {
            /* EOF - rpicam-vid exited */
            printf("[RPICAM] rpicam-vid closed pipe\n");
            break;
        }

        /* Append to NAL buffer */
        if (nal_len + n > NAL_BUFFER_SIZE)
        {
            /* Buffer overflow - reset */
            fprintf(stderr, "[RPICAM] NAL buffer overflow, resetting\n");
            nal_len = 0;
        }

        memcpy(nal_buf + nal_len, read_buf, n);
        nal_len += n;

        /* Process complete access units (frames) */
        /* Look for start codes and emit complete NAL units */
        size_t pos = 0;
        ssize_t start = find_start_code(nal_buf, nal_len, 0);

        while (start >= 0 && cam->running)
        {
            /* Find next start code */
            ssize_t next = find_start_code(nal_buf, nal_len, start + 3);

            if (next < 0)
            {
                /* No complete NAL yet - keep accumulating */
                break;
            }

            /* We have a complete NAL from start to next */
            size_t nal_unit_len = next - start;

            /* Get NAL type */
            int nal_type = get_nal_type(nal_buf + start, nal_unit_len);

            /* For IDR or non-VCL NALs followed by VCL, emit as frame */
            /* Simplified: emit each NAL as a "frame" - the sender will
             * handle fragmentation */

            /* Actually, we want to group SPS+PPS+IDR or single P-frames */
            /* For now, emit when we see a VCL NAL (1-5) followed by another
             * start */

            /* Simple approach: look for frame boundaries */
            /* A frame starts with SPS/PPS/IDR or just a P-slice */
            /* Emit accumulated NALs when we see a new frame starting */

            if (nal_type >= 1 && nal_type <= 5)
            {
                /* VCL NAL - this is video data */
                /* Check if next NAL is also VCL or non-VCL */
                int next_type = get_nal_type(nal_buf + next, nal_len - next);

                /* If next is SPS/PPS or another slice, emit current */
                if (next_type == NAL_TYPE_SPS || next_type == NAL_TYPE_PPS ||
                    (next_type >= 1 && next_type <= 5))
                {
                    /* Emit everything from pos to next as one frame */
                    size_t frame_len = next - pos;

                    fpv_rpicam_frame_t frame = {
                        .data = nal_buf + pos,
                        .len = frame_len,
                        .frame_id = cam->frame_id++,
                        .timestamp_us = get_time_us(),
                        .is_keyframe = has_idr(nal_buf + pos, frame_len),
                        .has_spspps = has_spspps(nal_buf + pos, frame_len)};

                    pthread_mutex_lock(&cam->stats_mutex);
                    cam->stats.frames_read++;
                    cam->stats.bytes_read += frame_len;
                    if (frame.is_keyframe)
                        cam->stats.keyframes++;
                    pthread_mutex_unlock(&cam->stats_mutex);

                    if (cam->callback)
                    {
                        cam->callback(&frame, cam->userdata);
                    }

                    pos = next;
                }
            }

            start = next;
        }

        /* Move remaining data to front of buffer */
        if (pos > 0 && pos < nal_len)
        {
            memmove(nal_buf, nal_buf + pos, nal_len - pos);
            nal_len -= pos;
        }
        else if (pos >= nal_len)
        {
            nal_len = 0;
        }
    }

    free(read_buf);
    free(nal_buf);

    printf("[RPICAM] Reader thread exiting\n");
    return NULL;
}

fpv_rpicam_t *fpv_rpicam_create(const fpv_rpicam_config_t *config,
                                fpv_rpicam_callback_t callback, void *userdata)
{
    fpv_rpicam_t *cam = calloc(1, sizeof(fpv_rpicam_t));
    if (!cam)
        return NULL;

    if (config)
    {
        cam->config = *config;
    }
    else
    {
        cam->config = (fpv_rpicam_config_t)FPV_RPICAM_CONFIG_DEFAULT;
    }

    cam->callback = callback;
    cam->userdata = userdata;
    cam->child_pid = -1;
    cam->read_fd = -1;
    pthread_mutex_init(&cam->stats_mutex, NULL);

    /* Create pipe for reading H.264 output */
    int pipefd[2];
    if (pipe(pipefd) < 0)
    {
        perror("pipe");
        free(cam);
        return NULL;
    }

    /* Set read end to non-blocking */
    int flags = fcntl(pipefd[0], F_GETFL, 0);
    fcntl(pipefd[0], F_SETFL, flags | O_NONBLOCK);

    /* Increase pipe buffer size if possible */
#ifdef F_SETPIPE_SZ
    fcntl(pipefd[0], F_SETPIPE_SZ, 1024 * 1024); /* 1MB pipe buffer */
#endif

    /* Fork and exec rpicam-vid */
    pid_t pid = fork();
    if (pid < 0)
    {
        perror("fork");
        close(pipefd[0]);
        close(pipefd[1]);
        free(cam);
        return NULL;
    }

    if (pid == 0)
    {
        /* Child process */
        close(pipefd[0]); /* Close read end */

        /* Redirect stdout to pipe */
        dup2(pipefd[1], STDOUT_FILENO);
        close(pipefd[1]);

        /* Build rpicam-vid command */
        char width_str[16], height_str[16], fps_str[16];
        char bitrate_str[16], idr_str[16];

        snprintf(width_str, sizeof(width_str), "%d", cam->config.width);
        snprintf(height_str, sizeof(height_str), "%d", cam->config.height);
        snprintf(fps_str, sizeof(fps_str), "%d", cam->config.fps);
        snprintf(bitrate_str, sizeof(bitrate_str), "%d",
                 cam->config.bitrate_kbps * 1000);
        snprintf(idr_str, sizeof(idr_str), "%d", cam->config.idr_interval);

        /* Execute rpicam-vid */
        execlp("rpicam-vid", "rpicam-vid", "-t", "0", /* Run forever */
               "--width", width_str, "--height", height_str, "--framerate",
               fps_str, "--bitrate", bitrate_str, "--intra", idr_str,
               "--profile", "baseline", "--level", "4.2", "--inline",
               "--flush", /* Flush after each frame */
               "-n",      /* No preview */
               "-o", "-", /* Output to stdout */
               NULL);

        perror("execlp rpicam-vid");
        _exit(1);
    }

    /* Parent process */
    close(pipefd[1]); /* Close write end */
    cam->read_fd = pipefd[0];
    cam->child_pid = pid;

    printf("[RPICAM] Started rpicam-vid (pid %d): %dx%d @ %dfps, %d kbps\n", pid,
           cam->config.width, cam->config.height, cam->config.fps,
           cam->config.bitrate_kbps);

    /* Start reader thread */
    cam->running = true;
    if (pthread_create(&cam->thread, NULL, reader_thread, cam) != 0)
    {
        perror("pthread_create");
        kill(cam->child_pid, SIGTERM);
        waitpid(cam->child_pid, NULL, 0);
        close(cam->read_fd);
        free(cam);
        return NULL;
    }

    return cam;
}

void fpv_rpicam_destroy(fpv_rpicam_t *cam)
{
    if (!cam)
        return;

    cam->running = false;

    /* Kill rpicam-vid */
    if (cam->child_pid > 0)
    {
        kill(cam->child_pid, SIGTERM);
        waitpid(cam->child_pid, NULL, 0);
    }

    /* Close pipe to unblock reader thread */
    if (cam->read_fd >= 0)
    {
        close(cam->read_fd);
    }

    pthread_join(cam->thread, NULL);
    pthread_mutex_destroy(&cam->stats_mutex);

    free(cam);
}

void fpv_rpicam_request_idr(fpv_rpicam_t *cam)
{
    if (!cam || cam->child_pid <= 0)
        return;

    /* Send SIGUSR1 to rpicam-vid to request IDR */
    kill(cam->child_pid, SIGUSR1);
}

fpv_rpicam_stats_t fpv_rpicam_get_stats(fpv_rpicam_t *cam)
{
    if (!cam)
    {
        fpv_rpicam_stats_t empty = {0};
        return empty;
    }

    pthread_mutex_lock(&cam->stats_mutex);
    fpv_rpicam_stats_t stats = cam->stats;
    pthread_mutex_unlock(&cam->stats_mutex);

    return stats;
}
