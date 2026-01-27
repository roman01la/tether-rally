/*
 * OpenGL Renderer - Implementation for macOS
 *
 * Direct BGRA texture upload from VideoToolbox CVPixelBuffer
 */

#include "renderer.h"
#include <stdlib.h>
#include <stdio.h>

#ifdef __APPLE__
#define GL_SILENCE_DEPRECATION
#include <OpenGL/gl.h>
#include <OpenGL/OpenGL.h>
#include <CoreVideo/CoreVideo.h>
#endif

struct fpv_renderer
{
    /* OpenGL state */
    GLuint texture_y;
    int tex_width;
    int tex_height;

    /* Current frame */
    CVPixelBufferRef current_frame;
    bool has_frame;
    bool texture_valid; /* True if texture has been uploaded */

    /* Stats */
    fpv_renderer_stats_t stats;
};

/* Simple vertex/fragment shaders would be better, but using fixed-function for simplicity */

fpv_renderer_t *fpv_renderer_create(void)
{
    fpv_renderer_t *r = calloc(1, sizeof(fpv_renderer_t));
    if (!r)
        return NULL;

    /* Generate texture */
    glGenTextures(1, &r->texture_y);

    return r;
}

void fpv_renderer_destroy(fpv_renderer_t *r)
{
    if (!r)
        return;

    if (r->texture_y)
        glDeleteTextures(1, &r->texture_y);
    if (r->current_frame)
        CVPixelBufferRelease(r->current_frame);
    free(r);
}

void fpv_renderer_update_frame(fpv_renderer_t *r, fpv_decoded_frame_t *frame)
{
    if (!r || !frame || !frame->native_handle)
    {
        fprintf(stderr, "[RENDER] update_frame: invalid params r=%p frame=%p handle=%p\n",
                (void *)r, (void *)frame, frame ? frame->native_handle : NULL);
        return;
    }

    /* Release previous frame */
    if (r->current_frame)
    {
        CVPixelBufferRelease(r->current_frame);
        r->stats.frames_skipped++;
    }

    /* Take ownership */
    CVPixelBufferRef pixbuf = (CVPixelBufferRef)frame->native_handle;
    r->current_frame = pixbuf;
    frame->native_handle = NULL; /* Transferred */

    /* Upload texture immediately while pixbuf is valid */
    int width = (int)CVPixelBufferGetWidth(pixbuf);
    int height = (int)CVPixelBufferGetHeight(pixbuf);
    r->tex_width = width;
    r->tex_height = height;

    CVReturn lock_err = CVPixelBufferLockBaseAddress(pixbuf, kCVPixelBufferLock_ReadOnly);
    if (lock_err != kCVReturnSuccess)
    {
        fprintf(stderr, "[RENDER] Lock failed in update_frame: %d\n", (int)lock_err);
        r->texture_valid = false;
        return;
    }

    /* BGRA format - single plane, direct upload */
    void *base = CVPixelBufferGetBaseAddress(pixbuf);
    size_t stride = CVPixelBufferGetBytesPerRow(pixbuf);

    if (!base)
    {
        fprintf(stderr, "[RENDER] Base address is NULL in update_frame\n");
        CVPixelBufferUnlockBaseAddress(pixbuf, kCVPixelBufferLock_ReadOnly);
        r->texture_valid = false;
        return;
    }

    /* Clear any pending GL errors before upload */
    while (glGetError() != GL_NO_ERROR)
    {
    }

    /* Upload directly to texture - no conversion needed! */
    glBindTexture(GL_TEXTURE_2D, r->texture_y);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);

    /* Handle stride if it doesn't match width*4 */
    if (stride == (size_t)width * 4)
    {
        /* Use glTexSubImage2D if texture already exists with same dimensions */
        if (r->tex_width == width && r->tex_height == height && r->texture_valid)
        {
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, width, height,
                            GL_BGRA, GL_UNSIGNED_INT_8_8_8_8_REV, base);
        }
        else
        {
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0,
                         GL_BGRA, GL_UNSIGNED_INT_8_8_8_8_REV, base);
        }
    }
    else
    {
        /* Need to upload row by row due to stride mismatch */
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0,
                     GL_BGRA, GL_UNSIGNED_INT_8_8_8_8_REV, NULL);
        for (int row = 0; row < height; row++)
        {
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, row, width, 1,
                            GL_BGRA, GL_UNSIGNED_INT_8_8_8_8_REV,
                            (uint8_t *)base + row * stride);
        }
    }

    CVPixelBufferUnlockBaseAddress(pixbuf, kCVPixelBufferLock_ReadOnly);

    GLenum gl_err = glGetError();
    if (gl_err != GL_NO_ERROR)
    {
        fprintf(stderr, "[RENDER] GL error after texture upload: 0x%x\n", gl_err);
        r->texture_valid = false;
    }
    else
    {
        r->texture_valid = true;
        r->has_frame = true;
    }
}

void fpv_renderer_draw(fpv_renderer_t *r, int viewport_width, int viewport_height)
{
    if (!r || !r->texture_valid)
    {
        /* Draw blue - no valid texture yet */
        glClearColor(0.0f, 0.0f, 0.3f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        return;
    }

    int width = r->tex_width;
    int height = r->tex_height;

    /* Setup viewport and projection */
    glViewport(0, 0, viewport_width, viewport_height);

    glMatrixMode(GL_PROJECTION);
    glLoadIdentity();
    glOrtho(-1, 1, -1, 1, -1, 1);

    glMatrixMode(GL_MODELVIEW);
    glLoadIdentity();

    /* Calculate aspect ratio preserving quad */
    float video_aspect = (float)width / height;
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

    /* Clear and draw */
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    /* Draw textured quad with video frame */
    glEnable(GL_TEXTURE_2D);
    glBindTexture(GL_TEXTURE_2D, r->texture_y);

    /* Set texture environment to replace vertex color with texture */
    glTexEnvi(GL_TEXTURE_ENV, GL_TEXTURE_ENV_MODE, GL_REPLACE);

    glColor3f(1.0f, 1.0f, 1.0f);
    glBegin(GL_QUADS);
    glTexCoord2f(0, 0);
    glVertex2f(-quad_w, quad_h);
    glTexCoord2f(1, 0);
    glVertex2f(quad_w, quad_h);
    glTexCoord2f(1, 1);
    glVertex2f(quad_w, -quad_h);
    glTexCoord2f(0, 1);
    glVertex2f(-quad_w, -quad_h);
    glEnd();

    glDisable(GL_TEXTURE_2D);

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
