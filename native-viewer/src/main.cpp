#include "viewer.h"
#include "config_manager.h"
#include "go2rtc_manager.h"
#include "url_prompt.h"
#include <iostream>
#include <string>
#include <cstring>
#include <csignal>

// Global for signal handling
static Go2RTCManager *g_go2rtc = nullptr;

void signalHandler(int /*signum*/)
{
    if (g_go2rtc)
    {
        g_go2rtc->stop();
    }
    exit(0);
}

void printUsage(const char *program)
{
    std::cout << "Usage: " << program << " [options]" << std::endl;
    std::cout << "Options:" << std::endl;
    std::cout << "  --whep <url>        WHEP stream URL (overrides saved config)" << std::endl;
    std::cout << "  --rtsp <url>        Direct RTSP URL (bypasses go2rtc)" << std::endl;
    std::cout << "  --control <url>     Control relay URL (e.g., https://control.example.com)" << std::endl;
    std::cout << "  --turn <url>        TURN credentials URL (e.g., https://relay.example.com/turn-credentials)" << std::endl;
    std::cout << "  --token <token>     Authentication token for control channel" << std::endl;
    std::cout << "  --reset             Clear saved configuration" << std::endl;
    std::cout << "  --fullscreen        Start in fullscreen mode" << std::endl;
    std::cout << "  --help              Show this help" << std::endl;
    std::cout << std::endl;
    std::cout << "Controls:" << std::endl;
    std::cout << "  F / F11             Toggle fullscreen" << std::endl;
    std::cout << "  S                   Toggle stats overlay" << std::endl;
    std::cout << "  Q / Escape          Quit" << std::endl;
}

int main(int argc, char *argv[])
{
    std::string whepUrl;
    std::string rtspUrl;
    std::string controlUrl;
    std::string turnUrl;
    std::string token;
    bool startFullscreen = false;
    bool resetConfig = false;

    // Parse command line arguments
    for (int i = 1; i < argc; i++)
    {
        if (strcmp(argv[i], "--whep") == 0 && i + 1 < argc)
        {
            whepUrl = argv[++i];
        }
        else if (strcmp(argv[i], "--rtsp") == 0 && i + 1 < argc)
        {
            rtspUrl = argv[++i];
        }
        else if (strcmp(argv[i], "--control") == 0 && i + 1 < argc)
        {
            controlUrl = argv[++i];
        }
        else if (strcmp(argv[i], "--turn") == 0 && i + 1 < argc)
        {
            turnUrl = argv[++i];
        }
        else if (strcmp(argv[i], "--token") == 0 && i + 1 < argc)
        {
            token = argv[++i];
        }
        else if (strcmp(argv[i], "--reset") == 0)
        {
            resetConfig = true;
        }
        else if (strcmp(argv[i], "--fullscreen") == 0)
        {
            startFullscreen = true;
        }
        else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0)
        {
            printUsage(argv[0]);
            return 0;
        }
        else
        {
            std::cerr << "Unknown option: " << argv[i] << std::endl;
            printUsage(argv[0]);
            return 1;
        }
    }

    std::cout << "ARRMA Remote Viewer" << std::endl;

    ConfigManager configManager;
    Go2RTCManager go2rtc;
    g_go2rtc = &go2rtc;

    // Set up signal handlers for clean shutdown
    signal(SIGINT, signalHandler);
    signal(SIGTERM, signalHandler);

    // Determine stream URL
    std::string streamUrl;

    if (!rtspUrl.empty())
    {
        // Direct RTSP mode - skip go2rtc
        streamUrl = rtspUrl;
        std::cout << "Using direct RTSP: " << streamUrl << std::endl;
    }
    else
    {
        // WHEP mode - need go2rtc

        // Load or prompt for WHEP URL
        if (whepUrl.empty() && !resetConfig)
        {
            auto savedConfig = configManager.load();
            if (savedConfig)
            {
                whepUrl = savedConfig->whep_url;
                std::cout << "Loaded saved WHEP URL: " << whepUrl << std::endl;
            }
        }

        if (whepUrl.empty() || resetConfig)
        {
            // Show URL prompt dialog
            auto promptResult = UrlPromptDialog::show(whepUrl);
            if (!promptResult)
            {
                std::cout << "Cancelled" << std::endl;
                return 0;
            }
            whepUrl = *promptResult;

            // Save to config
            AppConfig config;
            config.whep_url = whepUrl;
            if (configManager.save(config))
            {
                std::cout << "Configuration saved to: " << configManager.getConfigPath() << std::endl;
            }
        }

        // Start go2rtc
        std::cout << "Starting go2rtc..." << std::endl;
        if (!go2rtc.start(whepUrl))
        {
            std::cerr << "Failed to start go2rtc" << std::endl;
            std::cerr << "Make sure go2rtc is bundled with the app or available in PATH" << std::endl;
            return 1;
        }

        // Wait for stream to be ready
        if (!go2rtc.waitForStream(15))
        {
            std::cerr << "Failed to establish stream connection" << std::endl;
            go2rtc.stop();
            return 1;
        }

        streamUrl = go2rtc.getRtspUrl();
    }

    std::cout << "Connecting to: " << streamUrl << std::endl;

    Viewer viewer;

    ViewerConfig config;
    config.stream_url = streamUrl;
    config.window_width = 1280;
    config.window_height = 720;
    config.fullscreen = startFullscreen;

    // Set up control channel if we have a control URL and token
    if (!controlUrl.empty() && !token.empty())
    {
        config.control_url = controlUrl;
        config.token = token;
        config.turn_credentials_url = turnUrl;
        std::cout << "Control channel URL: " << config.control_url << std::endl;
        if (!turnUrl.empty())
        {
            std::cout << "TURN credentials URL: " << config.turn_credentials_url << std::endl;
        }
    }
    else if (!token.empty())
    {
        // If no explicit control URL, try to derive from WHEP URL (same host)
        if (!whepUrl.empty())
        {
            size_t schemeEnd = whepUrl.find("://");
            if (schemeEnd != std::string::npos)
            {
                size_t pathStart = whepUrl.find('/', schemeEnd + 3);
                if (pathStart != std::string::npos)
                {
                    config.control_url = whepUrl.substr(0, pathStart);
                }
                else
                {
                    config.control_url = whepUrl;
                }
            }
            config.token = token;
            config.turn_credentials_url = turnUrl;
            std::cout << "Control channel URL (derived from WHEP): " << config.control_url << std::endl;
        }
        else
        {
            std::cout << "Note: Token provided but no control URL - control channel disabled" << std::endl;
        }
    }

    if (!viewer.initialize(config))
    {
        std::cerr << "Failed to initialize viewer" << std::endl;
        go2rtc.stop();
        return 1;
    }

    int result = viewer.run();

    // Clean shutdown
    go2rtc.stop();
    g_go2rtc = nullptr;

    return result;
}
