#include "control_channel.h"
#include <rtc/rtc.hpp>
#include <iostream>
#include <chrono>
#include <thread>
#include <cstring>
#include <sstream>

// Protocol constants (matching web app)
constexpr uint8_t CMD_PING = 0x00;
constexpr uint8_t CMD_PONG = 0x02;

// Timing constants
constexpr int PING_INTERVAL_MS = 200;

// Simple JSON value extraction (no external library)
static std::string extractJsonString(const std::string &json, const std::string &key)
{
    std::string searchKey = "\"" + key + "\"";
    size_t keyPos = json.find(searchKey);
    if (keyPos == std::string::npos)
        return "";

    size_t colonPos = json.find(':', keyPos + searchKey.length());
    if (colonPos == std::string::npos)
        return "";

    size_t quoteStart = json.find('"', colonPos + 1);
    if (quoteStart == std::string::npos)
        return "";

    size_t quoteEnd = json.find('"', quoteStart + 1);
    if (quoteEnd == std::string::npos)
        return "";

    return json.substr(quoteStart + 1, quoteEnd - quoteStart - 1);
}

ControlChannel::ControlChannel() = default;

ControlChannel::~ControlChannel()
{
    disconnect();
}

bool ControlChannel::connect(const std::string &controlUrl, const std::string &token, const std::string &turnCredentialsUrl)
{
    disconnect();

    try
    {
        // Configure ICE servers
        rtc::Configuration config;

        // Try to fetch TURN credentials if URL provided
        if (!turnCredentialsUrl.empty())
        {
            // Append token to TURN credentials URL (required for authentication)
            std::string turnUrlWithToken = turnCredentialsUrl;
            if (turnUrlWithToken.find('?') != std::string::npos)
            {
                turnUrlWithToken += "&token=" + token;
            }
            else
            {
                turnUrlWithToken += "?token=" + token;
            }
            std::cout << "Fetching TURN credentials from: " << turnCredentialsUrl << std::endl;

            std::string curlCmd = "curl -s '" + turnUrlWithToken + "'";
            FILE *pipe = popen(curlCmd.c_str(), "r");
            if (pipe)
            {
                std::string turnJson;
                char buffer[4096];
                while (fgets(buffer, sizeof(buffer), pipe))
                {
                    turnJson += buffer;
                }
                pclose(pipe);

                // Parse the JSON response
                // Format: {"iceServers":[{"urls":["turn:..."],"username":"...","credential":"..."}]}
                if (turnJson.find("iceServers") != std::string::npos)
                {
                    std::string username = extractJsonString(turnJson, "username");
                    std::string credential = extractJsonString(turnJson, "credential");

                    // Find all TURN URLs and add them
                    // Look for turn:turn.cloudflare.com:3478 pattern
                    size_t pos = 0;
                    while ((pos = turnJson.find("turn:", pos)) != std::string::npos)
                    {
                        // Check it's not "turns:" by looking at position before
                        if (pos > 0 && turnJson[pos - 1] == 's')
                        {
                            pos++;
                            continue;
                        }

                        // Find the end of this URL (next quote)
                        size_t urlStart = pos;
                        size_t urlEnd = turnJson.find('"', urlStart);
                        if (urlEnd == std::string::npos)
                            break;

                        std::string turnUrl = turnJson.substr(urlStart, urlEnd - urlStart);

                        // Parse hostname and port from URL like "turn:turn.cloudflare.com:3478?transport=udp"
                        // Skip "turn:" prefix
                        std::string hostPort = turnUrl.substr(5);
                        size_t queryPos = hostPort.find('?');
                        if (queryPos != std::string::npos)
                        {
                            hostPort = hostPort.substr(0, queryPos);
                        }

                        size_t colonPos = hostPort.rfind(':');
                        if (colonPos != std::string::npos && !username.empty() && !credential.empty())
                        {
                            std::string hostname = hostPort.substr(0, colonPos);
                            uint16_t port = static_cast<uint16_t>(std::stoi(hostPort.substr(colonPos + 1)));

                            std::cout << "Adding TURN server (UDP): " << hostname << ":" << port << std::endl;
                            config.iceServers.emplace_back(hostname, port, username, credential,
                                                           rtc::IceServer::RelayType::TurnUdp);

                            // Also add TCP TURN as fallback (browsers often use this)
                            std::cout << "Adding TURN server (TCP): " << hostname << ":" << port << std::endl;
                            config.iceServers.emplace_back(hostname, port, username, credential,
                                                           rtc::IceServer::RelayType::TurnTcp);
                            break;
                        }

                        pos = urlEnd;
                    }
                }
            }
        }

        // Always add Cloudflare STUN (same as used for video)
        config.iceServers.emplace_back("stun:stun.cloudflare.com:3478");

        // Create peer connection
        pc_ = std::make_shared<rtc::PeerConnection>(config);

        pc_->onStateChange([this](rtc::PeerConnection::State state)
                           {
            const char* stateNames[] = {"New", "Connecting", "Connected", "Disconnected", "Failed", "Closed"};
            std::cout << "Control PeerConnection state: " << stateNames[static_cast<int>(state)] << std::endl;
            if (state == rtc::PeerConnection::State::Connected) {
                std::cout << "Control channel ICE connected!" << std::endl;
            } else if (state == rtc::PeerConnection::State::Failed) {
                std::cout << "Control channel ICE connection failed" << std::endl;
                connected_ = false;
            } else if (state == rtc::PeerConnection::State::Disconnected ||
                       state == rtc::PeerConnection::State::Closed) {
                connected_ = false;
            } });

        pc_->onGatheringStateChange([](rtc::PeerConnection::GatheringState state)
                                    { std::cout << "ICE gathering state: " << static_cast<int>(state) << std::endl; });

        pc_->onLocalCandidate([](rtc::Candidate candidate)
                              { std::cout << "Local ICE candidate: " << std::string(candidate) << std::endl; });

        // Create unreliable, unordered DataChannel (UDP-like)
        rtc::DataChannelInit dcInit;
        dcInit.reliability.unordered = true;
        dcInit.reliability.maxRetransmits = 0;

        dc_ = pc_->createDataChannel("control", dcInit);

        dc_->onOpen([this]()
                    {
            std::cout << "Control DataChannel open" << std::endl;
            connected_ = true;
            running_ = true;
            
            // Start ping thread
            pingThread_ = std::thread(&ControlChannel::pingLoop, this); });

        dc_->onClosed([this]()
                      {
            std::cout << "Control DataChannel closed" << std::endl;
            connected_ = false;
            running_ = false; });

        dc_->onError([](std::string error)
                     { std::cout << "Control DataChannel error: " << error << std::endl; });

        dc_->onMessage([this](rtc::message_variant message)
                       {
            if (std::holds_alternative<rtc::binary>(message)) {
                auto& data = std::get<rtc::binary>(message);
                onMessage(reinterpret_cast<const std::byte*>(data.data()), data.size());
            } });

        // Generate local description
        pc_->setLocalDescription();

        // Wait for ICE gathering to complete (or timeout)
        auto start = std::chrono::steady_clock::now();
        while (pc_->gatheringState() != rtc::PeerConnection::GatheringState::Complete)
        {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            auto elapsed = std::chrono::steady_clock::now() - start;
            if (elapsed > std::chrono::seconds(5))
            {
                std::cout << "ICE gathering timeout, proceeding..." << std::endl;
                break;
            }
        }

        auto localDesc = pc_->localDescription();
        if (!localDesc)
        {
            std::cerr << "No local description generated" << std::endl;
            return false;
        }

        std::string offerSdp = std::string(*localDesc);
        std::cout << "Generated offer, sending to relay..." << std::endl;

        // POST offer to the relay
        // Build URL: controlUrl/control/offer?token=...
        std::string url = controlUrl + "/control/offer?token=" + token;

        // Use libcurl for HTTP
        // For simplicity, let's use a system call to curl
        // In production, you'd want to use libcurl directly
        std::string curlCmd = "curl -s -X POST -H 'Content-Type: application/sdp' -d '" +
                              offerSdp + "' '" + url + "'";

        FILE *pipe = popen(curlCmd.c_str(), "r");
        if (!pipe)
        {
            std::cerr << "Failed to execute curl" << std::endl;
            return false;
        }

        std::string answerSdp;
        char buffer[4096];
        while (fgets(buffer, sizeof(buffer), pipe))
        {
            answerSdp += buffer;
        }
        int exitCode = pclose(pipe);

        if (exitCode != 0 || answerSdp.empty())
        {
            std::cerr << "Signaling failed: " << answerSdp << std::endl;
            return false;
        }

        std::cout << "Received answer, setting remote description..." << std::endl;

        // Set remote description
        rtc::Description answer(answerSdp, rtc::Description::Type::Answer);
        pc_->setRemoteDescription(answer);

        std::cout << "Control channel signaling complete" << std::endl;
        return true;
    }
    catch (const std::exception &e)
    {
        std::cerr << "Control channel error: " << e.what() << std::endl;
        return false;
    }
}

void ControlChannel::disconnect()
{
    running_ = false;

    if (pingThread_.joinable())
    {
        pingThread_.join();
    }

    if (dc_)
    {
        dc_->close();
        dc_.reset();
    }

    if (pc_)
    {
        pc_->close();
        pc_.reset();
    }

    connected_ = false;
    latency_ = 0;
}

void ControlChannel::setLatencyCallback(LatencyCallback callback)
{
    std::lock_guard<std::mutex> lock(callbackMutex_);
    latencyCallback_ = callback;
}

void ControlChannel::sendPing()
{
    if (!dc_ || !dc_->isOpen())
        return;

    // Packet: seq(2) + cmd(1) + timestamp(4) = 7 bytes
    uint8_t buf[7];
    seq_ = (seq_ + 1) & 0xFFFF;

    // Little-endian seq
    buf[0] = seq_ & 0xFF;
    buf[1] = (seq_ >> 8) & 0xFF;

    // Command
    buf[2] = CMD_PING;

    // Timestamp (lower 32 bits of milliseconds)
    auto now = std::chrono::steady_clock::now();
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()).count();
    uint32_t timestamp = static_cast<uint32_t>(ms & 0xFFFFFFFF);

    buf[3] = timestamp & 0xFF;
    buf[4] = (timestamp >> 8) & 0xFF;
    buf[5] = (timestamp >> 16) & 0xFF;
    buf[6] = (timestamp >> 24) & 0xFF;

    dc_->send(reinterpret_cast<std::byte *>(buf), sizeof(buf));
}

void ControlChannel::onMessage(const std::byte *data, size_t size)
{
    if (size < 3)
        return;

    const uint8_t *buf = reinterpret_cast<const uint8_t *>(data);
    uint8_t cmd = buf[2];

    // Handle PONG: seq(2) + cmd(1) + timestamp(4) = 7 bytes
    if (cmd == CMD_PONG && size >= 7)
    {
        uint32_t sentTime = buf[3] | (buf[4] << 8) | (buf[5] << 16) | (buf[6] << 24);

        auto now = std::chrono::steady_clock::now();
        auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()).count();
        uint32_t nowTimestamp = static_cast<uint32_t>(ms & 0xFFFFFFFF);

        int64_t rtt = nowTimestamp - sentTime;
        if (rtt < 0)
            rtt += 0x100000000LL; // Handle wraparound

        double oneWay = rtt / 2.0;

        // Exponential smoothing
        double currentLatency = latency_.load();
        if (currentLatency == 0)
        {
            latency_ = oneWay;
        }
        else
        {
            latency_ = currentLatency * 0.9 + oneWay * 0.1;
        }

        // Notify callback
        {
            std::lock_guard<std::mutex> lock(callbackMutex_);
            if (latencyCallback_)
            {
                latencyCallback_(latency_.load());
            }
        }
    }
}

void ControlChannel::pingLoop()
{
    while (running_)
    {
        sendPing();
        std::this_thread::sleep_for(std::chrono::milliseconds(PING_INTERVAL_MS));
    }
}
