/**
 * Native Viewer - Viewer Header
 *
 * Main viewer class that orchestrates GLFW window, RTSP stream decoding,
 * and video rendering with minimal latency.
 */

#pragma once

#include <memory>
#include <string>
#include <atomic>
#include <mutex>
#include <vector>

struct GLFWwindow;

class StreamDecoder;
class Renderer;
class ControlChannel;

/**
 * Configuration for the viewer
 */
struct ViewerConfig
{
    std::string stream_url;           // RTSP stream URL
    std::string control_url;          // Control relay URL
    std::string turn_credentials_url; // TURN credentials URL (optional)
    std::string token;                // Authentication token
    int window_width = 1280;          // Window width
    int window_height = 720;          // Window height
    bool fullscreen = false;          // Start fullscreen
};

/**
 * Statistics for display
 */
struct ViewerStats
{
    int video_width = 0;
    int video_height = 0;
    double framerate = 0;
    int frames_decoded = 0;
    double actual_fps = 0;
    double control_latency = 0; // Control channel latency in ms
    bool connected = false;
    bool control_connected = false;
};

/**
 * Main viewer class
 */
class Viewer
{
public:
    Viewer();
    ~Viewer();

    // Non-copyable
    Viewer(const Viewer &) = delete;
    Viewer &operator=(const Viewer &) = delete;

    /**
     * Initialize the viewer with the given configuration
     */
    bool initialize(const ViewerConfig &config);

    /**
     * Run the main loop (blocks until window closes)
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
    static void keyCallback(GLFWwindow *window, int key, int scancode,
                            int action, int mods);
    static void framebufferSizeCallback(GLFWwindow *window, int width, int height);

    // Frame callback from decoder
    void onFrame(const uint8_t *data, int width, int height);

    // Initialization helpers
    bool initWindow();
    bool initDecoder();

    // Rendering
    void renderFrame();

    // Configuration
    ViewerConfig config_;

    // GLFW window
    GLFWwindow *window_ = nullptr;
    int windowedX_ = 0;
    int windowedY_ = 0;
    int windowedWidth_ = 0;
    int windowedHeight_ = 0;
    bool isFullscreen_ = false;

    // Components
    std::unique_ptr<StreamDecoder> decoder_;
    std::unique_ptr<Renderer> renderer_;
    std::unique_ptr<ControlChannel> controlChannel_;

    // State
    std::atomic<bool> running_{false};
    std::atomic<bool> showStats_{false};
    std::atomic<int> framesDecoded_{0};
    std::atomic<double> controlLatency_{0};

    // FPS measurement
    double lastFpsTime_ = 0;
    int lastFpsFrameCount_ = 0;
    double actualFps_ = 0;

    // Frame data (protected by mutex)
    std::mutex frameMutex_;
    std::vector<uint8_t> frameData_;
    int frameWidth_ = 0;
    int frameHeight_ = 0;
    bool newFrame_ = false;
};
