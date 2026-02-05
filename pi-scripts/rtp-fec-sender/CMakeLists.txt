cmake_minimum_required(VERSION 3.10)
project(rtp-fec-sender C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -Wall -Wextra -O3")

# Find GStreamer
find_package(PkgConfig REQUIRED)
pkg_check_modules(GST REQUIRED gstreamer-1.0 gstreamer-app-1.0)

# Include directories
include_directories(${GST_INCLUDE_DIRS})
link_directories(${GST_LIBRARY_DIRS})

# Build executable
add_executable(rtp-fec-sender
    main.c
    fec.c
)

target_link_libraries(rtp-fec-sender ${GST_LIBRARIES})

# Install
install(TARGETS rtp-fec-sender DESTINATION bin)
