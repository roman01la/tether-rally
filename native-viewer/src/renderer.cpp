/**
 * Native WebRTC Viewer - Renderer Implementation
 *
 * OpenGL-based video renderer for displaying decoded YUV frames.
 * Supports both I420 (YUV420P) and NV12 pixel formats.
 */

#include "renderer.h"
#include "viewer.h"

#ifdef __APPLE__
#define GL_SILENCE_DEPRECATION
#include <OpenGL/gl3.h>
#else
#include <GL/gl.h>
#endif

#include <cstring>
#include <iostream>

namespace native_viewer {

// Vertex shader - simple fullscreen quad
static const char* vertex_shader_source = R"(
#version 330 core
layout (location = 0) in vec2 aPos;
layout (location = 1) in vec2 aTexCoord;

out vec2 TexCoord;

void main() {
    gl_Position = vec4(aPos, 0.0, 1.0);
    TexCoord = aTexCoord;
}
)";

// Fragment shader - YUV to RGB conversion
// Supports both I420 (separate U/V planes) and NV12 (interleaved UV)
static const char* fragment_shader_source = R"(
#version 330 core
out vec4 FragColor;
in vec2 TexCoord;

uniform sampler2D tex_y;
uniform sampler2D tex_u;
uniform sampler2D tex_v;
uniform sampler2D tex_uv;
uniform bool is_nv12;

void main() {
    float y = texture(tex_y, TexCoord).r;
    float u, v;
    
    if (is_nv12) {
        // NV12: UV interleaved in single texture
        vec2 uv = texture(tex_uv, TexCoord).rg;
        u = uv.r;
        v = uv.g;
    } else {
        // I420: Separate U and V planes
        u = texture(tex_u, TexCoord).r;
        v = texture(tex_v, TexCoord).r;
    }
    
    // BT.601 YUV to RGB conversion (standard for web video)
    // Y: 16-235, U/V: 16-240 (but we use full range here)
    y = 1.164 * (y - 0.0625);
    u = u - 0.5;
    v = v - 0.5;
    
    float r = y + 1.596 * v;
    float g = y - 0.391 * u - 0.813 * v;
    float b = y + 2.018 * u;
    
    FragColor = vec4(clamp(r, 0.0, 1.0), clamp(g, 0.0, 1.0), clamp(b, 0.0, 1.0), 1.0);
}
)";

// Fullscreen quad vertices (position + texcoord)
static const float quad_vertices[] = {
    // Position    // TexCoord (flipped Y for OpenGL)
    -1.0f,  1.0f,  0.0f, 0.0f,  // Top-left
     1.0f,  1.0f,  1.0f, 0.0f,  // Top-right
     1.0f, -1.0f,  1.0f, 1.0f,  // Bottom-right
    -1.0f, -1.0f,  0.0f, 1.0f,  // Bottom-left
};

static const unsigned int quad_indices[] = {
    0, 1, 2,  // First triangle
    0, 2, 3   // Second triangle
};

Renderer::Renderer() = default;

Renderer::~Renderer() {
    destroyResources();
}

bool Renderer::initialize(int width, int height) {
    viewport_width_ = width;
    viewport_height_ = height;
    
    if (!createShaders()) {
        return false;
    }
    
    // Create VAO, VBO, EBO for fullscreen quad
    glGenVertexArrays(1, &vao_);
    glGenBuffers(1, &vbo_);
    glGenBuffers(1, &ebo_);
    
    glBindVertexArray(vao_);
    
    glBindBuffer(GL_ARRAY_BUFFER, vbo_);
    glBufferData(GL_ARRAY_BUFFER, sizeof(quad_vertices), quad_vertices, GL_STATIC_DRAW);
    
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, ebo_);
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, sizeof(quad_indices), quad_indices, GL_STATIC_DRAW);
    
    // Position attribute
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), (void*)0);
    glEnableVertexAttribArray(0);
    
    // TexCoord attribute
    glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), (void*)(2 * sizeof(float)));
    glEnableVertexAttribArray(1);
    
    glBindVertexArray(0);
    
    std::cout << "Renderer initialized (" << width << "x" << height << ")\n";
    
    return true;
}

bool Renderer::createShaders() {
    // Compile vertex shader
    GLuint vertex_shader = glCreateShader(GL_VERTEX_SHADER);
    glShaderSource(vertex_shader, 1, &vertex_shader_source, nullptr);
    glCompileShader(vertex_shader);
    
    GLint success;
    glGetShaderiv(vertex_shader, GL_COMPILE_STATUS, &success);
    if (!success) {
        char info_log[512];
        glGetShaderInfoLog(vertex_shader, 512, nullptr, info_log);
        std::cerr << "Vertex shader compilation failed: " << info_log << "\n";
        return false;
    }
    
    // Compile fragment shader
    GLuint fragment_shader = glCreateShader(GL_FRAGMENT_SHADER);
    glShaderSource(fragment_shader, 1, &fragment_shader_source, nullptr);
    glCompileShader(fragment_shader);
    
    glGetShaderiv(fragment_shader, GL_COMPILE_STATUS, &success);
    if (!success) {
        char info_log[512];
        glGetShaderInfoLog(fragment_shader, 512, nullptr, info_log);
        std::cerr << "Fragment shader compilation failed: " << info_log << "\n";
        return false;
    }
    
    // Link program
    shader_program_ = glCreateProgram();
    glAttachShader(shader_program_, vertex_shader);
    glAttachShader(shader_program_, fragment_shader);
    glLinkProgram(shader_program_);
    
    glGetProgramiv(shader_program_, GL_LINK_STATUS, &success);
    if (!success) {
        char info_log[512];
        glGetProgramInfoLog(shader_program_, 512, nullptr, info_log);
        std::cerr << "Shader program linking failed: " << info_log << "\n";
        return false;
    }
    
    glDeleteShader(vertex_shader);
    glDeleteShader(fragment_shader);
    
    // Get uniform locations
    loc_tex_y_ = glGetUniformLocation(shader_program_, "tex_y");
    loc_tex_u_ = glGetUniformLocation(shader_program_, "tex_u");
    loc_tex_v_ = glGetUniformLocation(shader_program_, "tex_v");
    loc_tex_uv_ = glGetUniformLocation(shader_program_, "tex_uv");
    loc_is_nv12_ = glGetUniformLocation(shader_program_, "is_nv12");
    
    std::cout << "Shaders compiled successfully\n";
    
    return true;
}

bool Renderer::createTextures(int width, int height) {
    video_width_ = width;
    video_height_ = height;
    
    // Delete existing textures
    if (tex_y_) glDeleteTextures(1, &tex_y_);
    if (tex_u_) glDeleteTextures(1, &tex_u_);
    if (tex_v_) glDeleteTextures(1, &tex_v_);
    if (tex_uv_) glDeleteTextures(1, &tex_uv_);
    
    auto createTexture = [](GLuint& tex, int w, int h, GLenum format) {
        glGenTextures(1, &tex);
        glBindTexture(GL_TEXTURE_2D, tex);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        glTexImage2D(GL_TEXTURE_2D, 0, format, w, h, 0, GL_RED, GL_UNSIGNED_BYTE, nullptr);
    };
    
    // Y plane (full resolution)
    createTexture(tex_y_, width, height, GL_R8);
    
    // U and V planes (half resolution for I420)
    createTexture(tex_u_, width / 2, height / 2, GL_R8);
    createTexture(tex_v_, width / 2, height / 2, GL_R8);
    
    // UV plane for NV12 (half resolution, RG format)
    glGenTextures(1, &tex_uv_);
    glBindTexture(GL_TEXTURE_2D, tex_uv_);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RG8, width / 2, height / 2, 0, GL_RG, GL_UNSIGNED_BYTE, nullptr);
    
    glBindTexture(GL_TEXTURE_2D, 0);
    
    std::cout << "Created textures for " << width << "x" << height << " video\n";
    
    return true;
}

void Renderer::resize(int width, int height) {
    viewport_width_ = width;
    viewport_height_ = height;
}

void Renderer::submitFrame(const VideoFrame& frame) {
    std::lock_guard<std::mutex> lock(frame_mutex_);
    
    // Copy frame data (since pointers may be temporary)
    size_t y_size = frame.stride_y * frame.height;
    
    if (frame.is_nv12) {
        size_t uv_size = frame.stride_uv * (frame.height / 2);
        
        y_data_.resize(y_size);
        uv_data_.resize(uv_size);
        
        if (frame.data_y) {
            std::memcpy(y_data_.data(), frame.data_y, y_size);
        }
        if (frame.data_uv) {
            std::memcpy(uv_data_.data(), frame.data_uv, uv_size);
        }
    } else {
        size_t u_size = frame.stride_u * (frame.height / 2);
        size_t v_size = frame.stride_v * (frame.height / 2);
        
        y_data_.resize(y_size);
        u_data_.resize(u_size);
        v_data_.resize(v_size);
        
        if (frame.data_y) {
            std::memcpy(y_data_.data(), frame.data_y, y_size);
        }
        if (frame.data_u) {
            std::memcpy(u_data_.data(), frame.data_u, u_size);
        }
        if (frame.data_v) {
            std::memcpy(v_data_.data(), frame.data_v, v_size);
        }
    }
    
    pending_frame_ = frame;
    pending_frame_.data_y = y_data_.data();
    if (frame.is_nv12) {
        pending_frame_.data_uv = uv_data_.data();
    } else {
        pending_frame_.data_u = u_data_.data();
        pending_frame_.data_v = v_data_.data();
    }
    
    frame_pending_ = true;
}

void Renderer::updateTextures(const VideoFrame& frame) {
    // Create or resize textures if needed
    if (video_width_ != frame.width || video_height_ != frame.height) {
        createTextures(frame.width, frame.height);
    }
    
    is_nv12_ = frame.is_nv12;
    
    // Update Y texture
    glBindTexture(GL_TEXTURE_2D, tex_y_);
    glPixelStorei(GL_UNPACK_ROW_LENGTH, frame.stride_y);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, frame.width, frame.height,
                    GL_RED, GL_UNSIGNED_BYTE, frame.data_y);
    
    if (frame.is_nv12) {
        // Update UV texture for NV12
        glBindTexture(GL_TEXTURE_2D, tex_uv_);
        glPixelStorei(GL_UNPACK_ROW_LENGTH, frame.stride_uv / 2);
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, frame.width / 2, frame.height / 2,
                        GL_RG, GL_UNSIGNED_BYTE, frame.data_uv);
    } else {
        // Update U and V textures for I420
        glBindTexture(GL_TEXTURE_2D, tex_u_);
        glPixelStorei(GL_UNPACK_ROW_LENGTH, frame.stride_u);
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, frame.width / 2, frame.height / 2,
                        GL_RED, GL_UNSIGNED_BYTE, frame.data_u);
        
        glBindTexture(GL_TEXTURE_2D, tex_v_);
        glPixelStorei(GL_UNPACK_ROW_LENGTH, frame.stride_v);
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, frame.width / 2, frame.height / 2,
                        GL_RED, GL_UNSIGNED_BYTE, frame.data_v);
    }
    
    glPixelStorei(GL_UNPACK_ROW_LENGTH, 0);
    glBindTexture(GL_TEXTURE_2D, 0);
    
    has_frame_ = true;
}

void Renderer::render() {
    // Check for new frame
    {
        std::lock_guard<std::mutex> lock(frame_mutex_);
        if (frame_pending_) {
            updateTextures(pending_frame_);
            frame_pending_ = false;
        }
    }
    
    if (!has_frame_) {
        // No frame yet, draw placeholder
        glClearColor(0.1f, 0.1f, 0.15f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        return;
    }
    
    // Calculate aspect-correct viewport
    float video_aspect = static_cast<float>(video_width_) / video_height_;
    float viewport_aspect = static_cast<float>(viewport_width_) / viewport_height_;
    
    int render_width, render_height;
    int offset_x = 0, offset_y = 0;
    
    if (video_aspect > viewport_aspect) {
        // Video is wider than viewport
        render_width = viewport_width_;
        render_height = static_cast<int>(viewport_width_ / video_aspect);
        offset_y = (viewport_height_ - render_height) / 2;
    } else {
        // Video is taller than viewport
        render_height = viewport_height_;
        render_width = static_cast<int>(viewport_height_ * video_aspect);
        offset_x = (viewport_width_ - render_width) / 2;
    }
    
    // Clear background
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    
    // Set viewport for video
    glViewport(offset_x, offset_y, render_width, render_height);
    
    // Render video
    glUseProgram(shader_program_);
    
    // Bind textures
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, tex_y_);
    glUniform1i(loc_tex_y_, 0);
    
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, tex_u_);
    glUniform1i(loc_tex_u_, 1);
    
    glActiveTexture(GL_TEXTURE2);
    glBindTexture(GL_TEXTURE_2D, tex_v_);
    glUniform1i(loc_tex_v_, 2);
    
    glActiveTexture(GL_TEXTURE3);
    glBindTexture(GL_TEXTURE_2D, tex_uv_);
    glUniform1i(loc_tex_uv_, 3);
    
    glUniform1i(loc_is_nv12_, is_nv12_ ? 1 : 0);
    
    // Draw quad
    glBindVertexArray(vao_);
    glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, 0);
    glBindVertexArray(0);
    
    // Reset viewport
    glViewport(0, 0, viewport_width_, viewport_height_);
}

void Renderer::renderStats(const ViewerStats& stats) {
    // For now, stats are printed to console
    // A full implementation would render text overlay using:
    // - FreeType for text rendering
    // - Or a simple bitmap font
    
    // This is a placeholder - text rendering requires additional libraries
    (void)stats;
}

bool Renderer::hasFrame() const {
    return has_frame_;
}

void Renderer::destroyResources() {
    if (tex_y_) { glDeleteTextures(1, &tex_y_); tex_y_ = 0; }
    if (tex_u_) { glDeleteTextures(1, &tex_u_); tex_u_ = 0; }
    if (tex_v_) { glDeleteTextures(1, &tex_v_); tex_v_ = 0; }
    if (tex_uv_) { glDeleteTextures(1, &tex_uv_); tex_uv_ = 0; }
    
    if (shader_program_) { glDeleteProgram(shader_program_); shader_program_ = 0; }
    if (vao_) { glDeleteVertexArrays(1, &vao_); vao_ = 0; }
    if (vbo_) { glDeleteBuffers(1, &vbo_); vbo_ = 0; }
    if (ebo_) { glDeleteBuffers(1, &ebo_); ebo_ = 0; }
}

} // namespace native_viewer
