/*
 * FPV Sender - STUN Client
 * Minimal STUN implementation for NAT traversal
 */

#ifndef FPV_STUN_H
#define FPV_STUN_H

#include <stdint.h>
#include <stdbool.h>
#include <netinet/in.h>

#define STUN_MAGIC_COOKIE 0x2112A442
#define STUN_HEADER_SIZE 20
#define STUN_BINDING_REQUEST 0x0001
#define STUN_BINDING_RESPONSE 0x0101

/* STUN Attributes */
#define STUN_ATTR_MAPPED_ADDRESS 0x0001
#define STUN_ATTR_XOR_MAPPED_ADDRESS 0x0020
#define STUN_ATTR_USERNAME 0x0006
#define STUN_ATTR_MESSAGE_INTEGRITY 0x0008
#define STUN_ATTR_REALM 0x0014
#define STUN_ATTR_NONCE 0x0015
#define STUN_ATTR_ERROR_CODE 0x0009

/* TURN Authentication */
typedef struct
{
    const char *username;
    const char *password;
    const char *realm;
    const char *nonce;
} fpv_stun_auth_t;

/* STUN/TURN server config */
typedef struct
{
    const char *server_host;
    uint16_t server_port;
    fpv_stun_auth_t *auth; /* NULL for simple STUN, set for TURN */
} fpv_stun_config_t;

/* Result of STUN binding request */
typedef struct
{
    struct sockaddr_in mapped_addr;
    bool success;
    int error_code;
} fpv_stun_result_t;

/*
 * Generate a STUN binding request
 * Returns: number of bytes written, or -1 on error
 */
int fpv_stun_build_binding_request(uint8_t *buf, size_t buf_len,
                                   const uint8_t transaction_id[12],
                                   const fpv_stun_auth_t *auth);

/*
 * Parse a STUN binding response
 * Returns: true on success with mapped_addr filled in
 */
bool fpv_stun_parse_binding_response(const uint8_t *buf, size_t len,
                                     const uint8_t expected_txn_id[12],
                                     fpv_stun_result_t *result);

/*
 * Perform a STUN binding request (blocking)
 * Returns: true on success with result filled in
 */
bool fpv_stun_bind(int sockfd, const fpv_stun_config_t *config,
                   fpv_stun_result_t *result, int timeout_ms);

/*
 * Generate a random transaction ID
 */
void fpv_stun_generate_txn_id(uint8_t txn_id[12]);

/*
 * Helper: Check if packet looks like STUN
 */
static inline bool fpv_is_stun_packet(const uint8_t *buf, size_t len)
{
    if (len < STUN_HEADER_SIZE)
        return false;
    /* First two bits must be 0 for STUN, and check magic cookie */
    if ((buf[0] & 0xC0) != 0x00)
        return false;
    uint32_t cookie = (buf[4] << 24) | (buf[5] << 16) | (buf[6] << 8) | buf[7];
    return cookie == STUN_MAGIC_COOKIE;
}

#endif /* FPV_STUN_H */
