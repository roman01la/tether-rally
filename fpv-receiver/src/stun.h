/*
 * STUN Client - Minimal implementation for macOS receiver
 */

#ifndef FPV_STUN_H
#define FPV_STUN_H

#include <stdint.h>
#include <netinet/in.h>

/* STUN result */
typedef struct
{
    struct sockaddr_in local_addr;
    struct sockaddr_in public_addr;
    char server[64];
} fpv_stun_result_t;

/*
 * Perform STUN binding using an existing socket.
 * Returns 0 on success, fills result.
 */
int fpv_stun_discover(int sockfd, fpv_stun_result_t *result);

#endif /* FPV_STUN_H */
