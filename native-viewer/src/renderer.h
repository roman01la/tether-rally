/**
 * Native WebRTC Viewer - Renderer Header
 *
 * OpenGL-based video renderer for displaying decoded frames.
 * Supports YUV420P (I420) and NV12 pixel formats.
 */

#ifndef NATIVE_VIEWER_RENDERER_H
#define NATIVE_VIEWER_RENDERER_H

#include "whep_client.h"

#include <cstdint>
#include <mutex>
#include <string>

namespace native_viewer {

// Forward declaration
struct ViewerStats;

/**
 * OpenGL Video Renderer
 *
 * Renders YUV video frames to screen using OpenGL shaders.
 * Optimized for low latency with single-buffered texture updates.
 */
class Renderer {
public:
    Renderer();
    ~Renderer();
    
    // Non-copyable
    Renderer(const Renderer&) = delete;
    Renderer& operator=(const Renderer&) = delete;
    
    /**
     * Initialize OpenGL resources
     * @param width Viewport width
     * @param height Viewport height
     * @return true on success
     */
    bool initialize(int width, int height);
    
    /**
     * Handle viewport resize
     * @param width New width
     * @param height New height
     */
    void resize(int width, int height);
    
    /**
     * Submit a decoded video frame for rendering
     * @param frame Video frame data (YUV format)
     */
    void submitFrame(const VideoFrame& frame);
    
    /**
     * Render the current frame
     */
    void render();
    
    /**
     * Render stats overlay
     * @param stats Current viewer statistics
     */
    void renderStats(const ViewerStats& stats);
    
    /**
     * Check if renderer has a frame to display
     */
    bool hasFrame() const;
    
private:
    bool createShaders();
    bool createTextures(int width, int height);
    void updateTextures(const VideoFrame& frame);
    void destroyResources();
    
    // Viewport
    int viewport_width_ = 0;
    int viewport_height_ = 0;
    
    // Video dimensions
    int video_width_ = 0;
    int video_height_ = 0;
    
    // OpenGL resources
    uint32_t shader_program_ = 0;
    uint32_t vao_ = 0;
    uint32_t vbo_ = 0;
    uint32_t ebo_ = 0;
    
    // YUV textures
    uint32_t tex_y_ = 0;
    uint32_t tex_u_ = 0;
    uint32_t tex_v_ = 0;
    uint32_t tex_uv_ = 0;  // For NV12
    
    // Shader uniform locations
    int loc_tex_y_ = -1;
    int loc_tex_u_ = -1;
    int loc_tex_v_ = -1;
    int loc_tex_uv_ = -1;
    int loc_is_nv12_ = -1;
    
    // Frame state
    bool has_frame_ = false;
    bool is_nv12_ = false;
    
    // Thread safety for frame submission
    mutable std::mutex frame_mutex_;
    VideoFrame pending_frame_;
    bool frame_pending_ = false;
    
    // Frame data copy (since VideoFrame pointers may be temporary)
    std::vector<uint8_t> y_data_;
    std::vector<uint8_t> u_data_;
    std::vector<uint8_t> v_data_;
    std::vector<uint8_t> uv_data_;
};

} // namespace native_viewer

#endif // NATIVE_VIEWER_RENDERER_H
