#include "viewer.h"
#include "config_manager.h"
#include "url_prompt.h"
#include <iostream>
#include <string>
#include <cstring>

void printUsage(const char *program)
{
    std::cout << "Usage: " << program << " [options]" << std::endl;
    std::cout << "Options:" << std::endl;
    std::cout << "  --rtsp <url>        RTSP stream URL (e.g., rtsp://192.168.0.24:8554/cam)" << std::endl;
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
    std::string rtspUrl;
    bool startFullscreen = false;
    bool resetConfig = false;

    // Parse command line arguments
    for (int i = 1; i < argc; i++)
    {
        if (strcmp(argv[i], "--rtsp") == 0 && i + 1 < argc)
        {
            rtspUrl = argv[++i];
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

    // Load or prompt for RTSP URL
    if (rtspUrl.empty() && !resetConfig)
    {
        auto savedConfig = configManager.load();
        if (savedConfig)
        {
            rtspUrl = savedConfig->rtsp_url;
            std::cout << "Loaded saved RTSP URL: " << rtspUrl << std::endl;
        }
    }

    if (rtspUrl.empty() || resetConfig)
    {
        // Show URL prompt dialog
        auto promptResult = UrlPromptDialog::show(rtspUrl);
        if (!promptResult)
        {
            std::cout << "Cancelled" << std::endl;
            return 0;
        }
        rtspUrl = *promptResult;

        // Save to config
        AppConfig config;
        config.rtsp_url = rtspUrl;
        if (configManager.save(config))
        {
            std::cout << "Configuration saved to: " << configManager.getConfigPath() << std::endl;
        }
    }

    std::cout << "Connecting to: " << rtspUrl << std::endl;

    Viewer viewer;

    ViewerConfig config;
    config.stream_url = rtspUrl;
    config.window_width = 1280;
    config.window_height = 720;
    config.fullscreen = startFullscreen;

    if (!viewer.initialize(config))
    {
        std::cerr << "Failed to initialize viewer" << std::endl;
        return 1;
    }

    return viewer.run();
}
