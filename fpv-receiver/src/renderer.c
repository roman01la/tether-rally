/*
 * OpenGL Renderer - Implementation for macOS
 *
 * Renders NV12 (YUV 4:2:0 bi-planar) video frames with GPU-side YUV→RGB conversion.
 * Uses IOSurface-backed textures for zero-copy from VideoToolbox decoder.
 *
 * NV12 format uses 1.5 bytes/pixel vs BGRA's 4 bytes/pixel = 2.67x less bandwidth.
 */

#include "renderer.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <mach/mach_time.h>

#ifdef __APPLE__
#define GL_SILENCE_DEPRECATION
#include <OpenGL/gl.h>
#include <OpenGL/glext.h>
#include <OpenGL/OpenGL.h>
#include <OpenGL/CGLCurrent.h>
#include <OpenGL/CGLIOSurface.h>
#include <CoreVideo/CoreVideo.h>
#include <IOSurface/IOSurface.h>
#endif

/* Exponential moving average alpha (0.1 = smooth, 0.5 = responsive) */
#define EMA_ALPHA 0.2

static uint64_t get_time_us(void)
{
    static mach_timebase_info_data_t timebase_info;
    if (timebase_info.denom == 0)
    {
        mach_timebase_info(&timebase_info);
    }
    uint64_t mach_time = mach_absolute_time();
    return (mach_time * timebase_info.numer / timebase_info.denom) / 1000;
}

/* YUV→RGB conversion shader (BT.601 video range) */
static const char *vertex_shader_src =
    "#version 120\n"
    "attribute vec2 position;\n"
    "attribute vec2 texcoord;\n"
    "varying vec2 v_texcoord;\n"
    "void main() {\n"
    "    gl_Position = vec4(position, 0.0, 1.0);\n"
    "    v_texcoord = texcoord;\n"
    "}\n";

static const char *fragment_shader_src =
    "#version 120\n"
    "#extension GL_ARB_texture_rectangle : enable\n"
    "uniform sampler2DRect tex_y;\n"
    "uniform sampler2DRect tex_uv;\n"
    "uniform vec2 tex_size;\n"
    "varying vec2 v_texcoord;\n"
    "void main() {\n"
    "    vec2 tc = v_texcoord * tex_size;\n"
    "    float y = texture2DRect(tex_y, tc).r;\n"
    "    vec2 uv = texture2DRect(tex_uv, tc * 0.5).rg;\n"
    "    /* BT.601 video range YUV→RGB */\n"
    "    y = (y - 0.0625) * 1.164;\n"
    "    float u = uv.r - 0.5;\n"
    "    float v = uv.g - 0.5;\n"
    "    float r = y + 1.596 * v;\n"
    "    float g = y - 0.391 * u - 0.813 * v;\n"
    "    float b = y + 2.018 * u;\n"
    "    gl_FragColor = vec4(r, g, b, 1.0);\n"
    "}\n";

struct fpv_renderer
{
    /* Shader program */
    GLuint program;
    GLuint vertex_shader;
    GLuint fragment_shader;

    /* Shader uniforms/attributes */
    GLint attr_position;
    GLint attr_texcoord;
    GLint uniform_tex_y;
    GLint uniform_tex_uv;
    GLint uniform_tex_size;

    /* Textures for Y and UV planes */
    GLuint tex_y;
    GLuint tex_uv;

    /* Frame dimensions */
    int tex_width;
    int tex_height;

    /* Current frame (retained) */
    CVPixelBufferRef current_frame;
    bool has_frame;
    bool texture_valid;

    /* Jitter tracking */
    uint64_t last_frame_time_us;
    bool have_last_frame_time;

    /* Stats */
    fpv_renderer_stats_t stats;
};

static GLuint compile_shader(GLenum type, const char *source)
{
    GLuint shader = glCreateShader(type);
    glShaderSource(shader, 1, &source, NULL);
    glCompileShader(shader);

    GLint status;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &status);
    if (status != GL_TRUE)
    {
        char log[512];
        glGetShaderInfoLog(shader, sizeof(log), NULL, log);
        fprintf(stderr, "[RENDER] Shader compile error: %s\n", log);
        glDeleteShader(shader);
        return 0;
    }
    return shader;
}

static int create_shader_program(fpv_renderer_t *r)
{
    r->vertex_shader = compile_shader(GL_VERTEX_SHADER, vertex_shader_src);
    if (!r->vertex_shader)
        return -1;

    r->fragment_shader = compile_shader(GL_FRAGMENT_SHADER, fragment_shader_src);
    if (!r->fragment_shader)
    {
        glDeleteShader(r->vertex_shader);
        return -1;
    }

    r->program = glCreateProgram();
    glAttachShader(r->program, r->vertex_shader);
    glAttachShader(r->program, r->fragment_shader);
    glLinkProgram(r->program);

    GLint status;
    glGetProgramiv(r->program, GL_LINK_STATUS, &status);
    if (status != GL_TRUE)
    {
        char log[512];
        glGetProgramInfoLog(r->program, sizeof(log), NULL, log);
        fprintf(stderr, "[RENDER] Shader link error: %s\n", log);
        glDeleteProgram(r->program);
        glDeleteShader(r->vertex_shader);
        glDeleteShader(r->fragment_shader);
        return -1;
    }

    /* Get attribute/uniform locations */
    r->attr_position = glGetAttribLocation(r->program, "position");
    r->attr_texcoord = glGetAttribLocation(r->program, "texcoord");
    r->uniform_tex_y = glGetUniformLocation(r->program, "tex_y");
    r->uniform_tex_uv = glGetUniformLocation(r->program, "tex_uv");
    r->uniform_tex_size = glGetUniformLocation(r->program, "tex_size");

    return 0;
}

fpv_renderer_t *fpv_renderer_create(void)
{
    fpv_renderer_t *r = calloc(1, sizeof(fpv_renderer_t));
    if (!r)
        return NULL;

    /* Create shader program for YUV→RGB conversion */
    if (create_shader_program(r) != 0)
    {
        fprintf(stderr, "[RENDER] Failed to create shader program\n");
        free(r);
        return NULL;
    }

    /* Create textures for Y and UV planes */
    glGenTextures(1, &r->tex_y);
    glGenTextures(1, &r->tex_uv);

    /* Initialize jitter tracking */
    r->have_last_frame_time = false;
    r->stats.target_fps = 60.0;  /* Default target FPS */

    return r;
}

void fpv_renderer_destroy(fpv_renderer_t *r)
{
    if (!r)
        return;

    if (r->tex_y)
        glDeleteTextures(1, &r->tex_y);
    if (r->tex_uv)
        glDeleteTextures(1, &r->tex_uv);
    if (r->program)
    {
        glDeleteProgram(r->program);
        glDeleteShader(r->vertex_shader);
        glDeleteShader(r->fragment_shader);
    }
    if (r->current_frame)
        CVPixelBufferRelease(r->current_frame);
    free(r);
}

/* Internal implementation with timing support */
static void update_frame_internal(fpv_renderer_t *r, fpv_decoded_frame_t *frame,
                                  const uint64_t *timing_us)
{
    if (!r || !frame || !frame->native_handle)
    {
        return;
    }

    uint64_t upload_start = get_time_us();

    /* Track jitter - measure time between frame arrivals */
    if (r->have_last_frame_time)
    {
        double interval_us = (double)(upload_start - r->last_frame_time_us);
        double target_interval_us = 1000000.0 / r->stats.target_fps;  /* e.g., 16667us for 60fps */
        double jitter_us = (interval_us > target_interval_us) 
                           ? (interval_us - target_interval_us) 
                           : (target_interval_us - interval_us);

        /* Update exponential moving averages for jitter */
        if (r->stats.avg_interval_us == 0)
        {
            r->stats.avg_interval_us = interval_us;
            r->stats.avg_jitter_us = jitter_us;
        }
        else
        {
            r->stats.avg_interval_us = EMA_ALPHA * interval_us + (1.0 - EMA_ALPHA) * r->stats.avg_interval_us;
            r->stats.avg_jitter_us = EMA_ALPHA * jitter_us + (1.0 - EMA_ALPHA) * r->stats.avg_jitter_us;
        }
    }
    r->last_frame_time_us = upload_start;
    r->have_last_frame_time = true;

    /* Release previous frame */
    if (r->current_frame)
    {
        CVPixelBufferRelease(r->current_frame);
        r->stats.frames_skipped++;
    }

    /* Take ownership of new frame */
    CVPixelBufferRef pixbuf = (CVPixelBufferRef)frame->native_handle;
    r->current_frame = pixbuf;
    frame->native_handle = NULL;

    r->tex_width = (int)CVPixelBufferGetWidth(pixbuf);
    r->tex_height = (int)CVPixelBufferGetHeight(pixbuf);

    /* Get IOSurface for zero-copy texture binding */
    IOSurfaceRef surface = CVPixelBufferGetIOSurface(pixbuf);
    if (!surface)
    {
        fprintf(stderr, "[RENDER] No IOSurface backing\n");
        r->texture_valid = false;
        return;
    }

    CGLContextObj cgl_ctx = CGLGetCurrentContext();

    /* Bind Y plane (plane 0) - full resolution, 8-bit luminance */
    glBindTexture(GL_TEXTURE_RECTANGLE_ARB, r->tex_y);
    CGLError cgl_err = CGLTexImageIOSurface2D(
        cgl_ctx, GL_TEXTURE_RECTANGLE_ARB,
        GL_R8, r->tex_width, r->tex_height,
        GL_RED, GL_UNSIGNED_BYTE, surface, 0);
    if (cgl_err != kCGLNoError)
    {
        fprintf(stderr, "[RENDER] Failed to bind Y plane: %d\n", cgl_err);
        r->texture_valid = false;
        return;
    }
    glTexParameteri(GL_TEXTURE_RECTANGLE_ARB, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_RECTANGLE_ARB, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_RECTANGLE_ARB, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_RECTANGLE_ARB, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);

    /* Bind UV plane (plane 1) - half resolution, interleaved CbCr */
    glBindTexture(GL_TEXTURE_RECTANGLE_ARB, r->tex_uv);
    cgl_err = CGLTexImageIOSurface2D(
        cgl_ctx, GL_TEXTURE_RECTANGLE_ARB,
        GL_RG8, r->tex_width / 2, r->tex_height / 2,
        GL_RG, GL_UNSIGNED_BYTE, surface, 1);
    if (cgl_err != kCGLNoError)
    {
        fprintf(stderr, "[RENDER] Failed to bind UV plane: %d\n", cgl_err);
        r->texture_valid = false;
        return;
    }
    glTexParameteri(GL_TEXTURE_RECTANGLE_ARB, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_RECTANGLE_ARB, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_RECTANGLE_ARB, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_RECTANGLE_ARB, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);

    r->texture_valid = true;
    r->has_frame = true;

    /* Update timing statistics if provided */
    if (timing_us)
    {
        uint64_t upload_end = get_time_us();
        uint64_t first_packet = timing_us[0];
        uint64_t assembly_complete = timing_us[1];
        uint64_t decode_complete = timing_us[2];

        double assembly_us = (double)(assembly_complete - first_packet);
        double decode_us = (double)(decode_complete - assembly_complete);
        double upload_us = (double)(upload_end - decode_complete);
        double total_us = (double)(upload_end - first_packet);

        /* Update exponential moving averages */
        if (r->stats.avg_total_us == 0)
        {
            /* First sample */
            r->stats.avg_assembly_us = assembly_us;
            r->stats.avg_decode_us = decode_us;
            r->stats.avg_upload_us = upload_us;
            r->stats.avg_total_us = total_us;
        }
        else
        {
            r->stats.avg_assembly_us = EMA_ALPHA * assembly_us + (1.0 - EMA_ALPHA) * r->stats.avg_assembly_us;
            r->stats.avg_decode_us = EMA_ALPHA * decode_us + (1.0 - EMA_ALPHA) * r->stats.avg_decode_us;
            r->stats.avg_upload_us = EMA_ALPHA * upload_us + (1.0 - EMA_ALPHA) * r->stats.avg_upload_us;
            r->stats.avg_total_us = EMA_ALPHA * total_us + (1.0 - EMA_ALPHA) * r->stats.avg_total_us;
        }
    }
}

void fpv_renderer_update_frame(fpv_renderer_t *r, fpv_decoded_frame_t *frame)
{
    update_frame_internal(r, frame, NULL);
}

void fpv_renderer_update_frame_with_timing(fpv_renderer_t *r, fpv_decoded_frame_t *frame,
                                           const uint64_t *timing_us)
{
    update_frame_internal(r, frame, timing_us);
}

void fpv_renderer_draw(fpv_renderer_t *r, int viewport_width, int viewport_height)
{
    if (!r || !r->texture_valid)
    {
        glClearColor(0.0f, 0.0f, 0.3f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        return;
    }

    /* Calculate aspect-correct quad */
    float video_aspect = (float)r->tex_width / r->tex_height;
    float viewport_aspect = (float)viewport_width / viewport_height;
    float quad_w = 1.0f, quad_h = 1.0f;

    if (video_aspect > viewport_aspect)
    {
        quad_h = viewport_aspect / video_aspect;
    }
    else
    {
        quad_w = video_aspect / viewport_aspect;
    }

    /* Vertex data: position (x,y) + texcoord (s,t) */
    /* Flip V coord for correct orientation */
    float vertices[] = {
        -quad_w,
        quad_h,
        0.0f,
        1.0f, /* top-left */
        quad_w,
        quad_h,
        1.0f,
        1.0f, /* top-right */
        quad_w,
        -quad_h,
        1.0f,
        0.0f, /* bottom-right */
        -quad_w,
        -quad_h,
        0.0f,
        0.0f, /* bottom-left */
    };

    glViewport(0, 0, viewport_width, viewport_height);
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    glUseProgram(r->program);

    /* Bind textures */
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_RECTANGLE_ARB, r->tex_y);
    glUniform1i(r->uniform_tex_y, 0);

    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_RECTANGLE_ARB, r->tex_uv);
    glUniform1i(r->uniform_tex_uv, 1);

    glUniform2f(r->uniform_tex_size, (float)r->tex_width, (float)r->tex_height);

    /* Draw quad */
    glEnableVertexAttribArray(r->attr_position);
    glEnableVertexAttribArray(r->attr_texcoord);

    glVertexAttribPointer(r->attr_position, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), vertices);
    glVertexAttribPointer(r->attr_texcoord, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), vertices + 2);

    glDrawArrays(GL_TRIANGLE_FAN, 0, 4);

    glDisableVertexAttribArray(r->attr_position);
    glDisableVertexAttribArray(r->attr_texcoord);

    glUseProgram(0);

    r->stats.frames_rendered++;
}

bool fpv_renderer_has_frame(fpv_renderer_t *r)
{
    return r && r->has_frame;
}

fpv_renderer_stats_t fpv_renderer_get_stats(fpv_renderer_t *r)
{
    if (!r)
    {
        fpv_renderer_stats_t empty = {0};
        return empty;
    }
    return r->stats;
}
