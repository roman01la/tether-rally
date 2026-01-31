/**
 * Config Manager - Handles app configuration persistence
 */

#pragma once

#include <string>
#include <optional>

struct AppConfig
{
    std::string whep_url;
};

class ConfigManager
{
public:
    ConfigManager();

    /**
     * Load config from disk. Returns nullopt if no config exists.
     */
    std::optional<AppConfig> load();

    /**
     * Save config to disk.
     */
    bool save(const AppConfig &config);

    /**
     * Get the config directory path (creates if needed)
     */
    std::string getConfigDir();

    /**
     * Get the config file path
     */
    std::string getConfigPath();

private:
    std::string configDir_;
    std::string configPath_;
};
