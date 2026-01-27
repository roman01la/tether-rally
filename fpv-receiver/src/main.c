/*
 * FPV Receiver - Main Application
 *
 * Ultra-low latency FPV video receiver with GLFW/OpenGL rendering
 * and VideoToolbox decoding (macOS).
 *
 * Architecture:
 * - Network thread: receives packets, assembles frames, decodes
 * - Main thread: renders at 60Hz from latest decoded frame
 *
 * Usage: fpv-receiver [options]
 *   --local               Local network mode (no STUN/signaling)
 *   --sender <ip:port>    Sender address (required for --local)
 *   --port <port>         Local UDP port (default: random)
 *   --session <url>       Signaling server session URL
 *   --fullscreen          Start in fullscreen mode
 *   -v, --verbose         Verbose output
 */

#define GL_SILENCE_DEPRECATION

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <getopt.h>
#include <stdbool.h>
#include <sys/time.h>
#include <unistd.h>
#include <mach/mach_time.h>
#include <arpa/inet.h>
#include <pthread.h>

#include <GLFW/glfw3.h>
#include <CoreVideo/CoreVideo.h>

#include "protocol.h"
#include "receiver.h"
#include "stun.h"
#include "assembler.h"
#include "decoder.h"
#include "renderer.h"

/* IDR request reasons (from protocol) */
#define FPV_IDR_REASON_START 0x01
#define FPV_IDR_REASON_ERROR 0x02
#define FPV_IDR_REASON_TIMEOUT 0x03

/* Application state machine */
typedef enum
{
    STATE_INIT,
    STATE_STUN_GATHER,
    STATE_WAIT_SENDER,
    STATE_PUNCHING,
    STATE_STREAMING,
    STATE_ERROR
} app_state_t;

/* Thread-safe frame slot for passing decoded frames to render thread */
typedef struct
{
    pthread_mutex_t mutex;
    CVPixelBufferRef pixbuf; /* Latest decoded frame (retained) */
    int width;
    int height;
    uint32_t frame_id;
    bool has_new_frame; /* Flag for render thread */
    /* Timing telemetry */
    uint64_t first_packet_time_us;
    uint64_t assembly_complete_us;
    uint64_t decode_complete_us;
} frame_slot_t;

/* Application context */
typedef struct
{
    /* Configuration */
    bool local_mode;
    bool fullscreen;
    bool verbose;
    int local_port;
    char sender_addr_str[64];
    char session_url[256];

    /* Network */
    fpv_receiver_t *receiver;
    struct sockaddr_in sender_addr;
    bool sender_known;
    uint32_t session_id;
    uint32_t keepalive_seq;

    /* STUN */
    fpv_stun_result_t stun_result;

    /* Video pipeline */
    fpv_assembler_t assembler; /* Stack allocated */
    fpv_decoder_t *decoder;
    fpv_renderer_t *renderer;

    /* Thread-safe frame passing */
    frame_slot_t frame_slot;
    pthread_t network_thread;
    bool network_thread_running;

    /* State */
    app_state_t state;
    uint64_t state_enter_time_us;
    uint64_t last_keepalive_time_us;
    uint64_t last_video_time_us;
    uint64_t last_stats_time_us;
    bool got_first_frame;
    bool idr_requested;

    /* GLFW */
    GLFWwindow *window;
    int window_width;
    int window_height;
} app_ctx_t;

/* fpv_get_time_us is declared in assembler.h */

static volatile bool g_running = true;

static void signal_handler(int sig)
{
    (void)sig;
    g_running = false;
}

static void error_callback(int error, const char *description)
{
    fprintf(stderr, "GLFW error %d: %s\n", error, description);
}

static void key_callback(GLFWwindow *window, int key, int scancode, int action, int mods)
{
    (void)scancode;
    (void)mods;
    if (action == GLFW_PRESS)
    {
        if (key == GLFW_KEY_ESCAPE || key == GLFW_KEY_Q)
        {
            glfwSetWindowShouldClose(window, GLFW_TRUE);
        }
        else if (key == GLFW_KEY_F)
        {
            /* Toggle fullscreen */
            GLFWmonitor *monitor = glfwGetWindowMonitor(window);
            if (monitor)
            {
                /* Exit fullscreen */
                glfwSetWindowMonitor(window, NULL, 100, 100, 1280, 720, 0);
            }
            else
            {
                /* Enter fullscreen */
                monitor = glfwGetPrimaryMonitor();
                const GLFWvidmode *mode = glfwGetVideoMode(monitor);
                glfwSetWindowMonitor(window, monitor, 0, 0, mode->width, mode->height, mode->refreshRate);
            }
        }
    }
}

static void framebuffer_size_callback(GLFWwindow *window, int width, int height)
{
    app_ctx_t *ctx = glfwGetWindowUserPointer(window);
    if (ctx)
    {
        ctx->window_width = width;
        ctx->window_height = height;
        /* Viewport will be set at draw time */
    }
}

static int parse_addr(const char *str, struct sockaddr_in *addr)
{
    char ip[64];
    int port;

    if (sscanf(str, "%63[^:]:%d", ip, &port) != 2)
    {
        return -1;
    }

    addr->sin_family = AF_INET;
    addr->sin_port = htons(port);
    if (inet_pton(AF_INET, ip, &addr->sin_addr) != 1)
    {
        return -1;
    }

    return 0;
}

static void print_usage(const char *prog)
{
    printf("Usage: %s [options]\n", prog);
    printf("Options:\n");
    printf("  --local               Local network mode (no STUN/signaling)\n");
    printf("  --sender <ip:port>    Sender address (required for --local)\n");
    printf("  --port <port>         Local UDP port (default: random)\n");
    printf("  --session <url>       Signaling server session URL\n");
    printf("  --fullscreen          Start in fullscreen mode\n");
    printf("  -v, --verbose         Verbose output\n");
    printf("  -h, --help            Show this help\n");
    printf("\nControls:\n");
    printf("  F         Toggle fullscreen\n");
    printf("  Q/ESC     Quit\n");
}

static void change_state(app_ctx_t *ctx, app_state_t new_state)
{
    const char *state_names[] = {
        "INIT", "STUN_GATHER", "WAIT_SENDER", "PUNCHING", "STREAMING", "ERROR"};
    if (ctx->verbose)
    {
        printf("[STATE] %s -> %s\n", state_names[ctx->state], state_names[new_state]);
    }
    ctx->state = new_state;
    ctx->state_enter_time_us = fpv_get_time_us();
}

/* Post decoded frame to thread-safe slot for render thread */
static void post_frame_to_slot(app_ctx_t *ctx, fpv_decoded_frame_t *frame)
{
    frame_slot_t *slot = &ctx->frame_slot;

    pthread_mutex_lock(&slot->mutex);

    /* Release old frame */
    if (slot->pixbuf)
    {
        CVPixelBufferRelease(slot->pixbuf);
    }

    /* Take ownership of new frame */
    slot->pixbuf = (CVPixelBufferRef)frame->native_handle;
    CVPixelBufferRetain(slot->pixbuf);
    slot->width = frame->width;
    slot->height = frame->height;
    slot->frame_id = frame->frame_id;
    slot->has_new_frame = true;
    /* Timing telemetry */
    slot->first_packet_time_us = frame->first_packet_time_us;
    slot->assembly_complete_us = frame->assembly_complete_us;
    slot->decode_complete_us = frame->decode_complete_us;

    pthread_mutex_unlock(&slot->mutex);
}

/* Process a single video fragment (called from network thread) */
static void process_video_fragment(app_ctx_t *ctx, const uint8_t *buf, size_t len)
{
    fpv_video_fragment_t frag;
    if (fpv_parse_video_fragment(buf, len, &frag) != 0)
    {
        return;
    }

    /* Update video activity time on ANY valid packet, not just successful decode.
     * This prevents false IDR requests during FPS ramp-up or decode delays. */
    ctx->last_video_time_us = fpv_get_time_us();

    /* Feed to assembler */
    fpv_assembler_add_fragment(&ctx->assembler, &frag);

    /* Check timeouts */
    fpv_assembler_check_timeouts(&ctx->assembler);

    /* Request IDR only on actual packet loss (timeout), not supersede */
    if (fpv_assembler_needs_idr(&ctx->assembler) && ctx->sender_known && !ctx->idr_requested)
    {
        /* Rate limit IDR requests - at most every 1s */
        static uint64_t last_idr_request_time = 0;
        uint64_t now = fpv_get_time_us();
        if (now - last_idr_request_time > 1000000)
        {
            printf("[VIDEO] Packet loss detected - requesting IDR\n");
            fpv_receiver_send_idr_request(ctx->receiver, ctx->session_id,
                                          0, FPV_IDR_REASON_ERROR, &ctx->sender_addr);
            last_idr_request_time = now;
            ctx->idr_requested = true;
        }
        /* Always clear the flag to prevent buildup */
        fpv_assembler_clear_idr_request(&ctx->assembler);
    }

    /* Decode ALL completed frames to maintain H.264 reference state */
    fpv_access_unit_t *au;
    while ((au = fpv_assembler_get_au(&ctx->assembler)) != NULL)
    {
        /* Decode frame - must decode every frame for H.264 prediction */
        fpv_decoded_frame_t frame;
        int decode_result = fpv_decoder_decode(ctx->decoder, au->data, au->len,
                                               au->frame_id, au->ts_ms, au->is_keyframe, &frame);

        if (decode_result == 0)
        {
            /* Copy timing telemetry from AU to frame */
            frame.first_packet_time_us = au->first_packet_time_us;
            frame.assembly_complete_us = au->assembly_complete_us;
            frame.decode_complete_us = fpv_get_time_us();

            if (!ctx->got_first_frame)
            {
                ctx->got_first_frame = true;
                printf("[VIDEO] First frame decoded: %dx%d\n", frame.width, frame.height);
            }

            /* Clear IDR request flag when we successfully decode a keyframe */
            if (au->is_keyframe)
            {
                ctx->idr_requested = false;
                fpv_assembler_clear_idr_request(&ctx->assembler);
            }

            /* Post to frame slot for render thread (non-blocking) */
            post_frame_to_slot(ctx, &frame);

            fpv_decoder_release_frame(&frame);
        }

        fpv_access_unit_free(au);
    }
}

static void process_keepalive(app_ctx_t *ctx, const uint8_t *buf, size_t len,
                              const struct sockaddr_in *from)
{
    fpv_keepalive_t ka;
    if (fpv_parse_keepalive(buf, len, &ka) != 0)
    {
        return;
    }

    /* Remember sender address */
    if (!ctx->sender_known)
    {
        ctx->sender_addr = *from;
        ctx->sender_known = true;
        ctx->session_id = ka.session_id;
        if (ctx->verbose)
        {
            printf("[NET] Sender discovered: %s:%d\n",
                   inet_ntoa(from->sin_addr), ntohs(from->sin_port));
        }
    }

    /* Echo keepalive */
    fpv_receiver_send_keepalive(ctx->receiver, ctx->session_id,
                                ctx->keepalive_seq++, ka.ts_ms, &ctx->sender_addr);
}

static void process_probe(app_ctx_t *ctx, const uint8_t *buf, size_t len,
                          const struct sockaddr_in *from)
{
    fpv_probe_t probe;
    if (fpv_parse_probe(buf, len, &probe) != 0)
    {
        return;
    }

    if (ctx->verbose)
    {
        printf("[PUNCH] Probe from %s:%d, nonce=%llx\n",
               inet_ntoa(from->sin_addr), ntohs(from->sin_port), probe.nonce);
    }

    /* Remember sender and transition to streaming */
    if (!ctx->sender_known || ctx->state == STATE_PUNCHING)
    {
        ctx->sender_addr = *from;
        ctx->sender_known = true;
        ctx->session_id = probe.session_id;

        /* Echo probe back */
        fpv_receiver_send_probe(ctx->receiver, probe.session_id,
                                probe.probe_seq, probe.nonce, from);

        if (ctx->state == STATE_PUNCHING)
        {
            change_state(ctx, STATE_STREAMING);
        }
    }
}

/* Network thread: receives packets and decodes frames continuously */
static void *network_thread_func(void *arg)
{
    app_ctx_t *ctx = (app_ctx_t *)arg;
    uint8_t buf[2048];
    struct sockaddr_in from;

    printf("[THREAD] Network thread started\n");

    while (g_running && ctx->network_thread_running)
    {
        /* Receive with short timeout to allow clean shutdown */
        int n = fpv_receiver_recv(ctx->receiver, buf, sizeof(buf), &from);
        if (n <= 0)
        {
            /* No data, brief sleep to avoid busy loop */
            usleep(100);
            continue;
        }

        if ((size_t)n < FPV_COMMON_HEADER_SIZE)
            continue;

        uint8_t msg_type = buf[0];
        switch (msg_type)
        {
        case FPV_MSG_VIDEO_FRAGMENT:
            process_video_fragment(ctx, buf, n);
            break;
        case FPV_MSG_KEEPALIVE:
            process_keepalive(ctx, buf, n, &from);
            break;
        case FPV_MSG_PROBE:
            process_probe(ctx, buf, n, &from);
            break;
        default:
            break;
        }
    }

    printf("[THREAD] Network thread exiting\n");
    return NULL;
}

static void handle_packets(app_ctx_t *ctx)
{
    uint8_t buf[2048];
    struct sockaddr_in from;

    for (int i = 0; i < 100; i++)
    { /* Process up to 100 packets per frame */
        int n = fpv_receiver_recv(ctx->receiver, buf, sizeof(buf), &from);
        if (n <= 0)
            break;

        if ((size_t)n < FPV_COMMON_HEADER_SIZE)
            continue;

        uint8_t msg_type = buf[0];
        switch (msg_type)
        {
        case FPV_MSG_VIDEO_FRAGMENT:
            process_video_fragment(ctx, buf, n);
            break;
        case FPV_MSG_KEEPALIVE:
            process_keepalive(ctx, buf, n, &from);
            break;
        case FPV_MSG_PROBE:
            process_probe(ctx, buf, n, &from);
            break;
        default:
            break;
        }
    }
}

static void send_periodic_messages(app_ctx_t *ctx)
{
    uint64_t now = fpv_get_time_us();

    /* Send keepalive every 1s when streaming */
    if (ctx->sender_known && (now - ctx->last_keepalive_time_us > 1000000))
    {
        fpv_receiver_send_keepalive(ctx->receiver, ctx->session_id,
                                    ctx->keepalive_seq++, 0, &ctx->sender_addr);
        ctx->last_keepalive_time_us = now;
    }

    /* Request IDR if we haven't received video in 1s (after initial connection).
     * Using 1s instead of 400ms to tolerate FPS ramp-up (32→60fps) and brief hiccups. */
    if (ctx->got_first_frame && ctx->sender_known &&
        (now - ctx->last_video_time_us > 1000000) && !ctx->idr_requested)
    {
        if (ctx->verbose)
        {
            printf("[VIDEO] Requesting IDR (video timeout)\n");
        }
        fpv_receiver_send_idr_request(ctx->receiver, ctx->session_id,
                                      0, FPV_IDR_REASON_TIMEOUT, &ctx->sender_addr);
        ctx->idr_requested = true;
    }

    /* Reset IDR flag when we get video again */
    if (ctx->idr_requested && (now - ctx->last_video_time_us < 100000))
    {
        ctx->idr_requested = false;
    }
}

static void print_stats(app_ctx_t *ctx)
{
    uint64_t now = fpv_get_time_us();
    if (now - ctx->last_stats_time_us < 2000000)
        return; /* Every 2s */
    ctx->last_stats_time_us = now;

    fpv_receiver_stats_t rx_stats = fpv_receiver_get_stats(ctx->receiver);
    fpv_assembler_stats_t asm_stats = fpv_assembler_get_stats(&ctx->assembler);
    fpv_renderer_stats_t rnd_stats = fpv_renderer_get_stats(ctx->renderer);

    uint64_t frames_dropped = asm_stats.frames_dropped_timeout +
                              asm_stats.frames_dropped_superseded +
                              asm_stats.frames_dropped_overflow;

    /* Print packet/frame stats */
    printf("[STATS] RX: %llu pkts | ASM: %llu complete, %llu timeout, %llu superseded, %llu dup | RND: %llu\n",
           rx_stats.packets_received,
           asm_stats.frames_completed,
           asm_stats.frames_dropped_timeout,
           asm_stats.frames_dropped_superseded,
           asm_stats.duplicate_fragments,
           rnd_stats.frames_rendered);

    /* Print timing telemetry if available */
    if (rnd_stats.avg_total_us > 0)
    {
        printf("[TIMING] asm=%.1fms dec=%.1fms tex=%.1fms | TOTAL=%.1fms (pkt→texture)\n",
               rnd_stats.avg_assembly_us / 1000.0,
               rnd_stats.avg_decode_us / 1000.0,
               rnd_stats.avg_upload_us / 1000.0,
               rnd_stats.avg_total_us / 1000.0);
    }

    /* Print jitter telemetry if available */
    if (rnd_stats.avg_interval_us > 0)
    {
        double actual_fps = 1000000.0 / rnd_stats.avg_interval_us;
        double target_interval_ms = 1000.0 / rnd_stats.target_fps;
        printf("[JITTER] interval=%.1fms (%.1ffps) jitter=%.1fms (target=%.1fms)\n",
               rnd_stats.avg_interval_us / 1000.0,
               actual_fps,
               rnd_stats.avg_jitter_us / 1000.0,
               target_interval_ms);
    }
}

int main(int argc, char *argv[])
{
    app_ctx_t ctx = {0};
    ctx.window_width = 1280;
    ctx.window_height = 720;
    ctx.state = STATE_INIT;

    /* Parse arguments */
    static struct option long_options[] = {
        {"local", no_argument, 0, 'l'},
        {"sender", required_argument, 0, 's'},
        {"port", required_argument, 0, 'p'},
        {"session", required_argument, 0, 'S'},
        {"fullscreen", no_argument, 0, 'f'},
        {"verbose", no_argument, 0, 'v'},
        {"help", no_argument, 0, 'h'},
        {0, 0, 0, 0}};

    int opt;
    while ((opt = getopt_long(argc, argv, "ls:p:S:fvh", long_options, NULL)) != -1)
    {
        switch (opt)
        {
        case 'l':
            ctx.local_mode = true;
            break;
        case 's':
            strncpy(ctx.sender_addr_str, optarg, sizeof(ctx.sender_addr_str) - 1);
            break;
        case 'p':
            ctx.local_port = atoi(optarg);
            break;
        case 'S':
            strncpy(ctx.session_url, optarg, sizeof(ctx.session_url) - 1);
            break;
        case 'f':
            ctx.fullscreen = true;
            break;
        case 'v':
            ctx.verbose = true;
            break;
        case 'h':
            print_usage(argv[0]);
            return 0;
        default:
            print_usage(argv[0]);
            return 1;
        }
    }

    /* Validate arguments */
    if (ctx.local_mode && ctx.sender_addr_str[0] == '\0')
    {
        fprintf(stderr, "Error: --sender required with --local\n");
        return 1;
    }

    if (ctx.local_mode && parse_addr(ctx.sender_addr_str, &ctx.sender_addr) != 0)
    {
        fprintf(stderr, "Error: Invalid sender address: %s\n", ctx.sender_addr_str);
        return 1;
    }
    if (ctx.local_mode)
    {
        ctx.sender_known = true;
    }

    /* Signal handling */
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    /* Initialize GLFW */
    glfwSetErrorCallback(error_callback);
    if (!glfwInit())
    {
        fprintf(stderr, "Error: Failed to initialize GLFW\n");
        return 1;
    }

    /* Create window - use legacy OpenGL 2.1 for fixed-function pipeline support */
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 2);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 1);
    /* Don't request core profile - we need legacy fixed-function support */

    GLFWmonitor *monitor = ctx.fullscreen ? glfwGetPrimaryMonitor() : NULL;
    if (monitor)
    {
        const GLFWvidmode *mode = glfwGetVideoMode(monitor);
        ctx.window_width = mode->width;
        ctx.window_height = mode->height;
    }

    ctx.window = glfwCreateWindow(ctx.window_width, ctx.window_height,
                                  "FPV Receiver", monitor, NULL);
    if (!ctx.window)
    {
        fprintf(stderr, "Error: Failed to create GLFW window\n");
        glfwTerminate();
        return 1;
    }

    glfwSetWindowUserPointer(ctx.window, &ctx);
    glfwSetKeyCallback(ctx.window, key_callback);
    glfwSetFramebufferSizeCallback(ctx.window, framebuffer_size_callback);
    glfwMakeContextCurrent(ctx.window);
    glfwSwapInterval(0); /* Disable VSync for minimum latency (FPV mode) */

    /* Get actual framebuffer size (may differ on Retina) */
    glfwGetFramebufferSize(ctx.window, &ctx.window_width, &ctx.window_height);

    /* Create receiver */
    fpv_receiver_config_t rx_config = {
        .local_port = ctx.local_port,
        .recv_buf_size = 64 * 1024};
    ctx.receiver = fpv_receiver_create(&rx_config);
    if (!ctx.receiver)
    {
        fprintf(stderr, "Error: Failed to create receiver\n");
        glfwDestroyWindow(ctx.window);
        glfwTerminate();
        return 1;
    }

    {
        struct sockaddr_in local;
        fpv_receiver_get_local_addr(ctx.receiver, &local);
        printf("[NET] Listening on port %d\n", ntohs(local.sin_port));
    }

    /* Initialize assembler */
    if (fpv_assembler_init(&ctx.assembler) != 0)
    {
        fprintf(stderr, "Error: Failed to initialize assembler\n");
        fpv_receiver_destroy(ctx.receiver);
        glfwDestroyWindow(ctx.window);
        glfwTerminate();
        return 1;
    }

    /* Create decoder */
    ctx.decoder = fpv_decoder_create();
    if (!ctx.decoder)
    {
        fprintf(stderr, "Error: Failed to create decoder\n");
        fpv_assembler_destroy(&ctx.assembler);
        fpv_receiver_destroy(ctx.receiver);
        glfwDestroyWindow(ctx.window);
        glfwTerminate();
        return 1;
    }

    /* Create renderer */
    ctx.renderer = fpv_renderer_create();
    if (!ctx.renderer)
    {
        fprintf(stderr, "Error: Failed to create renderer\n");
        fpv_decoder_destroy(ctx.decoder);
        fpv_assembler_destroy(&ctx.assembler);
        fpv_receiver_destroy(ctx.receiver);
        glfwDestroyWindow(ctx.window);
        glfwTerminate();
        return 1;
    }

    /* Initialize frame slot for thread-safe frame passing */
    pthread_mutex_init(&ctx.frame_slot.mutex, NULL);
    ctx.frame_slot.pixbuf = NULL;
    ctx.frame_slot.has_new_frame = false;

    /* Initial state */
    if (ctx.local_mode)
    {
        change_state(&ctx, STATE_STREAMING);
        printf("[MODE] Local mode - sending to %s\n", ctx.sender_addr_str);

        /* Request initial IDR */
        fpv_receiver_send_idr_request(ctx.receiver, 0, 0, FPV_IDR_REASON_START, &ctx.sender_addr);
    }
    else
    {
        change_state(&ctx, STATE_STUN_GATHER);
    }

    ctx.last_stats_time_us = fpv_get_time_us();
    ctx.last_video_time_us = fpv_get_time_us();

    /* Start network thread for packet reception and decoding */
    ctx.network_thread_running = true;
    if (pthread_create(&ctx.network_thread, NULL, network_thread_func, &ctx) != 0)
    {
        fprintf(stderr, "Error: Failed to create network thread\n");
        fpv_renderer_destroy(ctx.renderer);
        fpv_decoder_destroy(ctx.decoder);
        fpv_assembler_destroy(&ctx.assembler);
        fpv_receiver_destroy(ctx.receiver);
        glfwDestroyWindow(ctx.window);
        glfwTerminate();
        return 1;
    }

    /* Main loop - render thread only */
    while (g_running && !glfwWindowShouldClose(ctx.window))
    {
        uint64_t now = fpv_get_time_us();

        /* Handle non-streaming states (STUN, etc) in main thread */
        switch (ctx.state)
        {
        case STATE_STUN_GATHER:
        {
            int fd = fpv_receiver_get_fd(ctx.receiver);
            if (fpv_stun_discover(fd, &ctx.stun_result) == 0)
            {
                printf("[STUN] Public address: %s:%d (via %s)\n",
                       inet_ntoa(ctx.stun_result.public_addr.sin_addr),
                       ntohs(ctx.stun_result.public_addr.sin_port),
                       ctx.stun_result.server);

                if (ctx.session_url[0])
                {
                    /* TODO: Register with signaling server */
                    change_state(&ctx, STATE_WAIT_SENDER);
                }
                else
                {
                    fprintf(stderr, "Error: No session URL provided\n");
                    change_state(&ctx, STATE_ERROR);
                }
            }
            else
            {
                if (now - ctx.state_enter_time_us > 10000000)
                {
                    fprintf(stderr, "Error: STUN discovery timeout\n");
                    change_state(&ctx, STATE_ERROR);
                }
            }
            break;
        }

        case STATE_WAIT_SENDER:
            /* TODO: Poll signaling server for sender */
            if (now - ctx.state_enter_time_us > 60000000)
            {
                fprintf(stderr, "Error: Waiting for sender timeout\n");
                change_state(&ctx, STATE_ERROR);
            }
            break;

        case STATE_PUNCHING:
            /* Send probes during punch window */
            if (ctx.sender_known)
            {
                fpv_receiver_send_probe(ctx.receiver, ctx.session_id,
                                        0, 0x12345678, &ctx.sender_addr);
            }
            break;

        case STATE_STREAMING:
            send_periodic_messages(&ctx);
            break;

        case STATE_ERROR:
            g_running = false;
            break;

        default:
            break;
        }

        /* Check for new frame from network thread */
        pthread_mutex_lock(&ctx.frame_slot.mutex);
        if (ctx.frame_slot.has_new_frame && ctx.frame_slot.pixbuf)
        {
            /* Create a decoded frame struct for the renderer */
            fpv_decoded_frame_t frame = {
                .native_handle = ctx.frame_slot.pixbuf,
                .width = ctx.frame_slot.width,
                .height = ctx.frame_slot.height,
                .frame_id = ctx.frame_slot.frame_id};
            CVPixelBufferRetain(ctx.frame_slot.pixbuf);

            /* Extract timing telemetry */
            uint64_t timing_us[3] = {
                ctx.frame_slot.first_packet_time_us,
                ctx.frame_slot.assembly_complete_us,
                ctx.frame_slot.decode_complete_us};
            ctx.frame_slot.has_new_frame = false;
            pthread_mutex_unlock(&ctx.frame_slot.mutex);

            /* Upload texture with timing (this is the only slow part, but doesn't block decode) */
            fpv_renderer_update_frame_with_timing(ctx.renderer, &frame, timing_us);
            CVPixelBufferRelease((CVPixelBufferRef)frame.native_handle);
        }
        else
        {
            pthread_mutex_unlock(&ctx.frame_slot.mutex);
        }

        /* Render at display refresh rate */
        glClearColor(0.1f, 0.1f, 0.1f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);

        fpv_renderer_draw(ctx.renderer, ctx.window_width, ctx.window_height);

        glfwSwapBuffers(ctx.window);
        glfwPollEvents();

        /* Print stats periodically */
        print_stats(&ctx);
    }

    printf("\n[EXIT] Shutting down...\n");

    /* Stop network thread */
    ctx.network_thread_running = false;
    pthread_join(ctx.network_thread, NULL);

    /* Cleanup frame slot */
    if (ctx.frame_slot.pixbuf)
    {
        CVPixelBufferRelease(ctx.frame_slot.pixbuf);
    }
    pthread_mutex_destroy(&ctx.frame_slot.mutex);

    /* Cleanup */
    fpv_renderer_destroy(ctx.renderer);
    fpv_decoder_destroy(ctx.decoder);
    fpv_assembler_destroy(&ctx.assembler);
    fpv_receiver_destroy(ctx.receiver);
    glfwDestroyWindow(ctx.window);
    glfwTerminate();

    return 0;
}
