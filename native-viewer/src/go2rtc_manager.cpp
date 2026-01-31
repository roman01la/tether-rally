#include "go2rtc_manager.h"
#include <iostream>
#include <fstream>
#include <cstdlib>
#include <cstring>
#include <unistd.h>
#include <signal.h>
#include <sys/wait.h>
#include <thread>
#include <chrono>
#include <fcntl.h>

#ifdef __APPLE__
#include <mach-o/dyld.h>
#include <libgen.h>
#endif

Go2RTCManager::Go2RTCManager() = default;

Go2RTCManager::~Go2RTCManager()
{
    stop();

    // Clean up temp config
    if (!configPath_.empty())
    {
        unlink(configPath_.c_str());
    }
}

std::string Go2RTCManager::findGo2RTCBinary()
{
    char path[4096];

#ifdef __APPLE__
    // On macOS, look in app bundle: AppName.app/Contents/Resources/go2rtc
    uint32_t size = sizeof(path);
    if (_NSGetExecutablePath(path, &size) == 0)
    {
        char *dir = dirname(path);
        std::string bundlePath = std::string(dir) + "/../Resources/go2rtc";
        if (access(bundlePath.c_str(), X_OK) == 0)
        {
            return bundlePath;
        }
        // Also check next to executable (for development)
        std::string devPath = std::string(dir) + "/go2rtc";
        if (access(devPath.c_str(), X_OK) == 0)
        {
            return devPath;
        }
    }
#else
    // On Linux, look next to executable
    ssize_t len = readlink("/proc/self/exe", path, sizeof(path) - 1);
    if (len > 0)
    {
        path[len] = '\0';
        char *dir = dirname(path);
        std::string binPath = std::string(dir) + "/go2rtc";
        if (access(binPath.c_str(), X_OK) == 0)
        {
            return binPath;
        }
    }
#endif

    // Fall back to PATH
    return "go2rtc";
}

bool Go2RTCManager::start(const std::string &whep_url)
{
    if (running_)
    {
        return true;
    }

    std::string go2rtcBin = findGo2RTCBinary();

    // Create temporary config file
    char tmpPath[] = "/tmp/arrma-go2rtc-XXXXXX.yaml";
    int fd = mkstemps(tmpPath, 5);
    if (fd < 0)
    {
        std::cerr << "Failed to create temp config file" << std::endl;
        return false;
    }
    configPath_ = tmpPath;

    // Write config
    std::string config =
        "streams:\n"
        "  cam:\n"
        "    - webrtc:" +
        whep_url + "\n"
                   "\n"
                   "rtsp:\n"
                   "  listen: :8554\n"
                   "\n"
                   "api:\n"
                   "  listen: :1984\n"
                   "\n"
                   "log:\n"
                   "  level: warn\n";

    write(fd, config.c_str(), config.size());
    close(fd);

    std::cout << "Starting go2rtc with WHEP URL: " << whep_url << std::endl;

    // Fork and exec go2rtc
    pid_ = fork();
    if (pid_ < 0)
    {
        std::cerr << "Failed to fork" << std::endl;
        return false;
    }

    if (pid_ == 0)
    {
        // Child process
        // Redirect stdout/stderr to /dev/null for cleaner output
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0)
        {
            dup2(devnull, STDOUT_FILENO);
            dup2(devnull, STDERR_FILENO);
            close(devnull);
        }

        execlp(go2rtcBin.c_str(), "go2rtc", "-c", configPath_.c_str(), nullptr);

        // If exec fails
        std::cerr << "Failed to exec go2rtc: " << strerror(errno) << std::endl;
        _exit(1);
    }

    // Parent process - wait a moment for go2rtc to start
    std::this_thread::sleep_for(std::chrono::milliseconds(500));

    // Check if process is still running
    int status;
    pid_t result = waitpid(pid_, &status, WNOHANG);
    if (result == pid_)
    {
        std::cerr << "go2rtc exited immediately" << std::endl;
        pid_ = -1;
        return false;
    }

    running_ = true;
    std::cout << "go2rtc started (PID: " << pid_ << ")" << std::endl;
    return true;
}

void Go2RTCManager::stop()
{
    if (pid_ > 0 && running_)
    {
        std::cout << "Stopping go2rtc (PID: " << pid_ << ")" << std::endl;

        // Send SIGTERM first
        kill(pid_, SIGTERM);

        // Wait up to 2 seconds for graceful shutdown
        for (int i = 0; i < 20; i++)
        {
            int status;
            pid_t result = waitpid(pid_, &status, WNOHANG);
            if (result == pid_)
            {
                running_ = false;
                pid_ = -1;
                return;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }

        // Force kill if still running
        kill(pid_, SIGKILL);
        waitpid(pid_, nullptr, 0);

        running_ = false;
        pid_ = -1;
    }
}

bool Go2RTCManager::isRunning() const
{
    return running_;
}

std::string Go2RTCManager::getRtspUrl() const
{
    return "rtsp://localhost:8554/cam";
}

bool Go2RTCManager::waitForStream(int timeoutSeconds)
{
    (void)timeoutSeconds; // unused for now

    std::cout << "Waiting for go2rtc to initialize..." << std::endl;

    // Wait for API to be available
    for (int i = 0; i < 40; i++)
    {
        if (!running_)
            return false;

        int result = system("curl -s -o /dev/null -w '' http://localhost:1984/api 2>/dev/null");
        if (result == 0)
        {
            // API is up, give go2rtc time to establish WebRTC connection
            // WebRTC negotiation typically takes 2-5 seconds
            std::cout << "go2rtc ready, establishing WebRTC connection..." << std::endl;
            std::this_thread::sleep_for(std::chrono::seconds(4));
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }

    std::cerr << "go2rtc API not responding" << std::endl;
    return false;
}
