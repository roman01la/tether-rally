/*
 * FPV Sender - UDP Sender
 *
 * Packetizes H.264 frames into UDP fragments per FPV_PLAN.md protocol.
 */

#ifndef FPV_SENDER_SENDER_H
#define FPV_SENDER_SENDER_H

#include "protocol.h"
#include "encoder.h"
#include <netinet/in.h>
#include <stdbool.h>

/* Sender configuration */
typedef struct
{
    int max_payload_size; /* Max UDP payload (default 1200) */
    uint32_t stream_id;   /* Stream ID (default 1) */
} fpv_sender_config_t;

#define FPV_SENDER_CONFIG_DEFAULT {           \
    .max_payload_size = FPV_MAX_PAYLOAD_SIZE, \
    .stream_id = 1}

/* Sender statistics */
typedef struct
{
    uint64_t frames_sent;
    uint64_t fragments_sent;
    uint64_t bytes_sent;
    uint64_t send_errors;
    uint64_t keyframes_sent;
} fpv_sender_stats_t;

/* Sender state (opaque) */
typedef struct fpv_sender fpv_sender_t;

/* Create sender */
fpv_sender_t *fpv_sender_create(int sockfd, uint32_t session_id,
                                const fpv_sender_config_t *config);

/* Destroy sender */
void fpv_sender_destroy(fpv_sender_t *s);

/* Set peer address */
void fpv_sender_set_peer(fpv_sender_t *s, const struct sockaddr_in *peer);

/* Send an encoded frame (fragments it as needed) */
int fpv_sender_send_frame(fpv_sender_t *s, const fpv_encoded_frame_t *frame);

/* Send keepalive */
int fpv_sender_send_keepalive(fpv_sender_t *s, uint32_t echo_ts_ms);

/* Send probe */
int fpv_sender_send_probe(fpv_sender_t *s, uint64_t nonce);

/* Get statistics */
fpv_sender_stats_t fpv_sender_get_stats(fpv_sender_t *s);

#endif /* FPV_SENDER_SENDER_H */
