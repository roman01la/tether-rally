/**
 * zfec WASM source for Emscripten compilation
 * Copy of pi-scripts/rtp-fec-sender/fec.c
 *
 * Build with: ./build.sh
 */

#include "fec.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

static const char *const Pp = "101110001";
static gf gf_exp[510];
static int gf_log[256];
static gf inverse[256];
static gf gf_mul_table[256][256];

static gf modnn(int x)
{
    while (x >= 255)
    {
        x -= 255;
        x = (x >> 8) + (x & 255);
    }
    return x;
}

#define SWAP(a, b, t) \
    {                 \
        t tmp;        \
        tmp = a;      \
        a = b;        \
        b = tmp;      \
    }
#define gf_mul(x, y) gf_mul_table[x][y]
#define USE_GF_MULC register gf *__gf_mulc_
#define GF_MULC0(c) __gf_mulc_ = gf_mul_table[c]
#define GF_ADDMULC(dst, x) dst ^= __gf_mulc_[x]

static void _init_mul_table(void)
{
    int i, j;
    for (i = 0; i < 256; i++)
        for (j = 0; j < 256; j++)
            gf_mul_table[i][j] = gf_exp[modnn(gf_log[i] + gf_log[j])];
    for (j = 0; j < 256; j++)
        gf_mul_table[0][j] = gf_mul_table[j][0] = 0;
}

#define NEW_GF_MATRIX(rows, cols) (gf *)malloc(rows *cols)

static void generate_gf(void)
{
    int i;
    gf mask;
    mask = 1;
    gf_exp[8] = 0;
    for (i = 0; i < 8; i++, mask <<= 1)
    {
        gf_exp[i] = mask;
        gf_log[gf_exp[i]] = i;
        if (Pp[i] == '1')
            gf_exp[8] ^= mask;
    }
    gf_log[gf_exp[8]] = 8;
    mask = 1 << 7;
    for (i = 9; i < 255; i++)
    {
        if (gf_exp[i - 1] >= mask)
            gf_exp[i] = gf_exp[8] ^ ((gf_exp[i - 1] ^ mask) << 1);
        else
            gf_exp[i] = gf_exp[i - 1] << 1;
        gf_log[gf_exp[i]] = i;
    }
    gf_log[0] = 255;
    for (i = 0; i < 255; i++)
        gf_exp[i + 255] = gf_exp[i];
    inverse[0] = 0;
    inverse[1] = 1;
    for (i = 2; i <= 255; i++)
        inverse[i] = gf_exp[255 - gf_log[i]];
}

#define addmul(dst, src, c, sz) \
    if (c != 0)                 \
    _addmul1(dst, src, c, sz)
#define UNROLL 16

static void _addmul1(register gf *restrict dst, const register gf *restrict src, gf c, size_t sz)
{
    USE_GF_MULC;
    const gf *lim = &dst[sz - UNROLL + 1];
    GF_MULC0(c);
#if (UNROLL > 1)
    for (; dst < lim; dst += UNROLL, src += UNROLL)
    {
        GF_ADDMULC(dst[0], src[0]);
        GF_ADDMULC(dst[1], src[1]);
        GF_ADDMULC(dst[2], src[2]);
        GF_ADDMULC(dst[3], src[3]);
#if (UNROLL > 4)
        GF_ADDMULC(dst[4], src[4]);
        GF_ADDMULC(dst[5], src[5]);
        GF_ADDMULC(dst[6], src[6]);
        GF_ADDMULC(dst[7], src[7]);
#endif
#if (UNROLL > 8)
        GF_ADDMULC(dst[8], src[8]);
        GF_ADDMULC(dst[9], src[9]);
        GF_ADDMULC(dst[10], src[10]);
        GF_ADDMULC(dst[11], src[11]);
        GF_ADDMULC(dst[12], src[12]);
        GF_ADDMULC(dst[13], src[13]);
        GF_ADDMULC(dst[14], src[14]);
        GF_ADDMULC(dst[15], src[15]);
#endif
    }
#endif
    lim += UNROLL - 1;
    for (; dst < lim; dst++, src++)
        GF_ADDMULC(*dst, *src);
}

static void _matmul(gf *a, gf *b, gf *c, unsigned n, unsigned k, unsigned m)
{
    unsigned row, col, i;
    for (row = 0; row < n; row++)
    {
        for (col = 0; col < m; col++)
        {
            gf *pa = &a[row * k];
            gf *pb = &b[col];
            gf acc = 0;
            for (i = 0; i < k; i++, pa++, pb += m)
                acc ^= gf_mul(*pa, *pb);
            c[row * m + col] = acc;
        }
    }
}

static void _invert_mat(gf *src, size_t k)
{
    gf c;
    size_t irow = 0, icol = 0, row, col, i, ix;
    unsigned *indxc = (unsigned *)malloc(k * sizeof(unsigned));
    unsigned *indxr = (unsigned *)malloc(k * sizeof(unsigned));
    unsigned *ipiv = (unsigned *)malloc(k * sizeof(unsigned));
    gf *id_row = NEW_GF_MATRIX(1, k);
    memset(id_row, '\0', k * sizeof(gf));
    for (i = 0; i < k; i++)
        ipiv[i] = 0;
    for (col = 0; col < k; col++)
    {
        gf *pivot_row;
        if (ipiv[col] != 1 && src[col * k + col] != 0)
        {
            irow = col;
            icol = col;
            goto found_piv;
        }
        for (row = 0; row < k; row++)
        {
            if (ipiv[row] != 1)
            {
                for (ix = 0; ix < k; ix++)
                {
                    if (ipiv[ix] == 0 && src[row * k + ix] != 0)
                    {
                        irow = row;
                        icol = ix;
                        goto found_piv;
                    }
                    else
                        assert(ipiv[ix] <= 1);
                }
            }
        }
    found_piv:
        ++(ipiv[icol]);
        if (irow != icol)
            for (ix = 0; ix < k; ix++)
                SWAP(src[irow * k + ix], src[icol * k + ix], gf);
        indxr[col] = irow;
        indxc[col] = icol;
        pivot_row = &src[icol * k];
        c = pivot_row[icol];
        assert(c != 0);
        if (c != 1)
        {
            c = inverse[c];
            pivot_row[icol] = 1;
            for (ix = 0; ix < k; ix++)
                pivot_row[ix] = gf_mul(c, pivot_row[ix]);
        }
        id_row[icol] = 1;
        if (memcmp(pivot_row, id_row, k * sizeof(gf)) != 0)
        {
            gf *p = src;
            for (ix = 0; ix < k; ix++, p += k)
            {
                if (ix != icol)
                {
                    c = p[icol];
                    p[icol] = 0;
                    addmul(p, pivot_row, c, k);
                }
            }
        }
        id_row[icol] = 0;
    }
    for (col = k; col > 0; col--)
        if (indxr[col - 1] != indxc[col - 1])
            for (row = 0; row < k; row++)
                SWAP(src[row * k + indxr[col - 1]], src[row * k + indxc[col - 1]], gf);
    free(indxc);
    free(indxr);
    free(ipiv);
    free(id_row);
}

void _invert_vdm(gf *src, unsigned k)
{
    unsigned i, j, row, col;
    gf *b, *c, *p, t, xx;
    if (k == 1)
        return;
    c = NEW_GF_MATRIX(1, k);
    b = NEW_GF_MATRIX(1, k);
    p = NEW_GF_MATRIX(1, k);
    for (j = 1, i = 0; i < k; i++, j += k)
    {
        c[i] = 0;
        p[i] = src[j];
    }
    c[k - 1] = p[0];
    for (i = 1; i < k; i++)
    {
        gf p_i = p[i];
        for (j = k - 1 - (i - 1); j < k - 1; j++)
            c[j] ^= gf_mul(p_i, c[j + 1]);
        c[k - 1] ^= p_i;
    }
    for (row = 0; row < k; row++)
    {
        xx = p[row];
        t = 1;
        b[k - 1] = 1;
        for (i = k - 1; i > 0; i--)
        {
            b[i - 1] = c[i] ^ gf_mul(xx, b[i]);
            t = gf_mul(xx, t) ^ b[i - 1];
        }
        for (col = 0; col < k; col++)
            src[col * k + row] = gf_mul(inverse[t], b[col]);
    }
    free(c);
    free(b);
    free(p);
}

static int fec_initialized = 0;
void fec_init(void)
{
    if (fec_initialized == 0)
    {
        generate_gf();
        _init_mul_table();
        fec_initialized = 1;
    }
}

#define FEC_MAGIC 0xFECC0DEC
void fec_free(fec_t *p)
{
    assert(p != NULL && p->magic == (((FEC_MAGIC ^ p->k) ^ p->n) ^ (unsigned long)(p->enc_matrix)));
    free(p->enc_matrix);
    free(p);
}

fec_t *fec_new(unsigned short k, unsigned short n)
{
    unsigned row, col;
    gf *p, *tmp_m;
    fec_t *retval;
    assert(k >= 1 && n >= 1 && n <= 256 && k <= n);
    if (fec_initialized == 0)
        return NULL;
    retval = (fec_t *)malloc(sizeof(fec_t));
    retval->k = k;
    retval->n = n;
    retval->enc_matrix = NEW_GF_MATRIX(n, k);
    retval->magic = ((FEC_MAGIC ^ k) ^ n) ^ (unsigned long)(retval->enc_matrix);
    tmp_m = NEW_GF_MATRIX(n, k);
    tmp_m[0] = 1;
    for (col = 1; col < k; col++)
        tmp_m[col] = 0;
    for (p = tmp_m + k, row = 0; row + 1 < n; row++, p += k)
        for (col = 0; col < k; col++)
            p[col] = gf_exp[modnn(row * col)];
    _invert_vdm(tmp_m, k);
    _matmul(tmp_m + k * k, tmp_m, retval->enc_matrix + k * k, n - k, k, k);
    memset(retval->enc_matrix, '\0', k * k * sizeof(gf));
    for (p = retval->enc_matrix, col = 0; col < k; col++, p += k + 1)
        *p = 1;
    free(tmp_m);
    return retval;
}

#ifndef STRIDE
#define STRIDE 8192
#endif

void fec_encode(const fec_t *code, const gf *restrict const *restrict const src, gf *restrict const *restrict const fecs, const unsigned *restrict const block_nums, size_t num_block_nums, size_t sz)
{
    unsigned char i, j;
    size_t k;
    unsigned fecnum;
    const gf *p;
    for (k = 0; k < sz; k += STRIDE)
    {
        size_t stride = ((sz - k) < STRIDE) ? (sz - k) : STRIDE;
        for (i = 0; i < num_block_nums; i++)
        {
            fecnum = block_nums[i];
            assert(fecnum >= code->k);
            memset(fecs[i] + k, 0, stride);
            p = &(code->enc_matrix[fecnum * code->k]);
            for (j = 0; j < code->k; j++)
                addmul(fecs[i] + k, src[j] + k, p[j], stride);
        }
    }
}

void build_decode_matrix_into_space(const fec_t *restrict const code, const unsigned *const restrict index, const unsigned k, gf *restrict const matrix)
{
    unsigned short i;
    gf *p;
    for (i = 0, p = matrix; i < k; i++, p += k)
    {
        if (index[i] < k)
        {
            memset(p, 0, k);
            p[i] = 1;
        }
        else
        {
            memcpy(p, &(code->enc_matrix[index[i] * code->k]), k);
        }
    }
    _invert_mat(matrix, k);
}

void fec_decode(const fec_t *code, const gf *restrict const *restrict const inpkts, gf *restrict const *restrict const outpkts, const unsigned *restrict const index, size_t sz)
{
    gf *m_dec = (gf *)alloca(code->k * code->k);
    unsigned char outix = 0;
    unsigned short row = 0, col = 0;
    build_decode_matrix_into_space(code, index, code->k, m_dec);
    for (row = 0; row < code->k; row++)
    {
        assert((index[row] >= code->k) || (index[row] == row));
        if (index[row] >= code->k)
        {
            memset(outpkts[outix], 0, sz);
            for (col = 0; col < code->k; col++)
                addmul(outpkts[outix], inpkts[col], m_dec[row * code->k + col], sz);
            outix++;
        }
    }
}
