/**
 * zfec -- fast forward error correction library with Python interface
 *
 * See README.rst for documentation.
 *
 * Vendored from: https://github.com/tahoe-lafs/zfec
 * License: BSD-2-Clause (see LICENSE below)
 */

#ifndef FEC_H
#define FEC_H

#include <stddef.h>

typedef unsigned char gf;

typedef struct
{
    unsigned long magic;
    unsigned short k, n; /* parameters of the code */
    gf *enc_matrix;
} fec_t;

#if defined(_MSC_VER)
#define restrict
#endif

/**
 * Initialize the fec library.
 *
 * Call this:
 *  - at least once
 *  - from at most one thread at a time
 *  - before calling any other APIs from the library
 *  - before creating any other threads that will use APIs from the library
 */
void fec_init(void);

/**
 * Create a new FEC encoder/decoder.
 * @param k the number of blocks required to reconstruct
 * @param n the total number of blocks created
 */
fec_t *fec_new(unsigned short k, unsigned short n);
void fec_free(fec_t *p);

/**
 * Encode data blocks into FEC parity blocks.
 * @param code the FEC codec
 * @param src the "primary blocks" i.e. the chunks of the input data (array of k pointers)
 * @param fecs buffers into which the secondary blocks will be written
 * @param block_nums the numbers of the desired check blocks (the id >= k)
 * @param num_block_nums the length of the block_nums array
 * @param sz size of a packet in bytes
 */
void fec_encode(const fec_t *code, const gf *restrict const *restrict const src,
                gf *restrict const *restrict const fecs,
                const unsigned *restrict const block_nums, size_t num_block_nums, size_t sz);

/**
 * Decode FEC-protected data.
 * @param code the FEC codec
 * @param inpkts an array of packets (size k); If a primary block, i, is present
 *        then it must be at index i. Secondary blocks can appear anywhere.
 * @param outpkts an array of buffers into which the reconstructed output packets
 *        will be written (only packets which are not present in the inpkts input
 *        will be reconstructed and written to outpkts)
 * @param index an array of the blocknums of the packets in inpkts
 * @param sz size of a packet in bytes
 */
void fec_decode(const fec_t *code, const gf *restrict const *restrict const inpkts,
                gf *restrict const *restrict const outpkts,
                const unsigned *restrict const index, size_t sz);

#if defined(_MSC_VER)
#define alloca _alloca
#else
#ifdef __GNUC__
#ifndef alloca
#define alloca(x) __builtin_alloca(x)
#endif
#else
#include <alloca.h>
#endif
#endif

#endif /* FEC_H */

/*
 * BSD-2-Clause License
 *
 * Copyright (C) 2007-2010 Zooko Wilcox-O'Hearn
 * Author: Zooko Wilcox-O'Hearn
 *
 * This work is derived from the "fec" software by Luigi Rizzo, et al.
 * fec.c -- forward error correction based on Vandermonde matrices
 * (C) 1997-98 Luigi Rizzo (luigi@iet.unipi.it)
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *
 * 1. Redistributions of source code must retain the above copyright notice,
 *    this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright notice,
 *    this list of conditions and the following disclaimer in the documentation
 *    and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE AUTHORS "AS IS" AND ANY EXPRESS OR IMPLIED
 * WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
 */
