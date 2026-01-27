/*
 * FPV Sender - UDP Sender Implementation
 */

#include "sender.h"
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <time.h>

struct fpv_sender
{
    int sockfd;
    uint32_t session_id;
    fpv_sender_config_t config;
    struct sockaddr_in peer_addr;
    bool peer_set;

    /* Counters */
    uint32_t keepalive_seq;
    uint32_t probe_seq;
    uint32_t start_time_ms;

    /* Stats */
    fpv_sender_stats_t stats;

    /* Packet buffer */
    uint8_t buf[FPV_MAX_PAYLOAD_SIZE + FPV_VIDEO_FRAGMENT_HEADER_SIZE];
};

fpv_sender_t *fpv_sender_create(int sockfd, uint32_t session_id,
                                const fpv_sender_config_t *config)
{
    fpv_sender_t *s = calloc(1, sizeof(fpv_sender_t));
    if (!s)
        return NULL;

    s->sockfd = sockfd;
    s->session_id = session_id;

    if (config)
    {
        s->config = *config;
    }
    else
    {
        s->config = (fpv_sender_config_t)FPV_SENDER_CONFIG_DEFAULT;
    }

    s->start_time_ms = fpv_get_time_ms();

    return s;
}

void fpv_sender_destroy(fpv_sender_t *s)
{
    free(s);
}

void fpv_sender_set_peer(fpv_sender_t *s, const struct sockaddr_in *peer)
{
    if (!s || !peer)
        return;
    s->peer_addr = *peer;
    s->peer_set = true;
}

int fpv_sender_send_frame(fpv_sender_t *s, const fpv_encoded_frame_t *frame)
{
    if (!s || !frame || !s->peer_set)
        return -1;

    const uint8_t *data = frame->data;
    size_t remaining = frame->len;

    /* Calculate fragment count */
    int max_payload = s->config.max_payload_size - FPV_VIDEO_FRAGMENT_HEADER_SIZE;
    int frag_count = (remaining + max_payload - 1) / max_payload;
    if (frag_count == 0)
        frag_count = 1;
    if (frag_count > 65535)
        return -1; /* Too large */

    uint32_t ts_ms = fpv_get_time_ms() - s->start_time_ms;

    /* Build flags */
    uint8_t flags = 0;
    if (frame->is_keyframe)
        flags |= FPV_FLAG_KEYFRAME;
    if (frame->has_spspps)
        flags |= FPV_FLAG_SPSPPS;

    /* Send each fragment */
    int sent = 0;
    for (int i = 0; i < frag_count; i++)
    {
        size_t chunk = remaining > (size_t)max_payload ? (size_t)max_payload : remaining;

        fpv_video_fragment_t frag = {
            .session_id = s->session_id,
            .stream_id = s->config.stream_id,
            .frame_id = frame->frame_id,
            .frag_index = (uint16_t)i,
            .frag_count = (uint16_t)frag_count,
            .ts_ms = ts_ms,
            .flags = flags,
            .codec = FPV_CODEC_H264,
            .payload_len = (uint16_t)chunk,
            .payload = data};

        int pkt_len = fpv_write_video_fragment(s->buf, sizeof(s->buf), &frag);
        if (pkt_len < 0)
        {
            s->stats.send_errors++;
            return -1;
        }

        ssize_t n = sendto(s->sockfd, s->buf, pkt_len, 0,
                           (struct sockaddr *)&s->peer_addr, sizeof(s->peer_addr));
        if (n < 0)
        {
            s->stats.send_errors++;
            /* Per spec: drop remainder and continue to next frame */
            return sent;
        }

        s->stats.fragments_sent++;
        s->stats.bytes_sent += n;
        sent++;

        data += chunk;
        remaining -= chunk;

        /* Brief pause between fragments to avoid burst loss (200µs) */
        if (i < frag_count - 1)
        {
            usleep(200);
        }
    }

    s->stats.frames_sent++;
    if (frame->is_keyframe)
    {
        s->stats.keyframes_sent++;
    }

    return sent;
}

int fpv_sender_send_keepalive(fpv_sender_t *s, uint32_t echo_ts_ms)
{
    if (!s || !s->peer_set)
        return -1;

    fpv_keepalive_t ka = {
        .session_id = s->session_id,
        .ts_ms = fpv_get_time_ms() - s->start_time_ms,
        .seq = s->keepalive_seq++,
        .echo_ts_ms = echo_ts_ms};

    int pkt_len = fpv_write_keepalive(s->buf, sizeof(s->buf), &ka);
    if (pkt_len < 0)
        return -1;

    ssize_t n = sendto(s->sockfd, s->buf, pkt_len, 0,
                       (struct sockaddr *)&s->peer_addr, sizeof(s->peer_addr));
    return n > 0 ? 0 : -1;
}

int fpv_sender_send_probe(fpv_sender_t *s, uint64_t nonce)
{
    if (!s || !s->peer_set)
        return -1;

    fpv_probe_t probe = {
        .session_id = s->session_id,
        .ts_ms = fpv_get_time_ms() - s->start_time_ms,
        .probe_seq = s->probe_seq++,
        .nonce = nonce,
        .role = FPV_ROLE_PI,
        .flags = 0};

    int pkt_len = fpv_write_probe(s->buf, sizeof(s->buf), &probe);
    if (pkt_len < 0)
        return -1;

    ssize_t n = sendto(s->sockfd, s->buf, pkt_len, 0,
                       (struct sockaddr *)&s->peer_addr, sizeof(s->peer_addr));
    return n > 0 ? 0 : -1;
}

fpv_sender_stats_t fpv_sender_get_stats(fpv_sender_t *s)
{
    if (!s)
    {
        fpv_sender_stats_t empty = {0};
        return empty;
    }
    return s->stats;
}
