/**
 * WebRTC DataChannel for control and latency measurement
 */

#pragma once

#include <string>
#include <functional>
#include <atomic>
#include <memory>
#include <mutex>
#include <thread>

namespace rtc
{
    class PeerConnection;
    class DataChannel;
}

class ControlChannel
{
public:
    using LatencyCallback = std::function<void(double latencyMs)>;

    ControlChannel();
    ~ControlChannel();

    // Connect to the control relay
    // controlUrl: base URL for WebRTC signaling (e.g., https://control.example.com)
    // token: authentication token
    // turnCredentialsUrl: URL to fetch TURN credentials (e.g., https://relay.example.com/turn-credentials)
    bool connect(const std::string &controlUrl, const std::string &token, const std::string &turnCredentialsUrl = "");

    // Disconnect from the relay
    void disconnect();

    // Check if connected
    bool isConnected() const { return connected_; }

    // Get current latency in milliseconds (smoothed)
    double getLatency() const { return latency_; }

    // Set callback for latency updates
    void setLatencyCallback(LatencyCallback callback);

private:
    void sendPing();
    void onMessage(const std::byte *data, size_t size);
    void pingLoop();

    std::shared_ptr<rtc::PeerConnection> pc_;
    std::shared_ptr<rtc::DataChannel> dc_;

    std::atomic<bool> connected_{false};
    std::atomic<bool> running_{false};
    std::atomic<double> latency_{0};

    LatencyCallback latencyCallback_;
    std::mutex callbackMutex_;

    std::thread pingThread_;
    uint16_t seq_ = 0;
};
