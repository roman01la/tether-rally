/*
 * FPV Sender - STUN Client Implementation
 */

#include "stun.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netdb.h>
#include <poll.h>
#include <time.h>
#include <fcntl.h>

void fpv_stun_generate_txn_id(uint8_t txn_id[12])
{
    /* Use random bytes for transaction ID */
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd >= 0)
    {
        read(fd, txn_id, 12);
        close(fd);
    }
    else
    {
        /* Fallback to rand() */
        srand(time(NULL));
        for (int i = 0; i < 12; i++)
        {
            txn_id[i] = rand() & 0xFF;
        }
    }
}

int fpv_stun_build_binding_request(uint8_t *buf, size_t buf_len,
                                   const uint8_t transaction_id[12],
                                   const fpv_stun_auth_t *auth)
{
    if (buf_len < STUN_HEADER_SIZE)
        return -1;

    int pos = 0;

    /* Message Type: Binding Request (0x0001) */
    buf[pos++] = 0x00;
    buf[pos++] = 0x01;

    /* Message Length placeholder (filled later) */
    int len_pos = pos;
    buf[pos++] = 0x00;
    buf[pos++] = 0x00;

    /* Magic Cookie */
    buf[pos++] = 0x21;
    buf[pos++] = 0x12;
    buf[pos++] = 0xA4;
    buf[pos++] = 0x42;

    /* Transaction ID (12 bytes) */
    memcpy(&buf[pos], transaction_id, 12);
    pos += 12;

    /* Add USERNAME attribute if auth provided */
    if (auth && auth->username)
    {
        size_t ulen = strlen(auth->username);
        size_t padded_len = (ulen + 3) & ~3; /* Pad to 4-byte boundary */

        if (pos + 4 + padded_len > buf_len)
            return -1;

        buf[pos++] = 0x00;
        buf[pos++] = 0x06; /* USERNAME type */
        buf[pos++] = (ulen >> 8) & 0xFF;
        buf[pos++] = ulen & 0xFF;
        memcpy(&buf[pos], auth->username, ulen);
        pos += ulen;
        /* Padding */
        while (pos % 4 != 0)
            buf[pos++] = 0x00;
    }

    /* Update message length (excludes 20-byte header) */
    int msg_len = pos - STUN_HEADER_SIZE;
    buf[len_pos] = (msg_len >> 8) & 0xFF;
    buf[len_pos + 1] = msg_len & 0xFF;

    return pos;
}

bool fpv_stun_parse_binding_response(const uint8_t *buf, size_t len,
                                     const uint8_t expected_txn_id[12],
                                     fpv_stun_result_t *result)
{
    if (len < STUN_HEADER_SIZE)
        return false;

    memset(result, 0, sizeof(*result));

    /* Check message type is Binding Response */
    uint16_t msg_type = (buf[0] << 8) | buf[1];
    if (msg_type != STUN_BINDING_RESPONSE)
    {
        return false;
    }

    /* Verify magic cookie */
    uint32_t cookie = (buf[4] << 24) | (buf[5] << 16) | (buf[6] << 8) | buf[7];
    if (cookie != STUN_MAGIC_COOKIE)
        return false;

    /* Verify transaction ID */
    if (memcmp(&buf[8], expected_txn_id, 12) != 0)
        return false;

    uint16_t msg_len = (buf[2] << 8) | buf[3];
    if (len < STUN_HEADER_SIZE + msg_len)
        return false;

    /* Parse attributes */
    size_t pos = STUN_HEADER_SIZE;
    while (pos + 4 <= len)
    {
        uint16_t attr_type = (buf[pos] << 8) | buf[pos + 1];
        uint16_t attr_len = (buf[pos + 2] << 8) | buf[pos + 3];
        pos += 4;

        if (pos + attr_len > len)
            break;

        if (attr_type == STUN_ATTR_XOR_MAPPED_ADDRESS && attr_len >= 8)
        {
            /* XOR-MAPPED-ADDRESS */
            uint8_t family = buf[pos + 1];
            if (family == 0x01)
            { /* IPv4 */
                uint16_t port_xor = (buf[pos + 2] << 8) | buf[pos + 3];
                uint16_t port = port_xor ^ (STUN_MAGIC_COOKIE >> 16);

                uint32_t addr_xor = (buf[pos + 4] << 24) | (buf[pos + 5] << 16) |
                                    (buf[pos + 6] << 8) | buf[pos + 7];
                uint32_t addr = addr_xor ^ STUN_MAGIC_COOKIE;

                result->mapped_addr.sin_family = AF_INET;
                result->mapped_addr.sin_port = htons(port);
                result->mapped_addr.sin_addr.s_addr = htonl(addr);
                result->success = true;
            }
        }
        else if (attr_type == STUN_ATTR_MAPPED_ADDRESS && attr_len >= 8 && !result->success)
        {
            /* MAPPED-ADDRESS (fallback if no XOR-MAPPED-ADDRESS) */
            uint8_t family = buf[pos + 1];
            if (family == 0x01)
            { /* IPv4 */
                uint16_t port = (buf[pos + 2] << 8) | buf[pos + 3];
                uint32_t addr = (buf[pos + 4] << 24) | (buf[pos + 5] << 16) |
                                (buf[pos + 6] << 8) | buf[pos + 7];

                result->mapped_addr.sin_family = AF_INET;
                result->mapped_addr.sin_port = htons(port);
                result->mapped_addr.sin_addr.s_addr = htonl(addr);
                result->success = true;
            }
        }
        else if (attr_type == STUN_ATTR_ERROR_CODE && attr_len >= 4)
        {
            result->error_code = (buf[pos + 2] & 0x07) * 100 + buf[pos + 3];
        }

        /* Move to next attribute (padded to 4 bytes) */
        pos += (attr_len + 3) & ~3;
    }

    return result->success;
}

bool fpv_stun_bind(int sockfd, const fpv_stun_config_t *config,
                   fpv_stun_result_t *result, int timeout_ms)
{
    if (!config || !result)
        return false;

    memset(result, 0, sizeof(*result));

    /* Resolve STUN server address */
    struct addrinfo hints = {0}, *res = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_DGRAM;

    char port_str[16];
    snprintf(port_str, sizeof(port_str), "%u", config->server_port);

    if (getaddrinfo(config->server_host, port_str, &hints, &res) != 0)
    {
        return false;
    }

    /* Generate transaction ID */
    uint8_t txn_id[12];
    fpv_stun_generate_txn_id(txn_id);

    /* Build request */
    uint8_t req_buf[512];
    int req_len = fpv_stun_build_binding_request(req_buf, sizeof(req_buf),
                                                 txn_id, config->auth);
    if (req_len < 0)
    {
        freeaddrinfo(res);
        return false;
    }

    /* Send request */
    if (sendto(sockfd, req_buf, req_len, 0, res->ai_addr, res->ai_addrlen) < 0)
    {
        freeaddrinfo(res);
        return false;
    }

    freeaddrinfo(res);

    /* Wait for response */
    struct pollfd pfd = {.fd = sockfd, .events = POLLIN};
    int ret = poll(&pfd, 1, timeout_ms);
    if (ret <= 0)
    {
        return false;
    }

    /* Receive response */
    uint8_t resp_buf[1500];
    ssize_t n = recv(sockfd, resp_buf, sizeof(resp_buf), 0);
    if (n < 0)
    {
        return false;
    }

    return fpv_stun_parse_binding_response(resp_buf, n, txn_id, result);
}
