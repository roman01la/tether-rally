/*
 * UDP Receiver - Receives packets and manages network state
 */

#ifndef FPV_RECEIVER_H
#define FPV_RECEIVER_H

#include "protocol.h"
#include <stdint.h>
#include <stdbool.h>
#include <netinet/in.h>

/* Receiver configuration */
typedef struct
{
    int local_port;
    size_t recv_buf_size; /* SO_RCVBUF (default 64KB) */
} fpv_receiver_config_t;

/* Receiver statistics */
typedef struct
{
    uint64_t packets_received;
    uint64_t bytes_received;
    uint64_t invalid_packets;
    uint32_t last_rx_ts_ms;
} fpv_receiver_stats_t;

/* Receiver state */
typedef struct fpv_receiver fpv_receiver_t;

/* Create receiver */
fpv_receiver_t *fpv_receiver_create(const fpv_receiver_config_t *config);

/* Destroy receiver */
void fpv_receiver_destroy(fpv_receiver_t *r);

/* Get socket file descriptor (for select/poll) */
int fpv_receiver_get_fd(fpv_receiver_t *r);

/* Get local address */
int fpv_receiver_get_local_addr(fpv_receiver_t *r, struct sockaddr_in *addr);

/*
 * Receive a packet (non-blocking)
 * Returns packet length on success, 0 if no packet, negative on error.
 * Caller provides buffer.
 */
int fpv_receiver_recv(fpv_receiver_t *r, uint8_t *buf, size_t buf_len,
                      struct sockaddr_in *from_addr);

/* Send packet to address */
int fpv_receiver_send(fpv_receiver_t *r, const uint8_t *buf, size_t len,
                      const struct sockaddr_in *to_addr);

/* Send keepalive */
int fpv_receiver_send_keepalive(fpv_receiver_t *r, uint32_t session_id,
                                uint32_t seq, uint32_t echo_ts_ms,
                                const struct sockaddr_in *to_addr);

/* Send IDR request */
int fpv_receiver_send_idr_request(fpv_receiver_t *r, uint32_t session_id,
                                  uint32_t seq, uint8_t reason,
                                  const struct sockaddr_in *to_addr);

/* Send probe */
int fpv_receiver_send_probe(fpv_receiver_t *r, uint32_t session_id,
                            uint32_t seq, uint64_t nonce,
                            const struct sockaddr_in *to_addr);

/* Get statistics */
fpv_receiver_stats_t fpv_receiver_get_stats(fpv_receiver_t *r);

#endif /* FPV_RECEIVER_H */
