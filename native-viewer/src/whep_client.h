/**
 * Native WebRTC Viewer - WHEP Client Header
 *
 * Handles WebRTC signaling via WHEP (WebRTC-HTTP Egress Protocol)
 * and manages the peer connection with hardware video decoding.
 */

#ifndef NATIVE_VIEWER_WHEP_CLIENT_H
#define NATIVE_VIEWER_WHEP_CLIENT_H

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>
#include <atomic>
#include <mutex>
#include <thread>

namespace native_viewer {

/**
 * Raw video frame data
 */
struct VideoFrame {
    int width = 0;
    int height = 0;
    int stride_y = 0;
    int stride_u = 0;
    int stride_v = 0;
    const uint8_t* data_y = nullptr;
    const uint8_t* data_u = nullptr;
    const uint8_t* data_v = nullptr;
    int64_t timestamp_us = 0;
    
    // For NV12 format (VideoToolbox output)
    const uint8_t* data_uv = nullptr;
    int stride_uv = 0;
    bool is_nv12 = false;
};

/**
 * WHEP client configuration
 */
struct WHEPConfig {
    std::string whep_url;          // WHEP endpoint URL
    std::string turn_url;          // Optional TURN server
    std::string turn_user;         // TURN username  
    std::string turn_pass;         // TURN password
    bool hardware_decode = true;   // Use hardware decoding
    int jitter_buffer_ms = 0;      // Jitter buffer size (0 = disabled for low latency)
};

/**
 * Connection statistics
 */
struct WHEPStats {
    int rtt_ms = 0;
    int bitrate_kbps = 0;
    int packets_received = 0;
    int packets_lost = 0;
    int64_t bytes_received = 0;
    int frames_received = 0;  // Decoded video frames
};

/**
 * Frame callback type
 */
using FrameCallback = std::function<void(const VideoFrame&)>;

/**
 * Connection state callback type
 */
using ConnectionCallback = std::function<void(bool connected)>;

/**
 * WHEP Client
 *
 * Connects to a WHEP endpoint (like MediaMTX) and receives
 * video frames via WebRTC with hardware decoding.
 */
class WHEPClient {
public:
    WHEPClient();
    ~WHEPClient();
    
    // Non-copyable
    WHEPClient(const WHEPClient&) = delete;
    WHEPClient& operator=(const WHEPClient&) = delete;
    
    /**
     * Initialize the client
     * @param config Client configuration
     * @return true on success
     */
    bool initialize(const WHEPConfig& config);
    
    /**
     * Start the WebRTC connection
     * @return true on success
     */
    bool connect();
    
    /**
     * Disconnect and cleanup
     */
    void disconnect();
    
    /**
     * Poll for events (call from main thread)
     */
    void poll();
    
    /**
     * Set callback for decoded video frames
     */
    void setFrameCallback(FrameCallback callback);
    
    /**
     * Set callback for connection state changes
     */
    void setConnectionCallback(ConnectionCallback callback);
    
    /**
     * Get current statistics
     */
    WHEPStats getStats() const;
    
    /**
     * Check if connected
     */
    bool isConnected() const;
    
private:
    // Internal implementation
    class Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace native_viewer

#endif // NATIVE_VIEWER_WHEP_CLIENT_H
