#include "viewer.h"
#include "stream_decoder.h"
#include "renderer.h"

#include <GLFW/glfw3.h>
#include <iostream>
#include <sstream>
#include <iomanip>

Viewer::Viewer() = default;

Viewer::~Viewer()
{
    stop();

    renderer_.reset();
    decoder_.reset();

    if (window_)
    {
        glfwDestroyWindow(window_);
        window_ = nullptr;
    }
    glfwTerminate();
}

bool Viewer::initialize(const ViewerConfig &config)
{
    config_ = config;

    if (!initWindow())
    {
        return false;
    }

    if (!initDecoder())
    {
        return false;
    }

    return true;
}

bool Viewer::initWindow()
{
    if (!glfwInit())
    {
        std::cerr << "Failed to initialize GLFW" << std::endl;
        return false;
    }

    // OpenGL hints
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
    glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
#ifdef __APPLE__
    glfwWindowHint(GLFW_OPENGL_FORWARD_COMPAT, GL_TRUE);
#endif

    // Create window
    window_ = glfwCreateWindow(config_.window_width, config_.window_height,
                               "ARRMA Viewer", nullptr, nullptr);
    if (!window_)
    {
        std::cerr << "Failed to create GLFW window" << std::endl;
        glfwTerminate();
        return false;
    }

    // Store initial window position/size for fullscreen toggle
    glfwGetWindowPos(window_, &windowedX_, &windowedY_);
    windowedWidth_ = config_.window_width;
    windowedHeight_ = config_.window_height;

    // Set callbacks
    glfwSetWindowUserPointer(window_, this);
    glfwSetKeyCallback(window_, keyCallback);
    glfwSetFramebufferSizeCallback(window_, framebufferSizeCallback);

    // Make context current
    glfwMakeContextCurrent(window_);

    // Disable vsync for lowest latency
    glfwSwapInterval(0);

    // Create renderer
    renderer_ = std::make_unique<Renderer>();
    if (!renderer_->initialize())
    {
        std::cerr << "Failed to initialize renderer" << std::endl;
        return false;
    }

    return true;
}

bool Viewer::initDecoder()
{
    decoder_ = std::make_unique<StreamDecoder>();

    std::cout << "Connecting to: " << config_.stream_url << std::endl;

    if (!decoder_->connect(config_.stream_url))
    {
        std::cerr << "Failed to connect to stream" << std::endl;
        return false;
    }

    // Resize renderer to match video
    int w = decoder_->getWidth();
    int h = decoder_->getHeight();
    if (w > 0 && h > 0)
    {
        renderer_->resize(w, h);
    }

    return true;
}

int Viewer::run()
{
    running_ = true;
    lastFpsTime_ = glfwGetTime();
    lastFpsFrameCount_ = 0;

    // Start decoder with frame callback
    decoder_->start([this](const uint8_t *data, int w, int h)
                    { onFrame(data, w, h); });

    // Main loop
    while (running_ && !glfwWindowShouldClose(window_))
    {
        glfwPollEvents();
        renderFrame();
        glfwSwapBuffers(window_);

        // Update FPS and window title every second
        double currentTime = glfwGetTime();
        if (currentTime - lastFpsTime_ >= 1.0)
        {
            int framesDelta = framesDecoded_ - lastFpsFrameCount_;
            actualFps_ = framesDelta / (currentTime - lastFpsTime_);
            lastFpsFrameCount_ = framesDecoded_;
            lastFpsTime_ = currentTime;

            // Update window title with stats
            if (showStats_)
            {
                std::ostringstream title;
                title << "ARRMA Viewer - "
                      << decoder_->getWidth() << "x" << decoder_->getHeight()
                      << " @ " << std::fixed << std::setprecision(0) << actualFps_ << " fps";
                glfwSetWindowTitle(window_, title.str().c_str());
            }
            else
            {
                glfwSetWindowTitle(window_, "ARRMA Viewer");
            }
        }
    }

    decoder_->stop();
    return 0;
}

void Viewer::stop()
{
    running_ = false;
}

void Viewer::onFrame(const uint8_t *data, int width, int height)
{
    std::lock_guard<std::mutex> lock(frameMutex_);

    size_t size = width * height * 3;
    if (frameData_.size() != size)
    {
        frameData_.resize(size);
    }

    memcpy(frameData_.data(), data, size);
    frameWidth_ = width;
    frameHeight_ = height;
    newFrame_ = true;
    framesDecoded_++;
}

void Viewer::renderFrame()
{
    // Check for new frame
    {
        std::lock_guard<std::mutex> lock(frameMutex_);
        if (newFrame_)
        {
            renderer_->uploadFrame(frameData_.data(), frameWidth_, frameHeight_);
            newFrame_ = false;
        }
    }

    // Render
    int fbWidth, fbHeight;
    glfwGetFramebufferSize(window_, &fbWidth, &fbHeight);
    renderer_->render(fbWidth, fbHeight);
}

ViewerStats Viewer::getStats() const
{
    ViewerStats stats;
    if (decoder_)
    {
        stats.video_width = decoder_->getWidth();
        stats.video_height = decoder_->getHeight();
        stats.framerate = decoder_->getFPS();
        stats.connected = decoder_->isConnected();
    }
    stats.frames_decoded = framesDecoded_;
    stats.actual_fps = actualFps_;
    return stats;
}

void Viewer::toggleFullscreen()
{
    if (!window_)
        return;

    if (isFullscreen_)
    {
        // Restore windowed mode
        glfwSetWindowMonitor(window_, nullptr,
                             windowedX_, windowedY_,
                             windowedWidth_, windowedHeight_, 0);
        isFullscreen_ = false;
    }
    else
    {
        // Save windowed position/size
        glfwGetWindowPos(window_, &windowedX_, &windowedY_);
        glfwGetWindowSize(window_, &windowedWidth_, &windowedHeight_);

        // Switch to fullscreen
        GLFWmonitor *monitor = glfwGetPrimaryMonitor();
        const GLFWvidmode *mode = glfwGetVideoMode(monitor);
        glfwSetWindowMonitor(window_, monitor, 0, 0,
                             mode->width, mode->height, mode->refreshRate);
        isFullscreen_ = true;
    }
}

void Viewer::toggleStatsOverlay()
{
    showStats_ = !showStats_;
}

void Viewer::keyCallback(GLFWwindow *window, int key, int scancode,
                         int action, int mods)
{
    if (action != GLFW_PRESS)
        return;

    Viewer *viewer = static_cast<Viewer *>(glfwGetWindowUserPointer(window));
    if (!viewer)
        return;

    switch (key)
    {
    case GLFW_KEY_ESCAPE:
    case GLFW_KEY_Q:
        viewer->stop();
        break;
    case GLFW_KEY_F:
    case GLFW_KEY_F11:
        viewer->toggleFullscreen();
        break;
    case GLFW_KEY_S:
        viewer->toggleStatsOverlay();
        break;
    }
}

void Viewer::framebufferSizeCallback(GLFWwindow *window, int width, int height)
{
    (void)window;
    (void)width;
    (void)height;
    // Renderer handles this via glViewport in render()
}
