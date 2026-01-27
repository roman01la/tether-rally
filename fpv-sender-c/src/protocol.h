/*
 * FPV Sender - Protocol Implementation
 *
 * Wire format matching fpv-receiver's protocol.h
 */

#ifndef FPV_SENDER_PROTOCOL_H
#define FPV_SENDER_PROTOCOL_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

/* Protocol version */
#define FPV_VERSION 1

/* Maximum UDP payload (avoid IP fragmentation) */
#define FPV_MAX_PAYLOAD_SIZE 1200

/* Message types */
#define FPV_MSG_VIDEO_FRAGMENT 0x01
#define FPV_MSG_KEEPALIVE 0x02
#define FPV_MSG_IDR_REQUEST 0x03
#define FPV_MSG_PROBE 0x04
#define FPV_MSG_HELLO 0x05

/* Video flags */
#define FPV_FLAG_KEYFRAME (1 << 0)
#define FPV_FLAG_SPSPPS (1 << 1)

/* Codec types */
#define FPV_CODEC_H264 1

/* Roles */
#define FPV_ROLE_PI 1
#define FPV_ROLE_MAC 2

/* Header sizes */
#define FPV_COMMON_HEADER_SIZE 8
#define FPV_VIDEO_FRAGMENT_HEADER_SIZE 28
#define FPV_KEEPALIVE_HEADER_SIZE 20
#define FPV_IDR_REQUEST_HEADER_SIZE 20
#define FPV_PROBE_HEADER_SIZE 28

/* Video fragment structure */
typedef struct
{
    uint32_t session_id;
    uint32_t stream_id;
    uint32_t frame_id;
    uint16_t frag_index;
    uint16_t frag_count;
    uint32_t ts_ms;
    uint8_t flags;
    uint8_t codec;
    uint16_t payload_len;
    const uint8_t *payload;
} fpv_video_fragment_t;

/* Keepalive structure */
typedef struct
{
    uint32_t session_id;
    uint32_t ts_ms;
    uint32_t seq;
    uint32_t echo_ts_ms;
} fpv_keepalive_t;

/* IDR request structure */
typedef struct
{
    uint32_t session_id;
    uint32_t seq;
    uint32_t ts_ms;
    uint8_t reason;
} fpv_idr_request_t;

/* Probe structure */
typedef struct
{
    uint32_t session_id;
    uint32_t ts_ms;
    uint32_t probe_seq;
    uint64_t nonce;
    uint8_t role;
    uint8_t flags;
} fpv_probe_t;

/* Serialize video fragment to buffer. Returns bytes written or -1 on error. */
int fpv_write_video_fragment(uint8_t *buf, size_t buf_len, const fpv_video_fragment_t *frag);

/* Serialize keepalive to buffer. Returns bytes written or -1 on error. */
int fpv_write_keepalive(uint8_t *buf, size_t buf_len, const fpv_keepalive_t *ka);

/* Serialize probe to buffer. Returns bytes written or -1 on error. */
int fpv_write_probe(uint8_t *buf, size_t buf_len, const fpv_probe_t *probe);

/* Parse IDR request from buffer. Returns 0 on success, -1 on error. */
int fpv_parse_idr_request(const uint8_t *buf, size_t len, fpv_idr_request_t *idr);

/* Parse keepalive from buffer. Returns 0 on success, -1 on error. */
int fpv_parse_keepalive(const uint8_t *buf, size_t len, fpv_keepalive_t *ka);

/* Get current monotonic time in milliseconds */
uint32_t fpv_get_time_ms(void);

#endif /* FPV_SENDER_PROTOCOL_H */
