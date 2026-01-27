/*
 * VideoToolbox H.264 Decoder for macOS
 *
 * Uses VTDecompressionSession for hardware-accelerated H.264 decode.
 * Outputs CVPixelBufferRef for OpenGL texture upload.
 */

#include "decoder.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#include <VideoToolbox/VideoToolbox.h>
#include <CoreVideo/CoreVideo.h>

struct fpv_decoder
{
    VTDecompressionSessionRef session;
    CMVideoFormatDescriptionRef format_desc;

    /* Cached SPS/PPS for session creation */
    uint8_t *sps;
    size_t sps_len;
    uint8_t *pps;
    size_t pps_len;

    /* Latest decoded frame (single slot per spec) */
    CVPixelBufferRef latest_pixbuf;

    /* State */
    bool needs_keyframe;

    /* Stats */
    fpv_decoder_stats_t stats;
};

/* NAL unit types */
#define NAL_TYPE_SLICE 1
#define NAL_TYPE_IDR 5
#define NAL_TYPE_SEI 6
#define NAL_TYPE_SPS 7
#define NAL_TYPE_PPS 8

/* Find start code in Annex B data */
static const uint8_t *find_start_code(const uint8_t *data, size_t len, size_t *sc_len)
{
    for (size_t i = 0; i + 2 < len; i++)
    {
        if (data[i] == 0 && data[i + 1] == 0)
        {
            if (data[i + 2] == 1)
            {
                *sc_len = 3;
                return data + i;
            }
            if (i + 3 < len && data[i + 2] == 0 && data[i + 3] == 1)
            {
                *sc_len = 4;
                return data + i;
            }
        }
    }
    return NULL;
}

/* Parse NAL units from Annex B data */
typedef struct
{
    const uint8_t *data;
    size_t len;
    uint8_t type;
} nal_unit_t;

static int parse_nals(const uint8_t *data, size_t len, nal_unit_t *nals, int max_nals)
{
    int count = 0;
    const uint8_t *p = data;
    const uint8_t *end = data + len;

    while (p < end && count < max_nals)
    {
        size_t sc_len;
        const uint8_t *sc = find_start_code(p, end - p, &sc_len);
        if (!sc)
            break;

        const uint8_t *nal_start = sc + sc_len;
        if (nal_start >= end)
            break;

        /* Find next start code or end */
        const uint8_t *next_sc = NULL;
        size_t next_sc_len;
        for (const uint8_t *q = nal_start; q < end - 2; q++)
        {
            next_sc = find_start_code(q, end - q, &next_sc_len);
            if (next_sc)
                break;
        }

        const uint8_t *nal_end = next_sc ? next_sc : end;

        nals[count].data = nal_start;
        nals[count].len = nal_end - nal_start;
        nals[count].type = nal_start[0] & 0x1F;
        count++;

        p = nal_end;
    }

    return count;
}

/* VTDecompressionSession callback */
static void decode_callback(void *decompressionOutputRefCon,
                            void *sourceFrameRefCon,
                            OSStatus status,
                            VTDecodeInfoFlags infoFlags,
                            CVImageBufferRef imageBuffer,
                            CMTime presentationTimeStamp,
                            CMTime presentationDuration)
{
    fpv_decoder_t *dec = (fpv_decoder_t *)decompressionOutputRefCon;

    if (status != noErr || !imageBuffer)
    {
        fprintf(stderr, "VT decode error: %d\n", (int)status);
        dec->stats.decode_errors++;
        dec->needs_keyframe = true;
        return;
    }

    /* Release previous frame, keep latest (single slot) */
    if (dec->latest_pixbuf)
    {
        CVPixelBufferRelease(dec->latest_pixbuf);
    }
    dec->latest_pixbuf = (CVPixelBufferRef)CVPixelBufferRetain(imageBuffer);
    dec->stats.frames_decoded++;
}

/* Create or recreate session with new SPS/PPS */
static int create_session(fpv_decoder_t *dec)
{
    if (!dec->sps || !dec->pps)
    {
        return -1;
    }

    /* Release old session */
    if (dec->session)
    {
        VTDecompressionSessionInvalidate(dec->session);
        CFRelease(dec->session);
        dec->session = NULL;
    }
    if (dec->format_desc)
    {
        CFRelease(dec->format_desc);
        dec->format_desc = NULL;
    }

    /* Create format description from SPS/PPS */
    const uint8_t *param_sets[] = {dec->sps, dec->pps};
    const size_t param_sizes[] = {dec->sps_len, dec->pps_len};

    OSStatus status = CMVideoFormatDescriptionCreateFromH264ParameterSets(
        kCFAllocatorDefault,
        2,
        param_sets,
        param_sizes,
        4, /* NAL length size */
        &dec->format_desc);

    if (status != noErr)
    {
        fprintf(stderr, "Failed to create format description: %d\n", (int)status);
        return -1;
    }

    /* Output pixel format - NV12 (native H.264 YUV 4:2:0 bi-planar)
     * This is 2.67x smaller than BGRA and avoids GPU format conversion.
     * The renderer handles YUV→RGB conversion via shader. */
    CFMutableDictionaryRef dest_attrs = CFDictionaryCreateMutable(
        kCFAllocatorDefault, 0,
        &kCFTypeDictionaryKeyCallBacks,
        &kCFTypeDictionaryValueCallBacks);

    int pixel_format = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange;
    CFNumberRef format_num = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &pixel_format);
    CFDictionarySetValue(dest_attrs, kCVPixelBufferPixelFormatTypeKey, format_num);
    CFRelease(format_num);

    /* Enable OpenGL compatibility for IOSurface-backed buffers */
    CFDictionarySetValue(dest_attrs, kCVPixelBufferOpenGLCompatibilityKey, kCFBooleanTrue);
    CFDictionarySetValue(dest_attrs, kCVPixelBufferIOSurfacePropertiesKey,
                         CFDictionaryCreate(kCFAllocatorDefault, NULL, NULL, 0,
                                            &kCFTypeDictionaryKeyCallBacks,
                                            &kCFTypeDictionaryValueCallBacks));

    /* Create decoder specification - REQUIRE hardware acceleration.
     * This eliminates the 75ms decode spikes caused by software fallback. */
    CFMutableDictionaryRef decoder_spec = CFDictionaryCreateMutable(
        kCFAllocatorDefault, 0,
        &kCFTypeDictionaryKeyCallBacks,
        &kCFTypeDictionaryValueCallBacks);

    /* RequireHardwareAcceleratedVideoDecoder ensures we fail fast if HW unavailable
     * rather than silently falling back to slow software decode */
    CFDictionarySetValue(decoder_spec,
                         kVTVideoDecoderSpecification_RequireHardwareAcceleratedVideoDecoder,
                         kCFBooleanTrue);

    /* Create decompression session */
    VTDecompressionOutputCallbackRecord callback = {
        .decompressionOutputCallback = decode_callback,
        .decompressionOutputRefCon = dec};

    status = VTDecompressionSessionCreate(
        kCFAllocatorDefault,
        dec->format_desc,
        decoder_spec,
        dest_attrs,
        &callback,
        &dec->session);

    CFRelease(decoder_spec);
    CFRelease(dest_attrs);

    if (status != noErr)
    {
        fprintf(stderr, "Failed to create decompression session: %d (HW decode required)\n", (int)status);
        return -1;
    }

    /* Request real-time decoding - prioritize low latency over efficiency */
    VTSessionSetProperty(dec->session,
                         kVTDecompressionPropertyKey_RealTime,
                         kCFBooleanTrue);

    /* Disable power efficiency mode - we want maximum performance */
    VTSessionSetProperty(dec->session,
                         kVTDecompressionPropertyKey_MaximizePowerEfficiency,
                         kCFBooleanFalse);

    /* Disable frame reordering - baseline profile has no B-frames */
    VTSessionSetProperty(dec->session,
                         kVTDecompressionPropertyKey_FieldMode,
                         kVTDecompressionProperty_FieldMode_DeinterlaceFields);

    return 0;
}

fpv_decoder_t *fpv_decoder_create(void)
{
    fpv_decoder_t *dec = calloc(1, sizeof(fpv_decoder_t));
    if (!dec)
        return NULL;

    dec->needs_keyframe = true;
    return dec;
}

void fpv_decoder_destroy(fpv_decoder_t *dec)
{
    if (!dec)
        return;

    if (dec->session)
    {
        VTDecompressionSessionInvalidate(dec->session);
        CFRelease(dec->session);
    }
    if (dec->format_desc)
    {
        CFRelease(dec->format_desc);
    }
    if (dec->latest_pixbuf)
    {
        CVPixelBufferRelease(dec->latest_pixbuf);
    }
    free(dec->sps);
    free(dec->pps);
    free(dec);
}

int fpv_decoder_decode(fpv_decoder_t *dec,
                       const uint8_t *data, size_t len,
                       uint32_t frame_id, uint32_t ts_ms,
                       bool is_keyframe,
                       fpv_decoded_frame_t *out_frame)
{
    /* Parse NAL units */
    nal_unit_t nals[32];
    int nal_count = parse_nals(data, len, nals, 32);

    if (nal_count == 0)
    {
        /* Debug: print first few bytes */
        fprintf(stderr, "No NALs found in %zu bytes. First 16: ", len);
        for (size_t i = 0; i < 16 && i < len; i++)
        {
            fprintf(stderr, "%02x ", data[i]);
        }
        fprintf(stderr, "\n");
        return -1;
    }

    /* Extract SPS/PPS if present */
    for (int i = 0; i < nal_count; i++)
    {
        if (nals[i].type == NAL_TYPE_SPS && nals[i].len > 0)
        {
            free(dec->sps);
            dec->sps = malloc(nals[i].len);
            if (dec->sps)
            {
                memcpy(dec->sps, nals[i].data, nals[i].len);
                dec->sps_len = nals[i].len;
            }
        }
        else if (nals[i].type == NAL_TYPE_PPS && nals[i].len > 0)
        {
            free(dec->pps);
            dec->pps = malloc(nals[i].len);
            if (dec->pps)
            {
                memcpy(dec->pps, nals[i].data, nals[i].len);
                dec->pps_len = nals[i].len;
            }
        }
    }

    /* Create/recreate session if needed */
    if (!dec->session && dec->sps && dec->pps)
    {
        if (create_session(dec) != 0)
        {
            return -2;
        }
        dec->needs_keyframe = false;
    }

    if (!dec->session)
    {
        return -3; /* No session yet, need SPS/PPS */
    }

    /* If we need a keyframe and this isn't one, skip */
    if (dec->needs_keyframe && !is_keyframe)
    {
        return -4;
    }

    /* Convert Annex B to AVCC format for VideoToolbox */
    /* Allocate buffer for AVCC data */
    uint8_t *avcc_buf = malloc(len + nal_count * 4);
    if (!avcc_buf)
        return -5;

    size_t avcc_len = 0;
    for (int i = 0; i < nal_count; i++)
    {
        /* Skip SPS/PPS - they're in the format description */
        if (nals[i].type == NAL_TYPE_SPS || nals[i].type == NAL_TYPE_PPS)
        {
            continue;
        }

        /* Write 4-byte length prefix (big-endian) */
        uint32_t nal_len = (uint32_t)nals[i].len;
        avcc_buf[avcc_len + 0] = (nal_len >> 24) & 0xFF;
        avcc_buf[avcc_len + 1] = (nal_len >> 16) & 0xFF;
        avcc_buf[avcc_len + 2] = (nal_len >> 8) & 0xFF;
        avcc_buf[avcc_len + 3] = nal_len & 0xFF;
        memcpy(avcc_buf + avcc_len + 4, nals[i].data, nals[i].len);
        avcc_len += 4 + nals[i].len;
    }

    if (avcc_len == 0)
    {
        free(avcc_buf);
        return 1; /* Only had SPS/PPS, no video data - not an error but no frame */
    }

    /* Create CMBlockBuffer */
    CMBlockBufferRef block_buf = NULL;
    OSStatus status = CMBlockBufferCreateWithMemoryBlock(
        kCFAllocatorDefault,
        avcc_buf,
        avcc_len,
        kCFAllocatorDefault,
        NULL,
        0,
        avcc_len,
        0,
        &block_buf);

    if (status != noErr)
    {
        free(avcc_buf);
        return -6;
    }

    /* Create CMSampleBuffer */
    CMSampleBufferRef sample_buf = NULL;
    const size_t sample_sizes[] = {avcc_len};

    status = CMSampleBufferCreateReady(
        kCFAllocatorDefault,
        block_buf,
        dec->format_desc,
        1,    /* sample count */
        0,    /* timing info count */
        NULL, /* timing info */
        1,    /* sample size count */
        sample_sizes,
        &sample_buf);

    CFRelease(block_buf);

    if (status != noErr)
    {
        return -7;
    }

    /* Decode - use synchronous decoding to avoid any latency/ordering issues */
    VTDecodeFrameFlags flags = kVTDecodeFrame_1xRealTimePlayback; /* No async flag = sync decode */
    VTDecodeInfoFlags info_flags;

    status = VTDecompressionSessionDecodeFrame(
        dec->session,
        sample_buf,
        flags,
        NULL,
        &info_flags);

    CFRelease(sample_buf);

    if (status != noErr)
    {
        dec->stats.decode_errors++;
        dec->needs_keyframe = true;
        return -8;
    }

    /* No need to wait - synchronous decode completes immediately */

    /* Return latest decoded frame */
    if (dec->latest_pixbuf)
    {
        out_frame->native_handle = dec->latest_pixbuf;
        out_frame->width = (int)CVPixelBufferGetWidth(dec->latest_pixbuf);
        out_frame->height = (int)CVPixelBufferGetHeight(dec->latest_pixbuf);
        out_frame->frame_id = frame_id;
        out_frame->ts_ms = ts_ms;
        dec->latest_pixbuf = NULL; /* Transfer ownership */

        if (is_keyframe)
        {
            dec->stats.keyframes_decoded++;
            dec->needs_keyframe = false;
        }

        return 0;
    }

    return -9;
}

void fpv_decoder_release_frame(fpv_decoded_frame_t *frame)
{
    if (frame && frame->native_handle)
    {
        CVPixelBufferRelease((CVPixelBufferRef)frame->native_handle);
        frame->native_handle = NULL;
    }
}

bool fpv_decoder_needs_keyframe(fpv_decoder_t *dec)
{
    return dec->needs_keyframe;
}

void fpv_decoder_reset(fpv_decoder_t *dec)
{
    if (dec->session)
    {
        VTDecompressionSessionInvalidate(dec->session);
        CFRelease(dec->session);
        dec->session = NULL;
    }
    if (dec->latest_pixbuf)
    {
        CVPixelBufferRelease(dec->latest_pixbuf);
        dec->latest_pixbuf = NULL;
    }
    dec->needs_keyframe = true;
}

fpv_decoder_stats_t fpv_decoder_get_stats(fpv_decoder_t *dec)
{
    return dec->stats;
}
