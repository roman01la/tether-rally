cmake_minimum_required(VERSION 3.16)
project(native-viewer VERSION 1.0.0 LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Find required packages
find_package(OpenGL REQUIRED)
find_package(PkgConfig REQUIRED)

# GLFW
find_package(glfw3 QUIET)
if(glfw3_FOUND)
    message(STATUS "Found GLFW via find_package")
    set(GLFW_LIBRARIES glfw)
else()
    pkg_check_modules(GLFW REQUIRED glfw3)
endif()

# FFmpeg
pkg_check_modules(AVFORMAT REQUIRED libavformat)
pkg_check_modules(AVCODEC REQUIRED libavcodec)
pkg_check_modules(AVUTIL REQUIRED libavutil)
pkg_check_modules(SWSCALE REQUIRED libswscale)

# macOS-specific configuration
if(APPLE)
    find_library(COCOA_LIBRARY Cocoa REQUIRED)
    find_library(IOKIT_LIBRARY IOKit REQUIRED)
    find_library(COREVIDEO_LIBRARY CoreVideo REQUIRED)
    find_library(VIDEOTOOLBOX_LIBRARY VideoToolbox REQUIRED)
    find_library(COREMEDIA_LIBRARY CoreMedia REQUIRED)
    find_library(COREFOUNDATION_LIBRARY CoreFoundation REQUIRED)
    
    set(PLATFORM_LIBRARIES
        ${COCOA_LIBRARY}
        ${IOKIT_LIBRARY}
        ${COREVIDEO_LIBRARY}
        ${VIDEOTOOLBOX_LIBRARY}
        ${COREMEDIA_LIBRARY}
        ${COREFOUNDATION_LIBRARY}
    )
    
    # Suppress OpenGL deprecation warnings on macOS
    add_definitions(-DGL_SILENCE_DEPRECATION)
elseif(UNIX)
    find_package(Threads REQUIRED)
    set(PLATFORM_LIBRARIES Threads::Threads ${CMAKE_DL_LIBS})
endif()

# Source files
set(SOURCES
    src/main.cpp
    src/viewer.cpp
    src/stream_decoder.cpp
    src/renderer.cpp
    src/config_manager.cpp
    src/url_prompt.cpp
)

set(HEADERS
    src/viewer.h
    src/stream_decoder.h
    src/renderer.h
    src/config_manager.h
    src/url_prompt.h
)

# Executable
add_executable(${PROJECT_NAME} ${SOURCES} ${HEADERS})

target_include_directories(${PROJECT_NAME} PRIVATE
    ${CMAKE_SOURCE_DIR}/src
    ${OPENGL_INCLUDE_DIR}
    ${AVFORMAT_INCLUDE_DIRS}
    ${AVCODEC_INCLUDE_DIRS}
    ${AVUTIL_INCLUDE_DIRS}
    ${SWSCALE_INCLUDE_DIRS}
)

if(TARGET glfw)
    target_link_libraries(${PROJECT_NAME} PRIVATE glfw)
else()
    target_include_directories(${PROJECT_NAME} PRIVATE ${GLFW_INCLUDE_DIRS})
    target_link_directories(${PROJECT_NAME} PRIVATE ${GLFW_LIBRARY_DIRS})
    target_link_libraries(${PROJECT_NAME} PRIVATE ${GLFW_LIBRARIES})
endif()

target_link_directories(${PROJECT_NAME} PRIVATE
    ${AVFORMAT_LIBRARY_DIRS}
    ${AVCODEC_LIBRARY_DIRS}
    ${AVUTIL_LIBRARY_DIRS}
    ${SWSCALE_LIBRARY_DIRS}
)

target_link_libraries(${PROJECT_NAME} PRIVATE
    ${OPENGL_LIBRARIES}
    ${AVFORMAT_LIBRARIES}
    ${AVCODEC_LIBRARIES}
    ${AVUTIL_LIBRARIES}
    ${SWSCALE_LIBRARIES}
    ${PLATFORM_LIBRARIES}
)

# Install
install(TARGETS ${PROJECT_NAME} RUNTIME DESTINATION bin)

# Print configuration summary
message(STATUS "")
message(STATUS "=== Native Viewer Configuration ===")
message(STATUS "Build type: ${CMAKE_BUILD_TYPE}")
message(STATUS "FFmpeg: ${AVFORMAT_VERSION}")
if(APPLE)
    message(STATUS "Platform: macOS (VideoToolbox HW decode)")
else()
    message(STATUS "Platform: Linux")
endif()
message(STATUS "===================================")
