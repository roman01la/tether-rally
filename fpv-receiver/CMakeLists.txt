cmake_minimum_required(VERSION 3.16)
project(fpv-receiver C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# Find packages
find_package(PkgConfig REQUIRED)
pkg_check_modules(GLFW3 REQUIRED glfw3)

# Platform-specific
if(APPLE)
    find_library(COCOA_FRAMEWORK Cocoa)
    find_library(IOKIT_FRAMEWORK IOKit)
    find_library(COREVIDEO_FRAMEWORK CoreVideo)
    find_library(VIDEOTOOLBOX_FRAMEWORK VideoToolbox)
    find_library(COREMEDIA_FRAMEWORK CoreMedia)
    find_library(COREFOUNDATION_FRAMEWORK CoreFoundation)
    find_library(OPENGL_FRAMEWORK OpenGL)
    find_library(ACCELERATE_FRAMEWORK Accelerate)
    
    set(PLATFORM_LIBS
        ${COCOA_FRAMEWORK}
        ${IOKIT_FRAMEWORK}
        ${COREVIDEO_FRAMEWORK}
        ${VIDEOTOOLBOX_FRAMEWORK}
        ${COREMEDIA_FRAMEWORK}
        ${COREFOUNDATION_FRAMEWORK}
        ${OPENGL_FRAMEWORK}
        ${ACCELERATE_FRAMEWORK}
    )
    set(PLATFORM_SOURCES
        src/decoder_videotoolbox.c
    )
elseif(WIN32)
    set(PLATFORM_LIBS opengl32)
    set(PLATFORM_SOURCES
        src/decoder_mf.c
    )
else()
    find_package(OpenGL REQUIRED)
    set(PLATFORM_LIBS ${OPENGL_LIBRARIES})
    set(PLATFORM_SOURCES
        src/decoder_ffmpeg.c
    )
endif()

# Sources
set(SOURCES
    src/main.c
    src/protocol.c
    src/receiver.c
    src/assembler.c
    src/renderer.c
    src/stun.c
    ${PLATFORM_SOURCES}
)

# Executable
add_executable(fpv-receiver ${SOURCES})

target_include_directories(fpv-receiver PRIVATE
    ${GLFW3_INCLUDE_DIRS}
    src
)

target_link_directories(fpv-receiver PRIVATE
    ${GLFW3_LIBRARY_DIRS}
)

target_link_libraries(fpv-receiver
    ${GLFW3_LIBRARIES}
    ${PLATFORM_LIBS}
)

# Compiler flags
if(APPLE)
    target_compile_options(fpv-receiver PRIVATE -Wall -Wextra -O2)
elseif(MSVC)
    target_compile_options(fpv-receiver PRIVATE /W4 /O2)
else()
    target_compile_options(fpv-receiver PRIVATE -Wall -Wextra -O2)
endif()
