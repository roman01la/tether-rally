#pragma once

#include <string>
#include <functional>
#include <thread>
#include <atomic>
#include <mutex>

extern "C"
{
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libswscale/swscale.h>
#include <libavutil/imgutils.h>
#include <libavutil/hwcontext.h>
}

class StreamDecoder
{
public:
    using FrameCallback = std::function<void(const uint8_t *data, int width, int height)>;

    StreamDecoder();
    ~StreamDecoder();

    // Connect to RTSP stream
    bool connect(const std::string &url);

    // Start decoding in background thread
    void start(FrameCallback callback);

    // Stop decoding
    void stop();

    // Check if connected
    bool isConnected() const { return connected_; }

    // Get stream info
    int getWidth() const { return width_; }
    int getHeight() const { return height_; }
    double getFPS() const { return fps_; }

private:
    void decodeLoop();
    bool setupHardwareDecoder();
    bool convertFrame(AVFrame *frame, uint8_t *output);

    AVFormatContext *formatCtx_ = nullptr;
    AVCodecContext *codecCtx_ = nullptr;
    SwsContext *swsCtx_ = nullptr;
    AVBufferRef *hwDeviceCtx_ = nullptr;

    int videoStreamIndex_ = -1;
    int width_ = 0;
    int height_ = 0;
    double fps_ = 0;

    std::atomic<bool> running_{false};
    std::atomic<bool> connected_{false};
    std::thread decodeThread_;
    FrameCallback frameCallback_;

    // For hardware frame transfer
    AVFrame *swFrame_ = nullptr;
    AVPixelFormat hwPixFmt_ = AV_PIX_FMT_NONE;
};
