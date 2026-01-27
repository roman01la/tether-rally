/*
 * FPV Protocol - Wire format implementation
 */

#include "protocol.h"
#include <string.h>
#include <arpa/inet.h>

/* Big-endian helpers */
static inline uint16_t read_u16(const uint8_t *buf)
{
    return ((uint16_t)buf[0] << 8) | buf[1];
}

static inline uint32_t read_u32(const uint8_t *buf)
{
    return ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
           ((uint32_t)buf[2] << 8) | buf[3];
}

static inline uint64_t read_u64(const uint8_t *buf)
{
    return ((uint64_t)read_u32(buf) << 32) | read_u32(buf + 4);
}

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

int fpv_parse_msg_type(const uint8_t *buf, size_t len, uint8_t *msg_type)
{
    if (len < 1)
        return -1;
    *msg_type = buf[0];
    return 0;
}

int fpv_parse_common_header(const uint8_t *buf, size_t len, fpv_common_header_t *hdr)
{
    if (len < FPV_COMMON_HEADER_SIZE)
        return -1;

    hdr->msg_type = buf[0];
    hdr->version = buf[1];
    hdr->header_len = read_u16(buf + 2);
    hdr->session_id = read_u32(buf + 4);

    if (hdr->version != FPV_VERSION)
        return -2;
    if (hdr->header_len < FPV_COMMON_HEADER_SIZE)
        return -3;

    return 0;
}

int fpv_parse_video_fragment(const uint8_t *buf, size_t len, fpv_video_fragment_t *frag)
{
    if (len < FPV_VIDEO_FRAGMENT_HEADER_SIZE)
        return -1;
    if (buf[0] != FPV_MSG_VIDEO_FRAGMENT)
        return -2;
    if (buf[1] != FPV_VERSION)
        return -3;

    frag->session_id = read_u32(buf + 4);
    frag->stream_id = read_u32(buf + 8);
    frag->frame_id = read_u32(buf + 12);
    frag->frag_index = read_u16(buf + 16);
    frag->frag_count = read_u16(buf + 18);
    frag->ts_ms = read_u32(buf + 20);
    frag->flags = buf[24];
    frag->codec = buf[25];
    frag->payload_len = read_u16(buf + 26);

    if (frag->codec != FPV_CODEC_H264)
        return -4;
    if (frag->frag_count == 0 || frag->frag_index >= frag->frag_count)
        return -5;
    if (len < FPV_VIDEO_FRAGMENT_HEADER_SIZE + frag->payload_len)
        return -6;

    frag->payload = buf + FPV_VIDEO_FRAGMENT_HEADER_SIZE;
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

int fpv_parse_probe(const uint8_t *buf, size_t len, fpv_probe_t *probe)
{
    if (len < FPV_PROBE_HEADER_SIZE)
        return -1;
    if (buf[0] != FPV_MSG_PROBE)
        return -2;
    if (buf[1] != FPV_VERSION)
        return -3;

    probe->session_id = read_u32(buf + 4);
    probe->ts_ms = read_u32(buf + 8);
    probe->probe_seq = read_u32(buf + 12);
    probe->nonce = read_u64(buf + 16);
    probe->role = buf[24];
    probe->flags = buf[25];
    return 0;
}

int fpv_parse_hello(const uint8_t *buf, size_t len, fpv_hello_t *hello)
{
    if (len < FPV_HELLO_HEADER_SIZE)
        return -1;
    if (buf[0] != FPV_MSG_HELLO)
        return -2;
    if (buf[1] != FPV_VERSION)
        return -3;

    hello->session_id = read_u32(buf + 4);
    hello->width = read_u16(buf + 8);
    hello->height = read_u16(buf + 10);
    hello->fps_x10 = read_u16(buf + 12);
    hello->bitrate_bps = read_u32(buf + 14);
    hello->avc_profile = buf[18];
    hello->avc_level = buf[19];
    hello->idr_interval_frames = read_u32(buf + 20);
    return 0;
}

int fpv_marshal_keepalive(const fpv_keepalive_t *ka, uint8_t *buf, size_t len)
{
    if (len < FPV_KEEPALIVE_HEADER_SIZE)
        return -1;

    buf[0] = FPV_MSG_KEEPALIVE;
    buf[1] = FPV_VERSION;
    write_u16(buf + 2, FPV_KEEPALIVE_HEADER_SIZE);
    write_u32(buf + 4, ka->session_id);
    write_u32(buf + 8, ka->ts_ms);
    write_u32(buf + 12, ka->seq);
    write_u32(buf + 16, ka->echo_ts_ms);
    return FPV_KEEPALIVE_HEADER_SIZE;
}

int fpv_marshal_idr_request(const fpv_idr_request_t *req, uint8_t *buf, size_t len)
{
    if (len < FPV_IDR_REQUEST_HEADER_SIZE)
        return -1;

    buf[0] = FPV_MSG_IDR_REQUEST;
    buf[1] = FPV_VERSION;
    write_u16(buf + 2, FPV_IDR_REQUEST_HEADER_SIZE);
    write_u32(buf + 4, req->session_id);
    write_u32(buf + 8, req->seq);
    write_u32(buf + 12, req->ts_ms);
    buf[16] = req->reason;
    buf[17] = 0; /* reserved */
    buf[18] = 0;
    buf[19] = 0;
    return FPV_IDR_REQUEST_HEADER_SIZE;
}

int fpv_marshal_probe(const fpv_probe_t *probe, uint8_t *buf, size_t len)
{
    if (len < FPV_PROBE_HEADER_SIZE)
        return -1;

    buf[0] = FPV_MSG_PROBE;
    buf[1] = FPV_VERSION;
    write_u16(buf + 2, FPV_PROBE_HEADER_SIZE);
    write_u32(buf + 4, probe->session_id);
    write_u32(buf + 8, probe->ts_ms);
    write_u32(buf + 12, probe->probe_seq);
    write_u64(buf + 16, probe->nonce);
    buf[24] = probe->role;
    buf[25] = probe->flags;
    buf[26] = 0; /* reserved */
    buf[27] = 0;
    return FPV_PROBE_HEADER_SIZE;
}

/* Wrap-around comparison (RFC 1982 serial arithmetic) */
bool fpv_is_newer(uint32_t a, uint32_t b)
{
    return (int32_t)(a - b) > 0;
}

bool fpv_is_older(uint32_t a, uint32_t b)
{
    return (int32_t)(a - b) < 0;
}
