/*
 * FPV Sender - Main Entry Point
 *
 * Low-latency H.264 video streaming from Raspberry Pi camera
 * Uses V4L2 for camera capture and hardware H.264 encoding
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
#include <getopt.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <sys/socket.h>
#include <time.h>

#include "protocol.h"
#include "camera.h"
#include "encoder.h"
#include "sender.h"
#include "stun.h"

/* Configuration */
static struct
{
    int width;
    int height;
    int fps;
    int bitrate_kbps;
    int idr_interval;
    const char *peer_host;
    uint16_t peer_port;
    uint16_t local_port;
    const char *stun_server;
    uint16_t stun_port;
    uint32_t session_id;
    bool verbose;
} config = {
    .width = 1280,
    .height = 720,
    .fps = 60,
    .bitrate_kbps = 2000,
    .idr_interval = 30,
    .peer_host = NULL,
    .peer_port = 5000,
    .local_port = 5001,
    .stun_server = NULL,
    .stun_port = 3478,
    .session_id = 0,
    .verbose = false};

/* Global state */
static volatile bool g_running = true;
static fpv_camera_t *g_camera = NULL;
static fpv_encoder_t *g_encoder = NULL;
static fpv_sender_t *g_sender = NULL;
static int g_sockfd = -1;

static uint64_t g_frame_count = 0;
static uint64_t g_start_time_ms = 0;
static uint64_t g_last_stats_time_ms = 0;

/* Signal handler */
static void signal_handler(int sig)
{
    (void)sig;
    printf("\nShutting down...\n");
    g_running = false;
}

/* Get monotonic time in milliseconds */
static uint64_t get_time_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000ULL + ts.tv_nsec / 1000000ULL;
}

/* Encoded frame callback - called from encoder thread */
static void on_encoded_frame(const fpv_encoded_frame_t *frame, void *userdata)
{
    (void)userdata;

    if (!g_sender || !g_running)
        return;

    int sent = fpv_sender_send_frame(g_sender, frame);

    if (config.verbose && sent > 0)
    {
        printf("Sent frame %u: %zu bytes, %d frags, keyframe=%d\n",
               frame->frame_id, frame->len, sent, frame->is_keyframe);
    }

    g_frame_count++;
}

/* Camera frame callback - called from camera thread */
static void on_camera_frame(const fpv_camera_frame_t *frame, void *userdata)
{
    (void)userdata;

    if (!g_encoder || !g_running)
        return;

    /* Feed frame to encoder */
    fpv_encoder_encode(g_encoder, frame);
}

/* Handle incoming packets (IDR requests, probes, etc.) */
static void handle_incoming_packets(void)
{
    uint8_t buf[1500];
    struct sockaddr_in from;
    socklen_t from_len = sizeof(from);

    while (1)
    {
        ssize_t n = recvfrom(g_sockfd, buf, sizeof(buf), MSG_DONTWAIT,
                             (struct sockaddr *)&from, &from_len);
        if (n <= 0)
            break;

        uint8_t msg_type = buf[0];

        if (msg_type == FPV_MSG_IDR_REQUEST)
        {
            fpv_idr_request_t req;
            if (fpv_parse_idr_request(buf, n, &req))
            {
                if (config.verbose)
                {
                    printf("Received IDR request from %s:%d\n",
                           inet_ntoa(from.sin_addr), ntohs(from.sin_port));
                }
                if (g_encoder)
                {
                    fpv_encoder_request_idr(g_encoder);
                }
            }
        }
        else if (msg_type == FPV_MSG_PROBE)
        {
            /* Respond to probes for latency measurement */
            if (config.verbose)
            {
                printf("Received probe from %s:%d\n",
                       inet_ntoa(from.sin_addr), ntohs(from.sin_port));
            }
        }
        else if (msg_type == FPV_MSG_KEEPALIVE)
        {
            /* Update peer address if needed */
        }
    }
}

/* Print statistics */
static void print_stats(void)
{
    uint64_t now = get_time_ms();
    uint64_t elapsed = now - g_start_time_ms;

    if (elapsed == 0)
        return;

    double fps = (double)g_frame_count * 1000.0 / elapsed;

    printf("Stats: frames=%llu, fps=%.1f",
           (unsigned long long)g_frame_count, fps);

    if (g_sender)
    {
        fpv_sender_stats_t stats = fpv_sender_get_stats(g_sender);
        double mbps = (double)stats.bytes_sent * 8.0 / elapsed / 1000.0;
        printf(", sent=%llu frags, %.2f Mbps, keyframes=%llu, errors=%llu",
               (unsigned long long)stats.fragments_sent,
               mbps,
               (unsigned long long)stats.keyframes_sent,
               (unsigned long long)stats.send_errors);
    }

    if (g_encoder)
    {
        fpv_encoder_stats_t stats = fpv_encoder_get_stats(g_encoder);
        printf(", enc_in=%llu enc_out=%llu",
               (unsigned long long)stats.frames_in,
               (unsigned long long)stats.frames_out);
    }

    printf("\n");
}

static void print_usage(const char *argv0)
{
    printf("Usage: %s [options]\n", argv0);
    printf("Options:\n");
    printf("  -w, --width <n>       Video width (default: %d)\n", config.width);
    printf("  -h, --height <n>      Video height (default: %d)\n", config.height);
    printf("  -f, --fps <n>         Frames per second (default: %d)\n", config.fps);
    printf("  -b, --bitrate <n>     Bitrate in kbps (default: %d)\n", config.bitrate_kbps);
    printf("  -i, --idr <n>         IDR interval in frames (default: %d)\n", config.idr_interval);
    printf("  -p, --peer <host:port> Peer address to send to\n");
    printf("  -l, --local <port>    Local UDP port (default: %d)\n", config.local_port);
    printf("  -s, --stun <host>     STUN server for NAT traversal\n");
    printf("  --session <id>        Session ID (default: random)\n");
    printf("  -v, --verbose         Verbose output\n");
    printf("  --help                Show this help\n");
}

static int parse_args(int argc, char *argv[])
{
    static struct option long_options[] = {
        {"width", required_argument, 0, 'w'},
        {"height", required_argument, 0, 'h'},
        {"fps", required_argument, 0, 'f'},
        {"bitrate", required_argument, 0, 'b'},
        {"idr", required_argument, 0, 'i'},
        {"peer", required_argument, 0, 'p'},
        {"local", required_argument, 0, 'l'},
        {"stun", required_argument, 0, 's'},
        {"session", required_argument, 0, 'S'},
        {"verbose", no_argument, 0, 'v'},
        {"help", no_argument, 0, 'H'},
        {0, 0, 0, 0}};

    int c;
    while ((c = getopt_long(argc, argv, "w:h:f:b:i:p:l:s:v", long_options, NULL)) != -1)
    {
        switch (c)
        {
        case 'w':
            config.width = atoi(optarg);
            break;
        case 'h':
            config.height = atoi(optarg);
            break;
        case 'f':
            config.fps = atoi(optarg);
            break;
        case 'b':
            config.bitrate_kbps = atoi(optarg);
            break;
        case 'i':
            config.idr_interval = atoi(optarg);
            break;
        case 'p':
        {
            /* Parse host:port */
            char *colon = strchr(optarg, ':');
            if (colon)
            {
                *colon = '\0';
                config.peer_host = optarg;
                config.peer_port = atoi(colon + 1);
            }
            else
            {
                config.peer_host = optarg;
            }
            break;
        }
        case 'l':
            config.local_port = atoi(optarg);
            break;
        case 's':
            config.stun_server = optarg;
            break;
        case 'S':
            config.session_id = strtoul(optarg, NULL, 0);
            break;
        case 'v':
            config.verbose = true;
            break;
        case 'H':
            print_usage(argv[0]);
            exit(0);
        default:
            print_usage(argv[0]);
            return -1;
        }
    }

    if (!config.peer_host)
    {
        fprintf(stderr, "Error: peer address required (-p host:port)\n");
        print_usage(argv[0]);
        return -1;
    }

    return 0;
}

int main(int argc, char *argv[])
{
    if (parse_args(argc, argv) < 0)
    {
        return 1;
    }

    /* Generate session ID if not provided */
    if (config.session_id == 0)
    {
        config.session_id = (uint32_t)time(NULL) ^ (uint32_t)getpid();
    }

    printf("FPV Sender starting...\n");
    printf("  Resolution: %dx%d @ %dfps\n", config.width, config.height, config.fps);
    printf("  Bitrate: %d kbps\n", config.bitrate_kbps);
    printf("  IDR interval: %d frames\n", config.idr_interval);
    printf("  Peer: %s:%d\n", config.peer_host, config.peer_port);
    printf("  Session: 0x%08X\n", config.session_id);

    /* Setup signal handler */
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    /* Create UDP socket */
    g_sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (g_sockfd < 0)
    {
        perror("socket");
        return 1;
    }

    /* Bind to local port */
    struct sockaddr_in local_addr = {
        .sin_family = AF_INET,
        .sin_port = htons(config.local_port),
        .sin_addr.s_addr = INADDR_ANY};

    if (bind(g_sockfd, (struct sockaddr *)&local_addr, sizeof(local_addr)) < 0)
    {
        perror("bind");
        close(g_sockfd);
        return 1;
    }

    /* Optional STUN binding */
    if (config.stun_server)
    {
        printf("Performing STUN binding to %s:%d...\n",
               config.stun_server, config.stun_port);

        fpv_stun_config_t stun_cfg = {
            .server_host = config.stun_server,
            .server_port = config.stun_port,
            .auth = NULL};

        fpv_stun_result_t stun_result;
        if (fpv_stun_bind(g_sockfd, &stun_cfg, &stun_result, 3000))
        {
            printf("STUN: mapped address %s:%d\n",
                   inet_ntoa(stun_result.mapped_addr.sin_addr),
                   ntohs(stun_result.mapped_addr.sin_port));
        }
        else
        {
            printf("STUN binding failed (continuing anyway)\n");
        }
    }

    /* Resolve peer address */
    struct sockaddr_in peer_addr = {
        .sin_family = AF_INET,
        .sin_port = htons(config.peer_port)};

    if (inet_pton(AF_INET, config.peer_host, &peer_addr.sin_addr) != 1)
    {
        /* Try DNS resolution */
        struct hostent *he = gethostbyname(config.peer_host);
        if (!he)
        {
            fprintf(stderr, "Cannot resolve %s\n", config.peer_host);
            close(g_sockfd);
            return 1;
        }
        memcpy(&peer_addr.sin_addr, he->h_addr_list[0], sizeof(peer_addr.sin_addr));
    }

    printf("Resolved peer: %s:%d\n",
           inet_ntoa(peer_addr.sin_addr), ntohs(peer_addr.sin_port));

    /* Create sender */
    fpv_sender_config_t sender_cfg = FPV_SENDER_CONFIG_DEFAULT;
    g_sender = fpv_sender_create(g_sockfd, config.session_id, &sender_cfg);
    if (!g_sender)
    {
        fprintf(stderr, "Failed to create sender\n");
        close(g_sockfd);
        return 1;
    }
    fpv_sender_set_peer(g_sender, &peer_addr);

    /* Create encoder */
    fpv_encoder_config_t enc_cfg = {
        .width = config.width,
        .height = config.height,
        .fps = config.fps,
        .bitrate_kbps = config.bitrate_kbps,
        .idr_interval = config.idr_interval,
        .profile = FPV_PROFILE_BASELINE,
        .level = FPV_LEVEL_31};

    g_encoder = fpv_encoder_create(&enc_cfg, on_encoded_frame, NULL);
    if (!g_encoder)
    {
        fprintf(stderr, "Failed to create encoder (is /dev/video11 available?)\n");
        fpv_sender_destroy(g_sender);
        close(g_sockfd);
        return 1;
    }

    /* Create camera */
    fpv_camera_config_t cam_cfg = {
        .width = config.width,
        .height = config.height,
        .fps = config.fps,
        .rotation = 0,
        .hflip = false,
        .vflip = false,
        .camera_name = NULL};

    g_camera = fpv_camera_create(&cam_cfg, on_camera_frame, NULL);
    if (!g_camera)
    {
        fprintf(stderr, "Failed to open camera (is /dev/video0 available?)\n");
        fpv_encoder_destroy(g_encoder);
        fpv_sender_destroy(g_sender);
        close(g_sockfd);
        return 1;
    }

    printf("FPV sender running. Press Ctrl+C to stop.\n");

    g_start_time_ms = get_time_ms();
    g_last_stats_time_ms = g_start_time_ms;

    /* Main loop */
    while (g_running)
    {
        /* Handle incoming packets */
        handle_incoming_packets();

        /* Send keepalive periodically */
        static uint64_t last_keepalive = 0;
        uint64_t now = get_time_ms();
        if (now - last_keepalive > 1000)
        {
            fpv_sender_send_keepalive(g_sender, 0);
            last_keepalive = now;
        }

        /* Print stats every 5 seconds */
        if (now - g_last_stats_time_ms > 5000)
        {
            print_stats();
            g_last_stats_time_ms = now;
        }

        usleep(10000); /* 10ms */
    }

    /* Cleanup */
    printf("Stopping...\n");

    if (g_camera)
        fpv_camera_destroy(g_camera);
    if (g_encoder)
        fpv_encoder_destroy(g_encoder);
    if (g_sender)
        fpv_sender_destroy(g_sender);
    if (g_sockfd >= 0)
        close(g_sockfd);

    print_stats();
    printf("Done.\n");

    return 0;
}
