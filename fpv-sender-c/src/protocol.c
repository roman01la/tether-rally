/*
 * FPV Sender - Protocol Implementation
 */

#include "protocol.h"
#include <string.h>
#include <time.h>

/* Big-endian write helpers */
static inline void write_u16(uint8_t *buf, uint16_t val)
{
    buf[0] = (val >> 8) & 0xFF;
    buf[1] = val & 0xFF;
}

static inline void write_u32(uint8_t *buf, uint32_t val)
{
    buf[0] = (val >> 24) & 0xFF;
    buf[1] = (val >> 16) & 0xFF;
    buf[2] = (val >> 8) & 0xFF;
    buf[3] = val & 0xFF;
}

static inline void write_u64(uint8_t *buf, uint64_t val)
{
    write_u32(buf, (uint32_t)(val >> 32));
    write_u32(buf + 4, (uint32_t)val);
}

/* Big-endian read helpers */
static inline uint16_t read_u16(const uint8_t *buf)
{
    return ((uint16_t)buf[0] << 8) | buf[1];
}

static inline uint32_t read_u32(const uint8_t *buf)
{
    return ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
           ((uint32_t)buf[2] << 8) | buf[3];
}

int fpv_write_video_fragment(uint8_t *buf, size_t buf_len, const fpv_video_fragment_t *frag)
{
    size_t total_len = FPV_VIDEO_FRAGMENT_HEADER_SIZE + frag->payload_len;
    if (buf_len < total_len)
        return -1;

    /* Common header */
    buf[0] = FPV_MSG_VIDEO_FRAGMENT;
    buf[1] = FPV_VERSION;
    write_u16(buf + 2, FPV_VIDEO_FRAGMENT_HEADER_SIZE);
    write_u32(buf + 4, frag->session_id);

    /* Video fragment header */
    write_u32(buf + 8, frag->stream_id);
    write_u32(buf + 12, frag->frame_id);
    write_u16(buf + 16, frag->frag_index);
    write_u16(buf + 18, frag->frag_count);
    write_u32(buf + 20, frag->ts_ms);
    buf[24] = frag->flags;
    buf[25] = frag->codec;
    write_u16(buf + 26, frag->payload_len);

    /* Payload */
    memcpy(buf + FPV_VIDEO_FRAGMENT_HEADER_SIZE, frag->payload, frag->payload_len);

    return (int)total_len;
}

int fpv_write_keepalive(uint8_t *buf, size_t buf_len, const fpv_keepalive_t *ka)
{
    if (buf_len < FPV_KEEPALIVE_HEADER_SIZE)
        return -1;

    /* Common header */
    buf[0] = FPV_MSG_KEEPALIVE;
    buf[1] = FPV_VERSION;
    write_u16(buf + 2, FPV_KEEPALIVE_HEADER_SIZE);
    write_u32(buf + 4, ka->session_id);

    /* Keepalive fields */
    write_u32(buf + 8, ka->ts_ms);
    write_u32(buf + 12, ka->seq);
    write_u32(buf + 16, ka->echo_ts_ms);

    return FPV_KEEPALIVE_HEADER_SIZE;
}

int fpv_write_probe(uint8_t *buf, size_t buf_len, const fpv_probe_t *probe)
{
    if (buf_len < FPV_PROBE_HEADER_SIZE)
        return -1;

    /* Common header */
    buf[0] = FPV_MSG_PROBE;
    buf[1] = FPV_VERSION;
    write_u16(buf + 2, FPV_PROBE_HEADER_SIZE);
    write_u32(buf + 4, probe->session_id);

    /* Probe fields */
    write_u32(buf + 8, probe->ts_ms);
    write_u32(buf + 12, probe->probe_seq);
    write_u64(buf + 16, probe->nonce);
    buf[24] = probe->role;
    buf[25] = probe->flags;
    buf[26] = 0; /* reserved */
    buf[27] = 0;

    return FPV_PROBE_HEADER_SIZE;
}

int fpv_parse_idr_request(const uint8_t *buf, size_t len, fpv_idr_request_t *idr)
{
    if (len < FPV_IDR_REQUEST_HEADER_SIZE)
        return -1;
    if (buf[0] != FPV_MSG_IDR_REQUEST)
        return -2;
    if (buf[1] != FPV_VERSION)
        return -3;

    idr->session_id = read_u32(buf + 4);
    idr->seq = read_u32(buf + 8);
    idr->ts_ms = read_u32(buf + 12);
    idr->reason = buf[16];

    return 0;
}

int fpv_parse_keepalive(const uint8_t *buf, size_t len, fpv_keepalive_t *ka)
{
    if (len < FPV_KEEPALIVE_HEADER_SIZE)
        return -1;
    if (buf[0] != FPV_MSG_KEEPALIVE)
        return -2;
    if (buf[1] != FPV_VERSION)
        return -3;

    ka->session_id = read_u32(buf + 4);
    ka->ts_ms = read_u32(buf + 8);
    ka->seq = read_u32(buf + 12);
    ka->echo_ts_ms = read_u32(buf + 16);

    return 0;
}

uint32_t fpv_get_time_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}
