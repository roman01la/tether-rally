/*
 * STUN Client - Implementation
 */

#include "stun.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <netdb.h>

#define STUN_BINDING_REQUEST 0x0001
#define STUN_BINDING_RESPONSE 0x0101
#define STUN_MAGIC_COOKIE 0x2112A442
#define ATTR_MAPPED_ADDRESS 0x0001
#define ATTR_XOR_MAPPED_ADDRESS 0x0020

static const char *stun_servers[] = {
    "stun.cloudflare.com",
    "stun.l.google.com",
    "stun1.l.google.com",
    NULL};

static int parse_stun_response(const uint8_t *buf, size_t len,
                               const uint8_t *txn_id,
                               struct sockaddr_in *public_addr)
{
    if (len < 20)
        return -1;

    uint16_t msg_type = (buf[0] << 8) | buf[1];
    if (msg_type != STUN_BINDING_RESPONSE)
        return -1;

    uint16_t msg_len = (buf[2] << 8) | buf[3];
    uint32_t magic = (buf[4] << 24) | (buf[5] << 16) | (buf[6] << 8) | buf[7];
    if (magic != STUN_MAGIC_COOKIE)
        return -1;

    /* Check transaction ID */
    if (memcmp(buf + 8, txn_id, 12) != 0)
        return -1;

    /* Parse attributes */
    size_t offset = 20;
    while (offset + 4 <= 20 + msg_len && offset + 4 <= len)
    {
        uint16_t attr_type = (buf[offset] << 8) | buf[offset + 1];
        uint16_t attr_len = (buf[offset + 2] << 8) | buf[offset + 3];

        if (offset + 4 + attr_len > len)
            break;

        const uint8_t *attr_data = buf + offset + 4;

        if (attr_type == ATTR_XOR_MAPPED_ADDRESS && attr_len >= 8)
        {
            uint8_t family = attr_data[1];
            if (family == 0x01)
            { /* IPv4 */
                uint16_t xport = (attr_data[2] << 8) | attr_data[3];
                uint16_t port = xport ^ (STUN_MAGIC_COOKIE >> 16);

                uint32_t xaddr = (attr_data[4] << 24) | (attr_data[5] << 16) |
                                 (attr_data[6] << 8) | attr_data[7];
                uint32_t addr = xaddr ^ STUN_MAGIC_COOKIE;

                public_addr->sin_family = AF_INET;
                public_addr->sin_port = htons(port);
                public_addr->sin_addr.s_addr = htonl(addr);
                return 0;
            }
        }
        else if (attr_type == ATTR_MAPPED_ADDRESS && attr_len >= 8)
        {
            uint8_t family = attr_data[1];
            if (family == 0x01)
            { /* IPv4 */
                uint16_t port = (attr_data[2] << 8) | attr_data[3];

                public_addr->sin_family = AF_INET;
                public_addr->sin_port = htons(port);
                memcpy(&public_addr->sin_addr.s_addr, attr_data + 4, 4);
                return 0;
            }
        }

        /* Next attribute (padded to 4 bytes) */
        offset += 4 + ((attr_len + 3) & ~3);
    }

    return -1;
}

int fpv_stun_discover(int sockfd, fpv_stun_result_t *result)
{
    /* Get local address */
    socklen_t addr_len = sizeof(result->local_addr);
    if (getsockname(sockfd, (struct sockaddr *)&result->local_addr, &addr_len) < 0)
    {
        return -1;
    }

    /* Build STUN binding request */
    uint8_t request[20];
    request[0] = (STUN_BINDING_REQUEST >> 8) & 0xFF;
    request[1] = STUN_BINDING_REQUEST & 0xFF;
    request[2] = 0; /* length = 0 */
    request[3] = 0;
    request[4] = (STUN_MAGIC_COOKIE >> 24) & 0xFF;
    request[5] = (STUN_MAGIC_COOKIE >> 16) & 0xFF;
    request[6] = (STUN_MAGIC_COOKIE >> 8) & 0xFF;
    request[7] = STUN_MAGIC_COOKIE & 0xFF;

    /* Transaction ID (12 random bytes) */
    uint8_t txn_id[12];
    for (int i = 0; i < 12; i++)
    {
        txn_id[i] = rand() & 0xFF;
    }
    memcpy(request + 8, txn_id, 12);

    /* Try each STUN server */
    for (int s = 0; stun_servers[s]; s++)
    {
        /* Resolve server */
        struct addrinfo hints = {0};
        hints.ai_family = AF_INET;
        hints.ai_socktype = SOCK_DGRAM;

        struct addrinfo *res;
        if (getaddrinfo(stun_servers[s], "3478", &hints, &res) != 0)
        {
            continue;
        }

        struct sockaddr_in server_addr;
        memcpy(&server_addr, res->ai_addr, sizeof(server_addr));
        freeaddrinfo(res);

        /* Try a few times */
        for (int attempt = 0; attempt < 3; attempt++)
        {
            /* Send request */
            if (sendto(sockfd, request, sizeof(request), 0,
                       (struct sockaddr *)&server_addr, sizeof(server_addr)) < 0)
            {
                continue;
            }

            /* Wait for response (with timeout) */
            fd_set fds;
            FD_ZERO(&fds);
            FD_SET(sockfd, &fds);

            struct timeval tv = {.tv_sec = 1, .tv_usec = 0};
            int ready = select(sockfd + 1, &fds, NULL, NULL, &tv);

            if (ready <= 0)
                continue;

            /* Receive response */
            uint8_t response[1024];
            ssize_t n = recv(sockfd, response, sizeof(response), 0);
            if (n < 20)
                continue;

            /* Parse response */
            if (parse_stun_response(response, n, txn_id, &result->public_addr) == 0)
            {
                strncpy(result->server, stun_servers[s], sizeof(result->server) - 1);
                return 0;
            }
        }
    }

    return -1;
}
