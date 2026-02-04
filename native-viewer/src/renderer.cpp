#include "renderer.h"

#ifdef __APPLE__
#ifndef GL_SILENCE_DEPRECATION
#define GL_SILENCE_DEPRECATION
#endif
#include <OpenGL/gl3.h>
#else
#include <GL/gl.h>
#endif

#include <iostream>

// Vertex shader - simple passthrough
static const char *vertexShaderSource = R"(
#version 330 core
layout (location = 0) in vec2 aPos;
layout (location = 1) in vec2 aTexCoord;
out vec2 TexCoord;
void main() {
    gl_Position = vec4(aPos, 0.0, 1.0);
    TexCoord = aTexCoord;
}
)";

// Fragment shader - texture sampling
static const char *fragmentShaderSource = R"(
#version 330 core
in vec2 TexCoord;
out vec4 FragColor;
uniform sampler2D videoTexture;
void main() {
    FragColor = texture(videoTexture, TexCoord);
}
)";

Renderer::Renderer() = default;

Renderer::~Renderer()
{
    if (texture_)
        glDeleteTextures(1, &texture_);
    if (vao_)
        glDeleteVertexArrays(1, &vao_);
    if (vbo_)
        glDeleteBuffers(1, &vbo_);
    if (ebo_)
        glDeleteBuffers(1, &ebo_);
    if (shaderProgram_)
        glDeleteProgram(shaderProgram_);
}

bool Renderer::initialize()
{
    // Compile vertex shader
    unsigned int vertexShader = glCreateShader(GL_VERTEX_SHADER);
    glShaderSource(vertexShader, 1, &vertexShaderSource, nullptr);
    glCompileShader(vertexShader);

    int success;
    glGetShaderiv(vertexShader, GL_COMPILE_STATUS, &success);
    if (!success)
    {
        char infoLog[512];
        glGetShaderInfoLog(vertexShader, 512, nullptr, infoLog);
        std::cerr << "Vertex shader compilation failed: " << infoLog << std::endl;
        return false;
    }

    // Compile fragment shader
    unsigned int fragmentShader = glCreateShader(GL_FRAGMENT_SHADER);
    glShaderSource(fragmentShader, 1, &fragmentShaderSource, nullptr);
    glCompileShader(fragmentShader);

    glGetShaderiv(fragmentShader, GL_COMPILE_STATUS, &success);
    if (!success)
    {
        char infoLog[512];
        glGetShaderInfoLog(fragmentShader, 512, nullptr, infoLog);
        std::cerr << "Fragment shader compilation failed: " << infoLog << std::endl;
        return false;
    }

    // Link program
    shaderProgram_ = glCreateProgram();
    glAttachShader(shaderProgram_, vertexShader);
    glAttachShader(shaderProgram_, fragmentShader);
    glLinkProgram(shaderProgram_);

    glGetProgramiv(shaderProgram_, GL_LINK_STATUS, &success);
    if (!success)
    {
        char infoLog[512];
        glGetProgramInfoLog(shaderProgram_, 512, nullptr, infoLog);
        std::cerr << "Shader program linking failed: " << infoLog << std::endl;
        return false;
    }

    glDeleteShader(vertexShader);
    glDeleteShader(fragmentShader);

    // Fullscreen quad vertices (position + texcoord)
    float vertices[] = {
        // positions    // texture coords (flipped Y for video)
        -1.0f,
        1.0f,
        0.0f,
        0.0f, // top-left
        1.0f,
        1.0f,
        1.0f,
        0.0f, // top-right
        1.0f,
        -1.0f,
        1.0f,
        1.0f, // bottom-right
        -1.0f,
        -1.0f,
        0.0f,
        1.0f, // bottom-left
    };

    unsigned int indices[] = {
        0, 1, 2,
        2, 3, 0};

    glGenVertexArrays(1, &vao_);
    glGenBuffers(1, &vbo_);
    glGenBuffers(1, &ebo_);

    glBindVertexArray(vao_);

    glBindBuffer(GL_ARRAY_BUFFER, vbo_);
    glBufferData(GL_ARRAY_BUFFER, sizeof(vertices), vertices, GL_STATIC_DRAW);

    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, ebo_);
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, sizeof(indices), indices, GL_STATIC_DRAW);

    // Position attribute
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), (void *)0);
    glEnableVertexAttribArray(0);

    // Texture coord attribute
    glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float),
                          (void *)(2 * sizeof(float)));
    glEnableVertexAttribArray(1);

    // Create texture with minimal filtering (fastest)
    glGenTextures(1, &texture_);
    glBindTexture(GL_TEXTURE_2D, texture_);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);  // Fastest
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);  // Fastest
    
    // Optimize pixel transfer for RGB (3 bytes per pixel, not 4-byte aligned)
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);

    glBindVertexArray(0);

    return true;
}

void Renderer::resize(int width, int height)
{
    textureWidth_ = width;
    textureHeight_ = height;

    glBindTexture(GL_TEXTURE_2D, texture_);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, width, height, 0,
                 GL_RGB, GL_UNSIGNED_BYTE, nullptr);
}

void Renderer::uploadFrame(const uint8_t *rgb_data, int width, int height)
{
    if (width != textureWidth_ || height != textureHeight_)
    {
        resize(width, height);
    }

    glBindTexture(GL_TEXTURE_2D, texture_);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, width, height,
                    GL_RGB, GL_UNSIGNED_BYTE, rgb_data);
}

void Renderer::render(int viewport_width, int viewport_height)
{
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    if (textureWidth_ == 0 || textureHeight_ == 0)
        return;

    // Calculate aspect-correct viewport
    float videoAspect = static_cast<float>(textureWidth_) / textureHeight_;
    float windowAspect = static_cast<float>(viewport_width) / viewport_height;

    int vp_x = 0, vp_y = 0, vp_w = viewport_width, vp_h = viewport_height;

    if (videoAspect > windowAspect)
    {
        // Video is wider - letterbox top/bottom
        vp_h = static_cast<int>(viewport_width / videoAspect);
        vp_y = (viewport_height - vp_h) / 2;
    }
    else
    {
        // Video is taller - pillarbox left/right
        vp_w = static_cast<int>(viewport_height * videoAspect);
        vp_x = (viewport_width - vp_w) / 2;
    }

    glViewport(vp_x, vp_y, vp_w, vp_h);

    glUseProgram(shaderProgram_);
    glBindVertexArray(vao_);
    glBindTexture(GL_TEXTURE_2D, texture_);
    glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, 0);

    // Reset viewport for any overlay rendering
    glViewport(0, 0, viewport_width, viewport_height);
}
