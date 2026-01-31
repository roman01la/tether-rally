cmake_minimum_required(VERSION 3.16)
project(native-viewer VERSION 1.0.0 LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Options
option(USE_HARDWARE_DECODE "Enable hardware video decoding" ON)
option(USE_VIDEOTOOLBOX "Use VideoToolbox for hardware decoding (macOS)" ON)

# Find required packages
find_package(OpenGL REQUIRED)
find_package(CURL REQUIRED)

# GLFW - try find_package first (Homebrew), then pkg-config
find_package(glfw3 QUIET)
if(glfw3_FOUND)
    message(STATUS "Found GLFW via find_package")
    set(GLFW_LIBRARIES glfw)
else()
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(GLFW REQUIRED glfw3)
endif()

# libwebrtc
if(DEFINED LIBWEBRTC_DIR)
    message(STATUS "Using libwebrtc from: ${LIBWEBRTC_DIR}")
    set(WEBRTC_INCLUDE_DIRS "${LIBWEBRTC_DIR}/include")
    set(WEBRTC_LIBRARY_DIRS "${LIBWEBRTC_DIR}/lib")
    set(WEBRTC_LIBRARIES "${LIBWEBRTC_DIR}/lib/libwebrtc.a")
else()
    message(STATUS "libwebrtc not specified. Building stub version for development...")
    message(STATUS "For full functionality, set -DLIBWEBRTC_DIR=/path/to/libwebrtc")
    set(BUILD_STUB ON)
endif()

# macOS-specific configuration (primary target)
if(APPLE)
    find_library(COCOA_LIBRARY Cocoa REQUIRED)
    find_library(IOKIT_LIBRARY IOKit REQUIRED)
    find_library(COREVIDEO_LIBRARY CoreVideo REQUIRED)
    find_library(COREMEDIA_LIBRARY CoreMedia REQUIRED)
    find_library(VIDEOTOOLBOX_LIBRARY VideoToolbox REQUIRED)
    find_library(COREFOUNDATION_LIBRARY CoreFoundation REQUIRED)
    find_library(SECURITY_LIBRARY Security REQUIRED)
    find_library(AUDIOTOOLBOX_LIBRARY AudioToolbox REQUIRED)
    
    set(PLATFORM_LIBRARIES
        ${COCOA_LIBRARY}
        ${IOKIT_LIBRARY}
        ${COREVIDEO_LIBRARY}
        ${COREMEDIA_LIBRARY}
        ${VIDEOTOOLBOX_LIBRARY}
        ${COREFOUNDATION_LIBRARY}
        ${SECURITY_LIBRARY}
        ${AUDIOTOOLBOX_LIBRARY}
    )
    
    add_definitions(-DWEBRTC_MAC -DWEBRTC_POSIX)
    
    # Suppress OpenGL deprecation warnings on macOS
    add_definitions(-DGL_SILENCE_DEPRECATION)
elseif(UNIX)
    # Linux (secondary target)
    find_package(Threads REQUIRED)
    find_package(PkgConfig REQUIRED)
    
    set(PLATFORM_LIBRARIES
        Threads::Threads
        ${CMAKE_DL_LIBS}
    )
    
    add_definitions(-DWEBRTC_LINUX -DWEBRTC_POSIX)
endif()

# Source files
set(SOURCES
    src/main.cpp
    src/viewer.cpp
    src/whep_client.cpp
    src/renderer.cpp
)

set(HEADERS
    src/viewer.h
    src/whep_client.h
    src/renderer.h
)

# Executable
add_executable(${PROJECT_NAME} ${SOURCES} ${HEADERS})

target_include_directories(${PROJECT_NAME} PRIVATE
    ${CMAKE_SOURCE_DIR}/src
    ${OPENGL_INCLUDE_DIR}
    ${CURL_INCLUDE_DIRS}
)

if(TARGET glfw)
    # Using find_package result
    target_link_libraries(${PROJECT_NAME} PRIVATE glfw)
else()
    # Using pkg-config result
    target_include_directories(${PROJECT_NAME} PRIVATE ${GLFW_INCLUDE_DIRS})
    target_link_directories(${PROJECT_NAME} PRIVATE ${GLFW_LIBRARY_DIRS})
    target_link_libraries(${PROJECT_NAME} PRIVATE ${GLFW_LIBRARIES})
endif()

if(DEFINED WEBRTC_INCLUDE_DIRS)
    target_include_directories(${PROJECT_NAME} PRIVATE ${WEBRTC_INCLUDE_DIRS})
endif()

if(BUILD_STUB)
    target_compile_definitions(${PROJECT_NAME} PRIVATE BUILD_STUB)
endif()

if(DEFINED WEBRTC_LIBRARY_DIRS)
    target_link_directories(${PROJECT_NAME} PRIVATE ${WEBRTC_LIBRARY_DIRS})
endif()

target_link_libraries(${PROJECT_NAME} PRIVATE
    ${OPENGL_LIBRARIES}
    CURL::libcurl
    ${PLATFORM_LIBRARIES}
)

if(NOT BUILD_STUB AND DEFINED WEBRTC_LIBRARIES)
    target_link_libraries(${PROJECT_NAME} PRIVATE ${WEBRTC_LIBRARIES})
endif()

# Install
install(TARGETS ${PROJECT_NAME}
    RUNTIME DESTINATION bin
)

# Print configuration summary
message(STATUS "")
message(STATUS "=== Native Viewer Configuration ===")
message(STATUS "Build type: ${CMAKE_BUILD_TYPE}")
message(STATUS "C++ Standard: ${CMAKE_CXX_STANDARD}")
message(STATUS "Hardware decode: ${USE_HARDWARE_DECODE}")
if(APPLE)
    message(STATUS "Platform: macOS")
    message(STATUS "VideoToolbox: Available")
elseif(UNIX)
    message(STATUS "Platform: Linux")
endif()
if(BUILD_STUB)
    message(STATUS "Mode: STUB (libwebrtc not found)")
    message(STATUS "      Set -DLIBWEBRTC_DIR=/path/to/libwebrtc for full functionality")
else()
    message(STATUS "Mode: FULL (libwebrtc found)")
endif()
message(STATUS "===================================")
message(STATUS "")
