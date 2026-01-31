/**
 * URL Prompt Dialog - Simple GLFW-based text input dialog
 */

#pragma once

#include <string>
#include <optional>

struct GLFWwindow;

class UrlPromptDialog
{
public:
    /**
     * Show the URL prompt dialog and return the entered URL.
     * Returns nullopt if cancelled.
     */
    static std::optional<std::string> show(const std::string &defaultUrl = "");
};
