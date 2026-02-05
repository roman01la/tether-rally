/**
 * zfec header for WASM build
 */

#ifndef FEC_H
#define FEC_H

#include <stddef.h>

typedef unsigned char gf;

typedef struct
{
    unsigned long magic;
    unsigned short k, n;
    gf *enc_matrix;
} fec_t;

#if defined(_MSC_VER)
#define restrict
#endif

void fec_init(void);
fec_t *fec_new(unsigned short k, unsigned short n);
void fec_free(fec_t *p);
void fec_encode(const fec_t *code, const gf *restrict const *restrict const src,
                gf *restrict const *restrict const fecs,
                const unsigned *restrict const block_nums, size_t num_block_nums, size_t sz);
void fec_decode(const fec_t *code, const gf *restrict const *restrict const inpkts,
                gf *restrict const *restrict const outpkts,
                const unsigned *restrict const index, size_t sz);

#ifdef __GNUC__
#ifndef alloca
#define alloca(x) __builtin_alloca(x)
#endif
#endif

#endif
