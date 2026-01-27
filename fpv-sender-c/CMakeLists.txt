cmake_minimum_required(VERSION 3.16)
project(fpv-sender C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -Wall -Wextra -O2")

# Check for Raspberry Pi (Linux with V4L2)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    set(ON_LINUX TRUE)
else()
    message(FATAL_ERROR "fpv-sender requires Linux (Raspberry Pi)")
endif()

# Source files - uses rpicam-vid for camera+encoding
set(SOURCES
    src/main2.c
    src/rpicam.c
    src/sender.c
    src/protocol.c
    src/stun.c
)

# Create executable
add_executable(fpv-sender ${SOURCES})

# Include directories
target_include_directories(fpv-sender PRIVATE
    ${CMAKE_SOURCE_DIR}/src
)

# Link libraries
target_link_libraries(fpv-sender
    pthread
)

# Installation
install(TARGETS fpv-sender DESTINATION bin)
