FROM golang:1.21-alpine AS builder

WORKDIR /build
COPY go.mod go.sum* ./
RUN go mod download 2>/dev/null || true
COPY main.go ./
RUN go build -o restreamer .

FROM alpine:3.19

# Install MediaMTX, FFmpeg, and fonts for telemetry overlay
RUN apk add --no-cache ffmpeg curl ttf-dejavu

# Download MediaMTX (latest version with better WHEP client support)
ARG MEDIAMTX_VERSION=1.15.6
RUN curl -L https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_amd64.tar.gz | tar xz -C /usr/local/bin

# Copy Go binary
COPY --from=builder /build/restreamer /usr/local/bin/

# HTTP control port
EXPOSE 8080

CMD ["restreamer"]
