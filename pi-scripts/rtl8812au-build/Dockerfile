FROM --platform=linux/arm64 debian:trixie

ARG KERNEL_VERSION=6.12.62+rpt-rpi-v8

RUN apt-get update && apt-get install -y \
    build-essential \
    bc \
    git \
    kmod \
    flex \
    bison \
    libssl-dev \
    libelf-dev \
    dwarves \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy kernel headers from Pi (must be provided as build context)
COPY kernel-headers.tar.gz /tmp/
RUN tar xzf /tmp/kernel-headers.tar.gz -C /usr/src && rm /tmp/kernel-headers.tar.gz

# Clone the driver (aircrack-ng fork has better kernel support)
RUN git clone --depth 1 https://github.com/aircrack-ng/rtl8812au.git driver

WORKDIR /build/driver

# Build the module
RUN make KVER=${KERNEL_VERSION} KSRC=/usr/src/linux-headers-${KERNEL_VERSION} -j$(nproc)

# Copy built module to output directory
RUN mkdir -p /output && cp 88XXau.ko /output/

CMD ["cp", "-v", "/output/88XXau.ko", "/host/"]
