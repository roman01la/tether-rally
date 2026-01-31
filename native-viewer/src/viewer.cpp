/**
 * Native WebRTC Viewer - Viewer Implementation
 *
 * Main viewer class implementation for macOS.
 * Uses VideoToolbox for hardware H.264 decoding.
 */

#include "viewer.h"
#include "whep_client.h"
#include "renderer.h"

#include <GLFW/glfw3.h>

#include <chrono>
#include <iostream>
#include <thread>

namespace native_viewer {

Viewer::Viewer() = default;

Viewer::~Viewer() {
    stop();
    
    renderer_.reset();
    whep_client_.reset();
    
    if (window_) {
        glfwDestroyWindow(window_);
        window_ = nullptr;
    }
    
    glfwTerminate();
}

void Viewer::errorCallback(int error, const char* description) {
    std::cerr << "GLFW Error " << error << ": " << description << "\n";
}

void Viewer::keyCallback(GLFWwindow* window, int key, int scancode, int action, int mods) {
    (void)scancode;
    (void)mods;
    
    if (action != GLFW_PRESS) {
        return;
    }
    
    Viewer* viewer = static_cast<Viewer*>(glfwGetWindowUserPointer(window));
    if (!viewer) {
        return;
    }
    
    switch (key) {
        case GLFW_KEY_ESCAPE:
            viewer->stop();
            break;
        case GLFW_KEY_F:
            viewer->toggleFullscreen();
            break;
        case GLFW_KEY_S:
            viewer->toggleStatsOverlay();
            break;
        default:
            break;
    }
}

void Viewer::framebufferSizeCallback(GLFWwindow* window, int width, int height) {
    Viewer* viewer = static_cast<Viewer*>(glfwGetWindowUserPointer(window));
    if (viewer) {
        viewer->fb_width_ = width;
        viewer->fb_height_ = height;
        if (viewer->renderer_) {
            viewer->renderer_->resize(width, height);
        }
    }
}

bool Viewer::initialize(const ViewerConfig& config) {
    config_ = config;
    
    if (!initWindow()) {
        return false;
    }
    
    // Create renderer
    renderer_ = std::make_unique<Renderer>();
    if (!renderer_->initialize(fb_width_, fb_height_)) {
        std::cerr << "Failed to initialize renderer\n";
        return false;
    }
    
    // Create WHEP client
    whep_client_ = std::make_unique<WHEPClient>();
    
    WHEPConfig whep_config{};
    whep_config.whep_url = config_.whep_url;
    whep_config.turn_url = config_.turn_url;
    whep_config.turn_user = config_.turn_user;
    whep_config.turn_pass = config_.turn_pass;
    
    // Set frame callback - this is called when a new frame is decoded
    whep_client_->setFrameCallback([this](const VideoFrame& frame) {
        if (renderer_) {
            renderer_->submitFrame(frame);
        }
        stats_.frames_decoded++;
        stats_.video_width = frame.width;
        stats_.video_height = frame.height;
    });
    
    // Set connection callback
    whep_client_->setConnectionCallback([this](bool connected) {
        stats_.connected = connected;
        if (connected) {
            std::cout << "WebRTC connected!\n";
        } else {
            std::cout << "WebRTC disconnected\n";
        }
    });
    
    if (!whep_client_->initialize(whep_config)) {
        std::cerr << "Failed to initialize WHEP client\n";
        return false;
    }
    
    return true;
}

bool Viewer::initWindow() {
    glfwSetErrorCallback(errorCallback);
    
    if (!glfwInit()) {
        std::cerr << "Failed to initialize GLFW\n";
        return false;
    }
    
    // OpenGL 3.3 Core Profile (compatible with macOS)
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
    glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
    glfwWindowHint(GLFW_OPENGL_FORWARD_COMPAT, GL_TRUE);  // Required on macOS
    
    // For low latency rendering
    glfwWindowHint(GLFW_DOUBLEBUFFER, GLFW_TRUE);
    
    GLFWmonitor* monitor = nullptr;
    int width = config_.window_width;
    int height = config_.window_height;
    
    if (config_.fullscreen) {
        monitor = glfwGetPrimaryMonitor();
        const GLFWvidmode* mode = glfwGetVideoMode(monitor);
        width = mode->width;
        height = mode->height;
    }
    
    window_ = glfwCreateWindow(width, height, "Tether Rally - Native Viewer", monitor, nullptr);
    if (!window_) {
        std::cerr << "Failed to create GLFW window\n";
        glfwTerminate();
        return false;
    }
    
    glfwSetWindowUserPointer(window_, this);
    glfwSetKeyCallback(window_, keyCallback);
    glfwSetFramebufferSizeCallback(window_, framebufferSizeCallback);
    
    glfwMakeContextCurrent(window_);
    
    // Disable vsync for lowest latency (may cause tearing)
    // Set to 1 for vsync enabled
    glfwSwapInterval(0);
    
    glfwGetFramebufferSize(window_, &fb_width_, &fb_height_);
    
    std::cout << "OpenGL Version: " << glGetString(GL_VERSION) << "\n";
    std::cout << "OpenGL Renderer: " << glGetString(GL_RENDERER) << "\n";
    
    return true;
}

bool Viewer::initWebRTC() {
    return whep_client_->connect();
}

int Viewer::run() {
    running_ = true;
    
    // Start WebRTC connection
    if (!initWebRTC()) {
        std::cerr << "Failed to start WebRTC connection\n";
        return 1;
    }
    
    auto last_time = std::chrono::steady_clock::now();
    int frame_count = 0;
    
    while (running_ && !glfwWindowShouldClose(window_)) {
        glfwPollEvents();
        
        // Process WebRTC events
        whep_client_->poll();
        
        // Render
        renderFrame();
        
        // Swap buffers
        glfwSwapBuffers(window_);
        
        // Update FPS counter
        frame_count++;
        auto now = std::chrono::steady_clock::now();
        auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_time).count();
        if (elapsed >= 1000) {
            stats_.framerate = frame_count;
            frame_count = 0;
            last_time = now;
            updateStats();
        }
    }
    
    return 0;
}

void Viewer::renderFrame() {
    glViewport(0, 0, fb_width_, fb_height_);
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    
    if (renderer_) {
        renderer_->render();
        
        if (show_stats_) {
            renderer_->renderStats(stats_);
        }
    }
}

void Viewer::updateStats() {
    if (whep_client_) {
        auto client_stats = whep_client_->getStats();
        stats_.rtt_ms = client_stats.rtt_ms;
        stats_.bitrate_kbps = client_stats.bitrate_kbps;
    }
}

void Viewer::stop() {
    running_ = false;
    if (whep_client_) {
        whep_client_->disconnect();
    }
}

ViewerStats Viewer::getStats() const {
    return stats_;
}

void Viewer::toggleFullscreen() {
    if (glfwGetWindowMonitor(window_)) {
        // Currently fullscreen, switch to windowed
        glfwSetWindowMonitor(window_, nullptr, 
                            windowed_x_, windowed_y_,
                            windowed_width_, windowed_height_, 0);
    } else {
        // Currently windowed, switch to fullscreen
        glfwGetWindowPos(window_, &windowed_x_, &windowed_y_);
        glfwGetWindowSize(window_, &windowed_width_, &windowed_height_);
        
        GLFWmonitor* monitor = glfwGetPrimaryMonitor();
        const GLFWvidmode* mode = glfwGetVideoMode(monitor);
        glfwSetWindowMonitor(window_, monitor, 0, 0, mode->width, mode->height, mode->refreshRate);
    }
}

void Viewer::toggleStatsOverlay() {
    show_stats_ = !show_stats_;
}

} // namespace native_viewer
