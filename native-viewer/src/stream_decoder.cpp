#include "stream_decoder.h"
#include <iostream>

// Global for get_format callback (used by VideoToolbox hwaccel)
static AVPixelFormat g_hwPixFmt = AV_PIX_FMT_NONE;

static AVPixelFormat getHwFormat(AVCodecContext * /*ctx*/, const AVPixelFormat *pix_fmts)
{
    for (const AVPixelFormat *p = pix_fmts; *p != AV_PIX_FMT_NONE; p++)
    {
        if (*p == g_hwPixFmt)
            return *p;
    }
    // Fallback to first software format
    return pix_fmts[0];
}

StreamDecoder::StreamDecoder()
{
    // FFmpeg network initialization
    avformat_network_init();
}

StreamDecoder::~StreamDecoder()
{
    stop();

    if (swsCtx_)
        sws_freeContext(swsCtx_);
    if (swFrame_)
        av_frame_free(&swFrame_);
    if (codecCtx_)
        avcodec_free_context(&codecCtx_);
    if (formatCtx_)
        avformat_close_input(&formatCtx_);
    if (hwDeviceCtx_)
        av_buffer_unref(&hwDeviceCtx_);

    avformat_network_deinit();
}

bool StreamDecoder::connect(const std::string &url)
{
    // Set low-latency options for RTSP
    AVDictionary *opts = nullptr;
    av_dict_set(&opts, "rtsp_transport", "tcp", 0);
    av_dict_set(&opts, "fflags", "nobuffer", 0);
    av_dict_set(&opts, "flags", "low_delay", 0);
    av_dict_set(&opts, "probesize", "500000", 0);       // Need enough to detect SPS/PPS
    av_dict_set(&opts, "analyzeduration", "500000", 0); // 0.5 sec max

    // Open input stream
    formatCtx_ = avformat_alloc_context();
    if (avformat_open_input(&formatCtx_, url.c_str(), nullptr, &opts) < 0)
    {
        std::cerr << "Failed to open stream: " << url << std::endl;
        av_dict_free(&opts);
        return false;
    }
    av_dict_free(&opts);

    // Find stream info (minimal probing)
    formatCtx_->max_analyze_duration = 0;
    if (avformat_find_stream_info(formatCtx_, nullptr) < 0)
    {
        std::cerr << "Failed to find stream info" << std::endl;
        return false;
    }

    // Find video stream
    for (unsigned int i = 0; i < formatCtx_->nb_streams; i++)
    {
        if (formatCtx_->streams[i]->codecpar->codec_type == AVMEDIA_TYPE_VIDEO)
        {
            videoStreamIndex_ = i;
            break;
        }
    }

    if (videoStreamIndex_ < 0)
    {
        std::cerr << "No video stream found" << std::endl;
        return false;
    }

    AVCodecParameters *codecpar = formatCtx_->streams[videoStreamIndex_]->codecpar;
    width_ = codecpar->width;
    height_ = codecpar->height;

    AVRational framerate = formatCtx_->streams[videoStreamIndex_]->avg_frame_rate;
    if (framerate.num > 0 && framerate.den > 0)
    {
        fps_ = static_cast<double>(framerate.num) / framerate.den;
    }

    std::cout << "Stream: " << width_ << "x" << height_ << " @ " << fps_ << " fps" << std::endl;

    // Note: VideoToolbox hwaccel has issues with RTSP/Annex-B streams from go2rtc
    // Software decode works well on Apple Silicon for 720p60
    bool hwSuccess = false; // Disable HW decode for now

    if (!hwSuccess)
    {
        // Fall back to software decoder
        const AVCodec *codec = avcodec_find_decoder(codecpar->codec_id);
        if (!codec)
        {
            std::cerr << "Codec not found" << std::endl;
            return false;
        }

        codecCtx_ = avcodec_alloc_context3(codec);
        avcodec_parameters_to_context(codecCtx_, codecpar);

        // Low-latency flags
        codecCtx_->flags |= AV_CODEC_FLAG_LOW_DELAY;
        codecCtx_->flags2 |= AV_CODEC_FLAG2_FAST;

        if (avcodec_open2(codecCtx_, codec, nullptr) < 0)
        {
            std::cerr << "Failed to open codec" << std::endl;
            return false;
        }

        std::cout << "Using software decoder: " << codec->name << std::endl;
    }

    connected_ = true;
    return true;
}

bool StreamDecoder::setupHardwareDecoder()
{
#ifdef __APPLE__
    AVCodecParameters *codecpar = formatCtx_->streams[videoStreamIndex_]->codecpar;

    // Check if we have codec extradata (SPS/PPS for H.264)
    if (codecpar->extradata && codecpar->extradata_size > 0)
    {
        std::cout << "Codec extradata present: " << codecpar->extradata_size << " bytes" << std::endl;
    }
    else
    {
        std::cout << "Warning: No codec extradata (SPS/PPS)" << std::endl;
        return false;
    }

    // Standard h264 decoder with VideoToolbox hwaccel
    const AVCodec *codec = avcodec_find_decoder(codecpar->codec_id);
    if (!codec)
    {
        std::cerr << "Codec not found" << std::endl;
        return false;
    }

    codecCtx_ = avcodec_alloc_context3(codec);
    avcodec_parameters_to_context(codecCtx_, codecpar);

    // Create hardware device context
    if (av_hwdevice_ctx_create(&hwDeviceCtx_, AV_HWDEVICE_TYPE_VIDEOTOOLBOX,
                               nullptr, nullptr, 0) < 0)
    {
        std::cerr << "Failed to create VideoToolbox context" << std::endl;
        avcodec_free_context(&codecCtx_);
        return false;
    }

    codecCtx_->hw_device_ctx = av_buffer_ref(hwDeviceCtx_);
    hwPixFmt_ = AV_PIX_FMT_VIDEOTOOLBOX;

    // Set the get_format callback
    g_hwPixFmt = AV_PIX_FMT_VIDEOTOOLBOX;
    codecCtx_->get_format = getHwFormat;

    // Low-latency flags
    codecCtx_->flags |= AV_CODEC_FLAG_LOW_DELAY;
    codecCtx_->flags2 |= AV_CODEC_FLAG2_FAST;
    codecCtx_->thread_count = 1;

    if (avcodec_open2(codecCtx_, codec, nullptr) < 0)
    {
        std::cerr << "Failed to open VideoToolbox decoder" << std::endl;
        av_buffer_unref(&hwDeviceCtx_);
        avcodec_free_context(&codecCtx_);
        return false;
    }

    swFrame_ = av_frame_alloc();
    std::cout << "Using VideoToolbox hardware decoder" << std::endl;
    return true;
#else
    return false;
#endif
}

void StreamDecoder::start(FrameCallback callback)
{
    if (running_ || !connected_)
        return;

    frameCallback_ = callback;
    running_ = true;
    decodeThread_ = std::thread(&StreamDecoder::decodeLoop, this);
}

void StreamDecoder::stop()
{
    running_ = false;
    if (decodeThread_.joinable())
    {
        decodeThread_.join();
    }
}

void StreamDecoder::decodeLoop()
{
    AVPacket *packet = av_packet_alloc();
    AVFrame *frame = av_frame_alloc();

    // Buffer for RGB output
    int rgbSize = av_image_get_buffer_size(AV_PIX_FMT_RGB24, width_, height_, 1);
    uint8_t *rgbBuffer = static_cast<uint8_t *>(av_malloc(rgbSize));

    while (running_)
    {
        if (av_read_frame(formatCtx_, packet) < 0)
        {
            // Reconnect or exit
            break;
        }

        if (packet->stream_index != videoStreamIndex_)
        {
            av_packet_unref(packet);
            continue;
        }

        if (avcodec_send_packet(codecCtx_, packet) < 0)
        {
            av_packet_unref(packet);
            continue;
        }

        while (avcodec_receive_frame(codecCtx_, frame) >= 0)
        {
            AVFrame *srcFrame = frame;

            // Handle VideoToolbox hardware surfaces (AV_PIX_FMT_VIDEOTOOLBOX)
            if (frame->format == AV_PIX_FMT_VIDEOTOOLBOX)
            {
                // Transfer from GPU to CPU - may fail on corrupted frames
                if (av_hwframe_transfer_data(swFrame_, frame, 0) < 0)
                {
                    // Skip this frame, wait for next keyframe
                    av_frame_unref(frame);
                    continue;
                }
                srcFrame = swFrame_;
            }

            // Convert to RGB
            if (!swsCtx_)
            {
                swsCtx_ = sws_getContext(
                    srcFrame->width, srcFrame->height,
                    static_cast<AVPixelFormat>(srcFrame->format),
                    width_, height_, AV_PIX_FMT_RGB24,
                    SWS_BILINEAR, nullptr, nullptr, nullptr);
            }

            uint8_t *dstData[1] = {rgbBuffer};
            int dstLinesize[1] = {3 * width_};

            sws_scale(swsCtx_, srcFrame->data, srcFrame->linesize,
                      0, srcFrame->height, dstData, dstLinesize);

            if (frameCallback_)
            {
                frameCallback_(rgbBuffer, width_, height_);
            }
        }

        av_packet_unref(packet);
    }

    av_free(rgbBuffer);
    av_frame_free(&frame);
    av_packet_free(&packet);
}
