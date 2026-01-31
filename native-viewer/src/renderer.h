/**
 * OpenGL Renderer for video frames
 */

#pragma once

#include <cstdint>

class Renderer
{
public:
    Renderer();
    ~Renderer();

    bool initialize();
    void resize(int width, int height);
    void uploadFrame(const uint8_t *rgb_data, int width, int height);
    void render(int viewport_width, int viewport_height);

private:
    unsigned int shaderProgram_ = 0;
    unsigned int vao_ = 0;
    unsigned int vbo_ = 0;
    unsigned int ebo_ = 0;
    unsigned int texture_ = 0;

    int textureWidth_ = 0;
    int textureHeight_ = 0;
};
