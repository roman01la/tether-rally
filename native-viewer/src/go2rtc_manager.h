/**
 * Go2RTC Process Manager - Spawns and manages go2rtc subprocess
 */

#pragma once

#include <string>
#include <atomic>

class Go2RTCManager
{
public:
    Go2RTCManager();
    ~Go2RTCManager();

    /**
     * Start go2rtc with the given WHEP URL.
     * Creates a temporary config and spawns the process.
     * Returns true if started successfully.
     */
    bool start(const std::string &whep_url);

    /**
     * Stop the go2rtc process.
     */
    void stop();

    /**
     * Check if go2rtc is running.
     */
    bool isRunning() const;

    /**
     * Get the RTSP URL to connect to.
     */
    std::string getRtspUrl() const;

    /**
     * Wait for stream to be ready (polls API).
     * Returns true if stream becomes available within timeout.
     */
    bool waitForStream(int timeoutSeconds = 10);

    /**
     * Get the path to the bundled go2rtc binary.
     * Looks in app bundle on macOS, next to executable on Linux.
     */
    static std::string findGo2RTCBinary();

private:
    pid_t pid_ = -1;
    std::string configPath_;
    std::atomic<bool> running_{false};
};
