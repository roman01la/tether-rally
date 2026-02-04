#include "config_manager.h"
#include <fstream>
#include <sstream>
#include <sys/stat.h>
#include <cstdlib>

#ifdef __APPLE__
#include <pwd.h>
#include <unistd.h>
#endif

ConfigManager::ConfigManager()
{
#ifdef __APPLE__
    const char *home = getenv("HOME");
    if (!home)
    {
        struct passwd *pw = getpwuid(getuid());
        home = pw->pw_dir;
    }
    configDir_ = std::string(home) + "/Library/Application Support/ARRMA Viewer";
#else
    const char *xdgConfig = getenv("XDG_CONFIG_HOME");
    if (xdgConfig)
    {
        configDir_ = std::string(xdgConfig) + "/arrma-viewer";
    }
    else
    {
        const char *home = getenv("HOME");
        configDir_ = std::string(home) + "/.config/arrma-viewer";
    }
#endif
    configPath_ = configDir_ + "/config.json";
}

std::string ConfigManager::getConfigDir()
{
    return configDir_;
}

std::string ConfigManager::getConfigPath()
{
    return configPath_;
}

std::optional<AppConfig> ConfigManager::load()
{
    std::ifstream file(configPath_);
    if (!file.is_open())
    {
        return std::nullopt;
    }

    std::stringstream buffer;
    buffer << file.rdbuf();
    std::string content = buffer.str();

    // Simple JSON parsing for {"rtsp_url": "..."}
    size_t urlStart = content.find("\"rtsp_url\"");
    if (urlStart == std::string::npos)
    {
        return std::nullopt;
    }

    size_t colonPos = content.find(':', urlStart);
    if (colonPos == std::string::npos)
    {
        return std::nullopt;
    }

    size_t firstQuote = content.find('"', colonPos);
    if (firstQuote == std::string::npos)
    {
        return std::nullopt;
    }

    size_t secondQuote = content.find('"', firstQuote + 1);
    if (secondQuote == std::string::npos)
    {
        return std::nullopt;
    }

    AppConfig config;
    config.rtsp_url = content.substr(firstQuote + 1, secondQuote - firstQuote - 1);

    return config;
}

bool ConfigManager::save(const AppConfig &config)
{
    // Create config directory if it doesn't exist
    mkdir(configDir_.c_str(), 0755);

    std::ofstream file(configPath_);
    if (!file.is_open())
    {
        return false;
    }

    // Simple JSON output
    file << "{\n";
    file << "  \"rtsp_url\": \"" << config.rtsp_url << "\"\n";
    file << "}\n";

    return file.good();
}
