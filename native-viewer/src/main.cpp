/**
 * Native WebRTC Viewer - Main Entry Point
 *
 * A low-latency video viewer using GLFW and libwebrtc.
 * Connects to a MediaMTX WHEP endpoint and displays the video stream
 * with minimal latency using hardware decoding.
 */

#include "viewer.h"

#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>

struct Config {
    std::string whep_url;
    std::string turn_url;
    std::string turn_user;
    std::string turn_pass;
    int width = 1280;
    int height = 720;
    bool fullscreen = false;
};

void print_usage(const char* program) {
    std::cerr << "Usage: " << program << " --url <WHEP_URL> [options]\n"
              << "\nRequired:\n"
              << "  --url <url>       WHEP endpoint URL\n"
              << "\nOptions:\n"
              << "  --turn-url <url>  TURN server URL\n"
              << "  --turn-user <u>   TURN username\n"
              << "  --turn-pass <p>   TURN password\n"
              << "  --width <w>       Window width (default: 1280)\n"
              << "  --height <h>      Window height (default: 720)\n"
              << "  --fullscreen      Start in fullscreen mode\n"
              << "  --help            Show this help\n"
              << "\nExample:\n"
              << "  " << program << " --url http://localhost:8889/cam/whep\n"
              << "  " << program << " --url https://cam.example.com/cam/whep --turn-url turn:turn.example.com:3478\n";
}

bool parse_args(int argc, char* argv[], Config& config) {
    for (int i = 1; i < argc; i++) {
        std::string arg = argv[i];
        
        if (arg == "--help" || arg == "-h") {
            print_usage(argv[0]);
            return false;
        } else if (arg == "--url" && i + 1 < argc) {
            config.whep_url = argv[++i];
        } else if (arg == "--turn-url" && i + 1 < argc) {
            config.turn_url = argv[++i];
        } else if (arg == "--turn-user" && i + 1 < argc) {
            config.turn_user = argv[++i];
        } else if (arg == "--turn-pass" && i + 1 < argc) {
            config.turn_pass = argv[++i];
        } else if (arg == "--width" && i + 1 < argc) {
            config.width = std::atoi(argv[++i]);
        } else if (arg == "--height" && i + 1 < argc) {
            config.height = std::atoi(argv[++i]);
        } else if (arg == "--fullscreen") {
            config.fullscreen = true;
        } else {
            std::cerr << "Unknown option: " << arg << "\n";
            print_usage(argv[0]);
            return false;
        }
    }
    
    if (config.whep_url.empty()) {
        std::cerr << "Error: --url is required\n\n";
        print_usage(argv[0]);
        return false;
    }
    
    return true;
}

int main(int argc, char* argv[]) {
    Config config;
    
    if (!parse_args(argc, argv, config)) {
        return 1;
    }
    
    std::cout << "Native WebRTC Viewer\n"
              << "====================\n"
              << "WHEP URL: " << config.whep_url << "\n";
    
    if (!config.turn_url.empty()) {
        std::cout << "TURN URL: " << config.turn_url << "\n";
    }
    
    std::cout << "Window: " << config.width << "x" << config.height;
    if (config.fullscreen) {
        std::cout << " (fullscreen)";
    }
    std::cout << "\n\n";
    
    // Create and run the viewer
    native_viewer::Viewer viewer;
    
    native_viewer::ViewerConfig viewer_config{};
    viewer_config.whep_url = config.whep_url;
    viewer_config.turn_url = config.turn_url;
    viewer_config.turn_user = config.turn_user;
    viewer_config.turn_pass = config.turn_pass;
    viewer_config.window_width = config.width;
    viewer_config.window_height = config.height;
    viewer_config.fullscreen = config.fullscreen;
    
    if (!viewer.initialize(viewer_config)) {
        std::cerr << "Failed to initialize viewer\n";
        return 1;
    }
    
    return viewer.run();
}
