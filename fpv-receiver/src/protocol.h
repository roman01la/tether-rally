/*
 * FPV Protocol - Wire format definitions per FPV_PLAN.md Appendix A
 *
 * All multi-byte integers are big-endian (network order).
 * All structures are packed with no padding.
 */

#ifndef FPV_PROTOCOL_H
#define FPV_PROTOCOL_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

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

/* IDR request reasons */
#define FPV_IDR_REASON_STARTUP 1
#define FPV_IDR_REASON_DECODE_ERROR 2
#define FPV_IDR_REASON_LOSS 3
#define FPV_IDR_REASON_USER 4

/* Header sizes */
#define FPV_COMMON_HEADER_SIZE 8
#define FPV_VIDEO_FRAGMENT_HEADER_SIZE 28
#define FPV_KEEPALIVE_HEADER_SIZE 20
#define FPV_IDR_REQUEST_HEADER_SIZE 20
#define FPV_PROBE_HEADER_SIZE 28
#define FPV_HELLO_HEADER_SIZE 32

/* Default timing constants */
#define FPV_PROBE_INTERVAL_MS 20
#define FPV_PUNCH_WINDOW_MS 3000
#define FPV_KEEPALIVE_INTERVAL_MS 1000
#define FPV_SESSION_IDLE_TIMEOUT_MS 3000
#define FPV_FRAME_TIMEOUT_MS 80    /* 80ms - ~5 frames at 60fps, tolerates jitter */
#define FPV_MAX_INFLIGHT_FRAMES 12 /* More headroom for 720p (10-17 packets/frame) */

/*
 * Common header (8 bytes)
 *
 * Offset | Size | Type | Name
 *      0 |    1 | u8   | msg_type
 *      1 |    1 | u8   | version
 *      2 |    2 | u16  | header_len
 *      4 |    4 | u32  | session_id
 */
typedef struct
{
    uint8_t msg_type;
    uint8_t version;
    uint16_t header_len;
    uint32_t session_id;
} fpv_common_header_t;

/*
 * VIDEO_FRAGMENT (msg_type = 0x01)
 *
 * Common header + type-specific:
 * Offset | Size | Type  | Name
 *      8 |    4 | u32   | stream_id
 *     12 |    4 | u32   | frame_id
 *     16 |    2 | u16   | frag_index
 *     18 |    2 | u16   | frag_count
 *     20 |    4 | u32   | ts_ms
 *     24 |    1 | u8    | flags
 *     25 |    1 | u8    | codec
 *     26 |    2 | u16   | payload_len
 *     28 |    N | bytes | payload
 */
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

/*
 * KEEPALIVE (msg_type = 0x02)
 *
 * Offset | Size | Type | Name
 *      8 |    4 | u32  | ts_ms
 *     12 |    4 | u32  | seq
 *     16 |    4 | u32  | echo_ts_ms
 */
typedef struct
{
    uint32_t session_id;
    uint32_t ts_ms;
    uint32_t seq;
    uint32_t echo_ts_ms;
} fpv_keepalive_t;

/*
 * IDR_REQUEST (msg_type = 0x03)
 *
 * Offset | Size | Type  | Name
 *      8 |    4 | u32   | seq
 *     12 |    4 | u32   | ts_ms
 *     16 |    1 | u8    | reason
 *     17 |    3 | bytes | reserved
 */
typedef struct
{
    uint32_t session_id;
    uint32_t seq;
    uint32_t ts_ms;
    uint8_t reason;
} fpv_idr_request_t;

/*
 * PROBE (msg_type = 0x04)
 *
 * Offset | Size | Type | Name
 *      8 |    4 | u32  | ts_ms
 *     12 |    4 | u32  | probe_seq
 *     16 |    8 | u64  | nonce
 *     24 |    1 | u8   | role
 *     25 |    1 | u8   | flags
 *     26 |    2 | u16  | reserved
 */
typedef struct
{
    uint32_t session_id;
    uint32_t ts_ms;
    uint32_t probe_seq;
    uint64_t nonce;
    uint8_t role;
    uint8_t flags;
} fpv_probe_t;

/*
 * HELLO (msg_type = 0x05)
 *
 * Offset | Size | Type  | Name
 *      8 |    2 | u16   | width
 *     10 |    2 | u16   | height
 *     12 |    2 | u16   | fps_x10
 *     14 |    4 | u32   | bitrate_bps
 *     18 |    1 | u8    | avc_profile
 *     19 |    1 | u8    | avc_level
 *     20 |    4 | u32   | idr_interval_frames
 *     24 |    8 | bytes | reserved
 */
typedef struct
{
    uint32_t session_id;
    uint16_t width;
    uint16_t height;
    uint16_t fps_x10;
    uint32_t bitrate_bps;
    uint8_t avc_profile;
    uint8_t avc_level;
    uint32_t idr_interval_frames;
} fpv_hello_t;

/* Parsing functions */
int fpv_parse_msg_type(const uint8_t *buf, size_t len, uint8_t *msg_type);
int fpv_parse_common_header(const uint8_t *buf, size_t len, fpv_common_header_t *hdr);
int fpv_parse_video_fragment(const uint8_t *buf, size_t len, fpv_video_fragment_t *frag);
int fpv_parse_keepalive(const uint8_t *buf, size_t len, fpv_keepalive_t *ka);
int fpv_parse_probe(const uint8_t *buf, size_t len, fpv_probe_t *probe);
int fpv_parse_hello(const uint8_t *buf, size_t len, fpv_hello_t *hello);

/* Marshaling functions */
int fpv_marshal_keepalive(const fpv_keepalive_t *ka, uint8_t *buf, size_t len);
int fpv_marshal_idr_request(const fpv_idr_request_t *req, uint8_t *buf, size_t len);
int fpv_marshal_probe(const fpv_probe_t *probe, uint8_t *buf, size_t len);

/* Utility functions */
bool fpv_is_newer(uint32_t a, uint32_t b);
bool fpv_is_older(uint32_t a, uint32_t b);

#endif /* FPV_PROTOCOL_H */
