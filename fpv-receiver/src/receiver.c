/*
 * UDP Receiver - Implementation
 */

#include "receiver.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/socket.h>
#include <arpa/inet.h>

#define DEFAULT_RECV_BUF_SIZE (64 * 1024) /* 64 KB per spec */

struct fpv_receiver
{
    int sockfd;
    fpv_receiver_stats_t stats;
    uint64_t start_time_us;
};

/* Get monotonic time in microseconds */
extern uint64_t fpv_get_time_us(void);

fpv_receiver_t *fpv_receiver_create(const fpv_receiver_config_t *config)
{
    fpv_receiver_t *r = calloc(1, sizeof(fpv_receiver_t));
    if (!r)
        return NULL;

    r->start_time_us = fpv_get_time_us();

    /* Create UDP socket */
    r->sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (r->sockfd < 0)
    {
        perror("socket");
        free(r);
        return NULL;
    }

    /* Set non-blocking */
    int flags = fcntl(r->sockfd, F_GETFL, 0);
    fcntl(r->sockfd, F_SETFL, flags | O_NONBLOCK);

    /* Set receive buffer size (per spec: 64KB to avoid hidden latency) */
    size_t buf_size = config->recv_buf_size > 0 ? config->recv_buf_size : DEFAULT_RECV_BUF_SIZE;
    if (setsockopt(r->sockfd, SOL_SOCKET, SO_RCVBUF, &buf_size, sizeof(buf_size)) < 0)
    {
        perror("setsockopt SO_RCVBUF");
    }

    /* Allow address reuse */
    int reuse = 1;
    setsockopt(r->sockfd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    /* Bind to local port */
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(config->local_port);

    if (bind(r->sockfd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
        perror("bind");
        close(r->sockfd);
        free(r);
        return NULL;
    }

    return r;
}

void fpv_receiver_destroy(fpv_receiver_t *r)
{
    if (!r)
        return;
    if (r->sockfd >= 0)
        close(r->sockfd);
    free(r);
}

int fpv_receiver_get_fd(fpv_receiver_t *r)
{
    return r ? r->sockfd : -1;
}

int fpv_receiver_get_local_addr(fpv_receiver_t *r, struct sockaddr_in *addr)
{
    if (!r || !addr)
        return -1;

    socklen_t len = sizeof(*addr);
    return getsockname(r->sockfd, (struct sockaddr *)addr, &len);
}

int fpv_receiver_recv(fpv_receiver_t *r, uint8_t *buf, size_t buf_len,
                      struct sockaddr_in *from_addr)
{
    if (!r || !buf)
        return -1;

    socklen_t addr_len = sizeof(*from_addr);
    ssize_t n = recvfrom(r->sockfd, buf, buf_len, 0,
                         (struct sockaddr *)from_addr, &addr_len);

    if (n < 0)
    {
        if (errno == EAGAIN || errno == EWOULDBLOCK)
        {
            return 0; /* No packet available */
        }
        return -1;
    }

    r->stats.packets_received++;
    r->stats.bytes_received += n;

    /* Parse timestamp if it's a known message type */
    if (n >= FPV_COMMON_HEADER_SIZE)
    {
        uint8_t msg_type = buf[0];
        if (msg_type == FPV_MSG_KEEPALIVE && n >= FPV_KEEPALIVE_HEADER_SIZE)
        {
            fpv_keepalive_t ka;
            if (fpv_parse_keepalive(buf, n, &ka) == 0)
            {
                r->stats.last_rx_ts_ms = ka.ts_ms;
            }
        }
        else if (msg_type == FPV_MSG_VIDEO_FRAGMENT && n >= FPV_VIDEO_FRAGMENT_HEADER_SIZE)
        {
            fpv_video_fragment_t frag;
            if (fpv_parse_video_fragment(buf, n, &frag) == 0)
            {
                r->stats.last_rx_ts_ms = frag.ts_ms;
            }
        }
    }

    return (int)n;
}

int fpv_receiver_send(fpv_receiver_t *r, const uint8_t *buf, size_t len,
                      const struct sockaddr_in *to_addr)
{
    if (!r || !buf || !to_addr)
        return -1;

    ssize_t n = sendto(r->sockfd, buf, len, 0,
                       (const struct sockaddr *)to_addr, sizeof(*to_addr));
    return (int)n;
}

int fpv_receiver_send_keepalive(fpv_receiver_t *r, uint32_t session_id,
                                uint32_t seq, uint32_t echo_ts_ms,
                                const struct sockaddr_in *to_addr)
{
    uint8_t buf[FPV_KEEPALIVE_HEADER_SIZE];

    fpv_keepalive_t ka = {
        .session_id = session_id,
        .ts_ms = (uint32_t)((fpv_get_time_us() - r->start_time_us) / 1000),
        .seq = seq,
        .echo_ts_ms = echo_ts_ms};

    int len = fpv_marshal_keepalive(&ka, buf, sizeof(buf));
    if (len < 0)
        return -1;

    return fpv_receiver_send(r, buf, len, to_addr);
}

int fpv_receiver_send_idr_request(fpv_receiver_t *r, uint32_t session_id,
                                  uint32_t seq, uint8_t reason,
                                  const struct sockaddr_in *to_addr)
{
    uint8_t buf[FPV_IDR_REQUEST_HEADER_SIZE];

    fpv_idr_request_t req = {
        .session_id = session_id,
        .seq = seq,
        .ts_ms = (uint32_t)((fpv_get_time_us() - r->start_time_us) / 1000),
        .reason = reason};

    int len = fpv_marshal_idr_request(&req, buf, sizeof(buf));
    if (len < 0)
        return -1;

    return fpv_receiver_send(r, buf, len, to_addr);
}

int fpv_receiver_send_probe(fpv_receiver_t *r, uint32_t session_id,
                            uint32_t seq, uint64_t nonce,
                            const struct sockaddr_in *to_addr)
{
    uint8_t buf[FPV_PROBE_HEADER_SIZE];

    fpv_probe_t probe = {
        .session_id = session_id,
        .ts_ms = (uint32_t)((fpv_get_time_us() - r->start_time_us) / 1000),
        .probe_seq = seq,
        .nonce = nonce,
        .role = FPV_ROLE_MAC,
        .flags = 0};

    int len = fpv_marshal_probe(&probe, buf, sizeof(buf));
    if (len < 0)
        return -1;

    return fpv_receiver_send(r, buf, len, to_addr);
}

fpv_receiver_stats_t fpv_receiver_get_stats(fpv_receiver_t *r)
{
    if (!r)
    {
        fpv_receiver_stats_t empty = {0};
        return empty;
    }
    return r->stats;
}
