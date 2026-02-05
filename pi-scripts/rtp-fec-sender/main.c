/**
 * RTP FEC Sender - Low-latency H.264 streaming with Reed-Solomon FEC
 *
 * Uses GStreamer appsink to receive RTP packets from libcamerasrc pipeline,
 * applies 8+2 Reed-Solomon FEC using vendored zfec, and sends via UDP.
 *
 * FEC Packet Format:
 *   | group_id (2B) | index (1B) | k (1B) | n (1B) | RTP packet... |
 *
 * Usage: rtp-fec-sender <client-ip> <client-port>
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <gst/gst.h>
#include <gst/app/gstappsink.h>

#include "fec.h"

#define FEC_K 4 /* Data packets per group */
#define FEC_N 7 /* Total packets per group (4 data + 3 parity) */
#define FEC_HEADER_SIZE 5
#define MAX_RTP_SIZE 1500

/* Global state */
static volatile int running = 1;
static int udp_sock = -1;
static struct sockaddr_in client_addr;
static fec_t *fec_codec = NULL;
static uint16_t group_id = 0;

/* Packet buffer for FEC encoding */
static uint8_t *packet_buffers[FEC_K];
static size_t packet_sizes[FEC_K];
static int packet_count = 0;
static size_t max_packet_size = 0;

/* Pre-allocated parity buffers for FEC encoding */
static uint8_t *parity_buffers[FEC_N - FEC_K];

/* GStreamer elements */
static GstElement *pipeline = NULL;
static GstElement *appsink = NULL;

static void signal_handler(int sig)
{
    (void)sig;
    running = 0;
    if (pipeline)
    {
        gst_element_send_event(pipeline, gst_event_new_eos());
    }
}

static void init_buffers(void)
{
    for (int i = 0; i < FEC_K; i++)
    {
        packet_buffers[i] = malloc(MAX_RTP_SIZE);
        packet_sizes[i] = 0;
    }
    for (int i = 0; i < FEC_N - FEC_K; i++)
    {
        parity_buffers[i] = malloc(MAX_RTP_SIZE);
    }
}

static void free_buffers(void)
{
    for (int i = 0; i < FEC_K; i++)
    {
        free(packet_buffers[i]);
        packet_buffers[i] = NULL;
    }
    for (int i = 0; i < FEC_N - FEC_K; i++)
    {
        free(parity_buffers[i]);
        parity_buffers[i] = NULL;
    }
}

static void send_fec_packet(uint16_t gid, uint8_t index, uint8_t k, uint8_t n,
                            const uint8_t *payload, size_t payload_size)
{
    uint8_t buffer[FEC_HEADER_SIZE + MAX_RTP_SIZE];

    /* FEC header: group_id (big-endian), index, k, n */
    buffer[0] = (gid >> 8) & 0xFF;
    buffer[1] = gid & 0xFF;
    buffer[2] = index;
    buffer[3] = k;
    buffer[4] = n;

    memcpy(buffer + FEC_HEADER_SIZE, payload, payload_size);

    sendto(udp_sock, buffer, FEC_HEADER_SIZE + payload_size, 0,
           (struct sockaddr *)&client_addr, sizeof(client_addr));
}

static void flush_fec_group(void)
{
    if (packet_count == 0)
        return;

    /* Pad all packets to max size for FEC encoding */
    for (int i = 0; i < packet_count; i++)
    {
        if (packet_sizes[i] < max_packet_size)
        {
            memset(packet_buffers[i] + packet_sizes[i], 0,
                   max_packet_size - packet_sizes[i]);
        }
    }

    /* If we have fewer than K packets, still send what we have */
    if (packet_count < FEC_K)
    {
        /* Send data packets without FEC */
        for (int i = 0; i < packet_count; i++)
        {
            send_fec_packet(group_id, i, packet_count, packet_count,
                            packet_buffers[i], packet_sizes[i]);
        }
    }
    else
    {
        /* Prepare source pointers */
        const gf *src_ptrs[FEC_K];
        for (int i = 0; i < FEC_K; i++)
        {
            src_ptrs[i] = packet_buffers[i];
        }

        /* Block numbers for parity packets (8, 9 for 8+2 FEC) */
        unsigned block_nums[FEC_N - FEC_K];
        for (int i = 0; i < FEC_N - FEC_K; i++)
        {
            block_nums[i] = FEC_K + i;
        }

        /* Encode */
        fec_encode(fec_codec, src_ptrs, parity_buffers, block_nums,
                   FEC_N - FEC_K, max_packet_size);

        /* Send all packets: data first, then parity */
        /* Pace packets to avoid micro-bursts (WFB-ng style) */
        for (int i = 0; i < FEC_K; i++)
        {
            send_fec_packet(group_id, i, FEC_K, FEC_N,
                            packet_buffers[i], packet_sizes[i]);
            usleep(200); /* 200µs inter-packet gap */
        }
        for (int i = 0; i < FEC_N - FEC_K; i++)
        {
            send_fec_packet(group_id, FEC_K + i, FEC_K, FEC_N,
                            parity_buffers[i], max_packet_size);
            if (i < FEC_N - FEC_K - 1)
                usleep(200); /* No delay after last packet */
        }
    }

    /* Reset for next group */
    group_id++;
    packet_count = 0;
    max_packet_size = 0;
}

static void handle_rtp_packet(const uint8_t *data, size_t size)
{
    if (size > MAX_RTP_SIZE)
    {
        fprintf(stderr, "RTP packet too large: %zu\n", size);
        return;
    }

    /* Add packet to buffer */
    memcpy(packet_buffers[packet_count], data, size);
    packet_sizes[packet_count] = size;
    if (size > max_packet_size)
    {
        max_packet_size = size;
    }
    packet_count++;

    /* Flush when we have K packets */
    if (packet_count >= FEC_K)
    {
        flush_fec_group();
    }
}

static GstFlowReturn on_new_sample(GstAppSink *sink, gpointer user_data)
{
    (void)user_data;

    GstSample *sample = gst_app_sink_pull_sample(sink);
    if (!sample)
    {
        return GST_FLOW_ERROR;
    }

    GstBuffer *buffer = gst_sample_get_buffer(sample);
    if (!buffer)
    {
        gst_sample_unref(sample);
        return GST_FLOW_ERROR;
    }

    GstMapInfo map;
    if (gst_buffer_map(buffer, &map, GST_MAP_READ))
    {
        handle_rtp_packet(map.data, map.size);
        gst_buffer_unmap(buffer, &map);
    }

    gst_sample_unref(sample);
    return GST_FLOW_OK;
}

static int setup_udp_socket(const char *ip, int port, int source_port)
{
    udp_sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (udp_sock < 0)
    {
        perror("socket");
        return -1;
    }

    /* Allow socket reuse for quick restarts */
    int reuse = 1;
    setsockopt(udp_sock, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    /* Small send buffer to prevent hidden queuing (WFB-ng style) */
    int sndbuf = 32768; /* 32KB instead of default ~200KB */
    setsockopt(udp_sock, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

    /* Bind to source port (same port used for hole punching) */
    if (source_port > 0)
    {
        struct sockaddr_in local_addr;
        memset(&local_addr, 0, sizeof(local_addr));
        local_addr.sin_family = AF_INET;
        local_addr.sin_addr.s_addr = INADDR_ANY;
        local_addr.sin_port = htons(source_port);

        if (bind(udp_sock, (struct sockaddr *)&local_addr, sizeof(local_addr)) < 0)
        {
            perror("bind");
            close(udp_sock);
            return -1;
        }
        printf("  Source port: %d\n", source_port);
    }

    memset(&client_addr, 0, sizeof(client_addr));
    client_addr.sin_family = AF_INET;
    client_addr.sin_port = htons(port);
    if (inet_pton(AF_INET, ip, &client_addr.sin_addr) <= 0)
    {
        perror("inet_pton");
        close(udp_sock);
        return -1;
    }

    return 0;
}

static int setup_gstreamer(int width, int height, int fps)
{
    GError *error = NULL;
    char pipeline_str[1024];

    /* Build pipeline string */
    snprintf(pipeline_str, sizeof(pipeline_str),
             "libcamerasrc ! "
             "video/x-raw,width=%d,height=%d,framerate=%d/1 ! "
             "v4l2h264enc extra-controls=\"controls,repeat_sequence_header=1,h264_i_frame_period=10,video_bitrate=1500000\" ! "
             "video/x-h264,profile=constrained-baseline,level=(string)4 ! "
             "h264parse config-interval=1 ! "
             "rtph264pay config-interval=1 pt=96 mtu=1400 ! "
             "appsink name=rtp emit-signals=true sync=false max-buffers=1 drop=true",
             width, height, fps);

    pipeline = gst_parse_launch(pipeline_str, &error);
    if (!pipeline)
    {
        fprintf(stderr, "Failed to create pipeline: %s\n",
                error ? error->message : "unknown error");
        if (error)
            g_error_free(error);
        return -1;
    }

    /* Get appsink and configure */
    appsink = gst_bin_get_by_name(GST_BIN(pipeline), "rtp");
    if (!appsink)
    {
        fprintf(stderr, "Failed to get appsink\n");
        gst_object_unref(pipeline);
        return -1;
    }

    /* Set up callbacks */
    GstAppSinkCallbacks callbacks = {
        .eos = NULL,
        .new_preroll = NULL,
        .new_sample = on_new_sample};
    gst_app_sink_set_callbacks(GST_APP_SINK(appsink), &callbacks, NULL, NULL);

    return 0;
}

static void print_usage(const char *prog)
{
    fprintf(stderr, "Usage: %s <client-ip> <client-port> <source-port> [width] [height] [fps]\n", prog);
    fprintf(stderr, "  client-ip    : Destination IP address\n");
    fprintf(stderr, "  client-port  : Destination UDP port\n");
    fprintf(stderr, "  source-port  : Local source port (for NAT traversal)\n");
    fprintf(stderr, "  width        : Video width (default: 1280)\n");
    fprintf(stderr, "  height       : Video height (default: 720)\n");
    fprintf(stderr, "  fps          : Framerate (default: 60)\n");
}

int main(int argc, char *argv[])
{
    if (argc < 4)
    {
        print_usage(argv[0]);
        return 1;
    }

    const char *client_ip = argv[1];
    int client_port = atoi(argv[2]);
    int source_port = atoi(argv[3]);
    int width = argc > 4 ? atoi(argv[4]) : 640;
    int height = argc > 5 ? atoi(argv[5]) : 480;
    int fps = argc > 6 ? atoi(argv[6]) : 60;

    printf("RTP FEC Sender starting...\n");
    printf("  Target: %s:%d\n", client_ip, client_port);
    printf("  Video: %dx%d @ %dfps\n", width, height, fps);
    printf("  FEC: %d+%d Reed-Solomon\n", FEC_K, FEC_N - FEC_K);

    /* Set up signal handlers */
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    /* Initialize FEC */
    fec_init();
    fec_codec = fec_new(FEC_K, FEC_N);
    if (!fec_codec)
    {
        fprintf(stderr, "Failed to create FEC codec\n");
        return 1;
    }

    /* Initialize buffers */
    init_buffers();

    /* Set up UDP socket */
    if (setup_udp_socket(client_ip, client_port, source_port) < 0)
    {
        fec_free(fec_codec);
        free_buffers();
        return 1;
    }

    /* Send hole punch packets to open NAT pinhole */
    printf("Sending hole punch packets...\n");
    for (int i = 0; i < 5; i++)
    {
        uint8_t punch = 0x00;
        sendto(udp_sock, &punch, 1, 0,
               (struct sockaddr *)&client_addr, sizeof(client_addr));
        usleep(50000); /* 50ms between punches */
    }

    /* Initialize GStreamer */
    gst_init(&argc, &argv);
    if (setup_gstreamer(width, height, fps) < 0)
    {
        close(udp_sock);
        fec_free(fec_codec);
        free_buffers();
        return 1;
    }

    /* Start pipeline */
    printf("Starting GStreamer pipeline...\n");
    GstStateChangeReturn ret = gst_element_set_state(pipeline, GST_STATE_PLAYING);
    if (ret == GST_STATE_CHANGE_FAILURE)
    {
        fprintf(stderr, "Failed to start pipeline\n");
        gst_object_unref(pipeline);
        close(udp_sock);
        fec_free(fec_codec);
        free_buffers();
        return 1;
    }

    printf("Streaming...\n");

    /* Run main loop */
    GstBus *bus = gst_element_get_bus(pipeline);
    while (running)
    {
        GstMessage *msg = gst_bus_timed_pop(bus, 100 * GST_MSECOND);
        if (msg)
        {
            switch (GST_MESSAGE_TYPE(msg))
            {
            case GST_MESSAGE_ERROR:
            {
                GError *err;
                gchar *debug;
                gst_message_parse_error(msg, &err, &debug);
                fprintf(stderr, "Error: %s\n", err->message);
                g_error_free(err);
                g_free(debug);
                running = 0;
                break;
            }
            case GST_MESSAGE_EOS:
                printf("End of stream\n");
                running = 0;
                break;
            default:
                break;
            }
            gst_message_unref(msg);
        }
    }

    /* Flush any remaining packets */
    flush_fec_group();

    /* Cleanup */
    printf("Shutting down...\n");
    gst_object_unref(bus);
    gst_element_set_state(pipeline, GST_STATE_NULL);
    gst_object_unref(appsink);
    gst_object_unref(pipeline);
    close(udp_sock);
    fec_free(fec_codec);
    free_buffers();

    return 0;
}
