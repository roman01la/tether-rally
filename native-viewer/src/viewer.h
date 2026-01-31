/**
 * Native WebRTC Viewer - Viewer Header
 *
 * Main viewer class that orchestrates GLFW window, WebRTC connection,
 * and video rendering.
 */

#ifndef NATIVE_VIEWER_VIEWER_H
#define NATIVE_VIEWER_VIEWER_H

#include <memory>
#include <string>
#include <atomic>

// Forward declarations
struct GLFWwindow;

namespace native_viewer {

// Forward declarations
class WHEPClient;
class Renderer;

/**
 * Configuration for the viewer
 */
struct ViewerConfig {
    std::string whep_url;          // WHEP endpoint URL
    std::string turn_url;          // Optional TURN server URL
    std::string turn_user;         // TURN username
    std::string turn_pass;         // TURN password
    int window_width = 1280;       // Window width
    int window_height = 720;       // Window height
    bool fullscreen = false;       // Start fullscreen
};

/**
 * Statistics for display
 */
struct ViewerStats {
    int video_width = 0;
    int video_height = 0;
    int framerate = 0;
    int bitrate_kbps = 0;
    int rtt_ms = 0;
    int frames_decoded = 0;
    int frames_dropped = 0;
    bool connected = false;
};

/**
 * Main viewer class
 *
 * Manages the GLFW window, WebRTC connection via WHEP,
 * and renders video frames with minimal latency.
 */
class Viewer {
public:
    Viewer();
    ~Viewer();
    
    // Non-copyable
    Viewer(const Viewer&) = delete;
    Viewer& operator=(const Viewer&) = delete;
    
    /**
     * Initialize the viewer with the given configuration
     * @param config Viewer configuration
     * @return true on success
     */
    bool initialize(const ViewerConfig& config);
    
    /**
     * Run the main loop (blocks until window closes)
     * @return Exit code (0 = success)
     */
    int run();
    
    /**
     * Request the viewer to stop
     */
    void stop();
    
    /**
     * Get current statistics
     */
    ViewerStats getStats() const;
    
    /**
     * Toggle fullscreen mode
     */
    void toggleFullscreen();
    
    /**
     * Toggle stats overlay
     */
    void toggleStatsOverlay();
    
private:
    // GLFW callbacks
    static void keyCallback(GLFWwindow* window, int key, int scancode, int action, int mods);
    static void framebufferSizeCallback(GLFWwindow* window, int width, int height);
    static void errorCallback(int error, const char* description);
    
    // Internal methods
    bool initWindow();
    bool initWebRTC();
    void renderFrame();
    void updateStats();
    
    // Configuration
    ViewerConfig config_;
    
    // Window
    GLFWwindow* window_ = nullptr;
    int fb_width_ = 0;
    int fb_height_ = 0;
    bool show_stats_ = false;
    
    // Components
    std::unique_ptr<WHEPClient> whep_client_;
    std::unique_ptr<Renderer> renderer_;
    
    // State
    std::atomic<bool> running_{false};
    ViewerStats stats_;
    
    // For stats calculation
    int64_t last_stats_time_ = 0;
    int last_frame_count_ = 0;
};

} // namespace native_viewer

#endif // NATIVE_VIEWER_VIEWER_H
