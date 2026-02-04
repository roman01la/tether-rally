/**
 * RTP Receiver over UDPSocket (Direct Sockets API)
 * Parses RTP headers and emits packets for depacketization
 * Tracks statistics: bitrate, packet loss, jitter
 * Supports UDP hole punching for NAT traversal
 */

export class RTPReceiver extends EventTarget {
  constructor(localPort = 5000) {
    super();
    this.localPort = localPort;
    this.socket = null;
    this.readable = null;
    this.writable = null;
    this.writer = null;
    this.running = false;
    this.punching = false;

    // Reorder buffer to handle out-of-order packets
    // Holds packets indexed by sequence number, flushes in order
    this.reorderBuffer = new Map();
    this.reorderTimeout = null;
    this.expectedSeq = -1;
    this.REORDER_WINDOW = 20; // Max packets to buffer (reduced for lower latency)
    this.REORDER_DELAY_MS = 10; // Max time to wait for missing packet (reduced for lower latency)
    this.LATE_THRESHOLD = 10; // Drop packets arriving this far behind expectedSeq

    // Stats tracking
    this.stats = {
      packetsReceived: 0,
      packetsLost: 0,
      bytesReceived: 0,
      lastSequence: -1,
      sequenceWrap: 0, // Track 16-bit sequence wraps

      // Jitter calculation (RFC 3550)
      lastArrivalTime: 0,
      lastRtpTimestamp: 0,
      jitter: 0, // In RTP timestamp units

      // For periodic stats
      periodStart: 0,
      periodPackets: 0,
      periodBytes: 0,
      periodLost: 0,
      highestSequence: -1,
    };
  }

  log(msg) {
    this.dispatchEvent(new CustomEvent("log", { detail: msg }));
  }

  resetStats() {
    this.stats = {
      packetsReceived: 0,
      packetsLost: 0,
      bytesReceived: 0,
      lastSequence: -1,
      highestSequence: -1,
      sequenceWrap: 0,
      lastArrivalTime: 0,
      lastRtpTimestamp: 0,
      jitter: 0,
      periodStart: performance.now(),
      periodPackets: 0,
      periodBytes: 0,
      periodLost: 0,
    };
  }

  // Get current stats snapshot
  getStats() {
    const now = performance.now();
    const periodElapsed = (now - this.stats.periodStart) / 1000;

    const bitrate =
      periodElapsed > 0
        ? (this.stats.periodBytes * 8) / periodElapsed / 1000 // kbps
        : 0;

    const packetRate =
      periodElapsed > 0 ? this.stats.periodPackets / periodElapsed : 0;

    const lossRate =
      this.stats.periodPackets + this.stats.periodLost > 0
        ? (this.stats.periodLost /
            (this.stats.periodPackets + this.stats.periodLost)) *
          100
        : 0;

    // Jitter in milliseconds (assuming 90kHz clock for video)
    const jitterMs = this.stats.jitter / 90;

    return {
      packetsReceived: this.stats.packetsReceived,
      packetsLost: this.stats.packetsLost,
      bytesReceived: this.stats.bytesReceived,
      bitrate: Math.round(bitrate), // kbps
      packetRate: Math.round(packetRate), // pps
      lossPercent: lossRate.toFixed(1),
      jitterMs: jitterMs.toFixed(1),
    };
  }

  // Reset periodic counters (call this after reading stats)
  resetPeriod() {
    this.stats.periodStart = performance.now();
    this.stats.periodPackets = 0;
    this.stats.periodBytes = 0;
    this.stats.periodLost = 0;
  }

  /**
   * Buffer a packet and emit in-order packets to the depacketizer.
   * This handles minor reordering without adding significant latency.
   */
  bufferPacket(packet) {
    const seq = packet.sequenceNumber;

    // Initialize expected sequence on first packet
    if (this.expectedSeq === -1) {
      this.expectedSeq = seq;
    }

    // Check if packet is too late (significantly behind expectedSeq)
    // These packets have already been "skipped" - emitting them now would cause disorder
    const behindBy = (this.expectedSeq - seq) & 0xffff;
    if (behindBy > 0 && behindBy < 0x8000 && behindBy <= this.LATE_THRESHOLD) {
      // Packet is slightly late but within tolerance - still buffer it
      // It might help if we haven't flushed yet
    } else if (behindBy > 0 && behindBy < 0x8000) {
      // Packet is too late - drop it
      this.log(
        `Dropping late packet: seq=${seq}, expected=${this.expectedSeq}, behind=${behindBy}`,
      );
      return;
    }

    // Add to buffer
    this.reorderBuffer.set(seq, packet);

    // Flush all consecutive packets starting from expectedSeq
    this.flushBuffer();

    // If buffer is getting too large, force flush (skip missing packets)
    if (this.reorderBuffer.size > this.REORDER_WINDOW) {
      this.forceFlush();
    }

    // Set timeout to force flush if packet doesn't arrive
    this.scheduleFlush();
  }

  flushBuffer() {
    // Emit packets in sequence order
    while (this.reorderBuffer.has(this.expectedSeq)) {
      const packet = this.reorderBuffer.get(this.expectedSeq);
      this.reorderBuffer.delete(this.expectedSeq);
      this.dispatchEvent(new CustomEvent("packet", { detail: packet }));
      this.expectedSeq = (this.expectedSeq + 1) & 0xffff;
    }
  }

  forceFlush() {
    // Find the minimum sequence in buffer that is AHEAD of expectedSeq
    // Never go backwards - that would cause out-of-order delivery
    if (this.reorderBuffer.size === 0) return;

    let minAheadSeq = null;
    for (const seq of this.reorderBuffer.keys()) {
      // Check if seq is ahead of expectedSeq
      const ahead = (seq - this.expectedSeq) & 0xffff;
      if (ahead > 0 && ahead < 0x8000) {
        // seq is ahead of expectedSeq
        if (minAheadSeq === null) {
          minAheadSeq = seq;
        } else {
          const diff = (minAheadSeq - seq) & 0xffff;
          if (diff > 0 && diff < 0x8000) {
            minAheadSeq = seq; // seq is less than minAheadSeq
          }
        }
      } else if (ahead === 0) {
        // This is expectedSeq - should have been flushed already
        minAheadSeq = seq;
        break;
      }
      // If seq is behind expectedSeq, it's a late packet - ignore it
    }

    if (minAheadSeq !== null) {
      // Skip to the minimum ahead sequence (dropping missing packets)
      const skipped = (minAheadSeq - this.expectedSeq) & 0xffff;
      if (skipped > 1) {
        this.log(
          `Skipping ${skipped - 1} missing packets, jumping to seq ${minAheadSeq}`,
        );
      }
      this.expectedSeq = minAheadSeq;
      this.flushBuffer();
    }

    // Clean up any stale late packets from the buffer
    for (const seq of this.reorderBuffer.keys()) {
      const behind = (this.expectedSeq - seq) & 0xffff;
      if (behind > 0 && behind < 0x8000) {
        this.reorderBuffer.delete(seq);
      }
    }
  }

  scheduleFlush() {
    if (this.reorderTimeout) return;

    this.reorderTimeout = setTimeout(() => {
      this.reorderTimeout = null;
      if (this.reorderBuffer.size > 0) {
        this.forceFlush();
      }
    }, this.REORDER_DELAY_MS);
  }

  // Compare sequence numbers with wrap-around handling
  seqLessThan(a, b) {
    const diff = (b - a) & 0xffff;
    return diff > 0 && diff < 0x8000;
  }

  updateStats(packet, packetSize) {
    const now = performance.now();
    const seq = packet.sequenceNumber;

    this.stats.packetsReceived++;
    this.stats.bytesReceived += packetSize;
    this.stats.periodPackets++;
    this.stats.periodBytes += packetSize;

    // Track highest sequence seen for loss calculation
    // This is more accurate than tracking every gap
    if (this.stats.lastSequence < 0) {
      // First packet
      this.stats.lastSequence = seq;
      this.stats.highestSequence = seq;
    } else {
      // Check if this is a new highest sequence
      const diff = (seq - this.stats.highestSequence) & 0xffff;
      if (diff > 0 && diff < 0x8000) {
        // seq is ahead of highestSequence
        // Gap = how many we skipped (could be reordered, will arrive later)
        const gap = diff - 1;
        if (gap > 0 && gap < 100) {
          // Only count as potential loss if gap is small
          // Large gaps are usually sequence wrap or stream restart
          this.stats.packetsLost += gap;
          this.stats.periodLost += gap;
        }
        this.stats.highestSequence = seq;
      }
      // If seq is behind highestSequence, it's a reordered packet -
      // ideally we'd "un-lose" it, but that's complex.
      // For now, loss is an upper bound estimate.
      this.stats.lastSequence = seq;
    }

    // Jitter calculation (RFC 3550 algorithm)
    if (this.stats.lastArrivalTime > 0 && this.stats.lastRtpTimestamp > 0) {
      // Convert arrival time diff to RTP timestamp units (90kHz for video)
      const arrivalDiff = (now - this.stats.lastArrivalTime) * 90; // ms to 90kHz
      const rtpDiff = packet.timestamp - this.stats.lastRtpTimestamp;

      // Handle timestamp wrap-around for 32-bit RTP timestamps
      const D = Math.abs(arrivalDiff - rtpDiff);

      // Exponential moving average: J = J + (|D| - J) / 16
      this.stats.jitter += (D - this.stats.jitter) / 16;
    }

    this.stats.lastArrivalTime = now;
    this.stats.lastRtpTimestamp = packet.timestamp;
  }

  log(msg) {
    this.dispatchEvent(new CustomEvent("log", { detail: msg }));
  }

  async start(localAddress = "0.0.0.0") {
    this.log(`Opening UDP socket on ${localAddress}:${this.localPort}...`);
    this.resetStats();

    // Bind to specified address to receive RTP packets
    // Use "0.0.0.0" for IPv4, "::1" for IPv6 localhost, "::" for IPv6 any
    // Use smaller buffer sizes for lower latency (tradeoff: may drop packets under load)
    this.socket = new UDPSocket({
      localAddress,
      localPort: this.localPort,
      receiveBufferSize: 65536, // 64KB - enough for ~40 RTP packets
      sendBufferSize: 16384, // 16KB - we don't send much
    });
    const info = await this.socket.opened;

    this.readable = info.readable;
    this.writable = info.writable;
    this.writer = this.writable.getWriter();
    this.log(`UDP socket bound to ${info.localAddress}:${info.localPort}`);

    this.running = true;
    this.receiveLoop();
  }

  /**
   * Start socket binding only, without starting receive loop.
   * Use this when you need to do STUN queries before receiving RTP.
   * Call startReceiveLoop() after STUN is complete.
   */
  async bind(localAddress = "0.0.0.0") {
    this.log(`Binding UDP socket on ${localAddress}:${this.localPort}...`);
    this.resetStats();

    this.socket = new UDPSocket({
      localAddress,
      localPort: this.localPort,
      receiveBufferSize: 65536,
      sendBufferSize: 16384,
    });
    const info = await this.socket.opened;

    this.readable = info.readable;
    this.writable = info.writable;
    this.writer = this.writable.getWriter();
    this.log(`UDP socket bound to ${info.localAddress}:${info.localPort}`);

    return info;
  }

  /**
   * Start the receive loop after bind() and any STUN queries.
   */
  startReceiveLoop() {
    this.running = true;
    this.receiveLoop();
  }

  async receiveLoop() {
    this.reader = this.readable.getReader();

    try {
      while (this.running) {
        const { value, done } = await this.reader.read();
        if (done) {
          this.log("UDP stream ended");
          break;
        }

        const packet = this.parseRTPPacket(value.data);
        if (packet) {
          this.updateStats(packet, value.data.byteLength);
          this.bufferPacket(packet);
        }
      }
    } catch (err) {
      if (this.running) {
        this.log(`Receive error: ${err.message}`);
      }
    } finally {
      try {
        this.reader.releaseLock();
      } catch (e) {
        // Ignore
      }
      this.reader = null;
    }
  }

  parseRTPPacket(data) {
    if (data.byteLength < 12) {
      return null; // Too short for RTP header
    }

    const view = new DataView(data.buffer, data.byteOffset, data.byteLength);

    // Byte 0: V(2) P(1) X(1) CC(4)
    const firstByte = view.getUint8(0);
    const version = (firstByte >> 6) & 0x03;
    const padding = (firstByte >> 5) & 0x01;
    const extension = (firstByte >> 4) & 0x01;
    const csrcCount = firstByte & 0x0f;

    if (version !== 2) {
      return null; // Not RTP
    }

    // Byte 1: M(1) PT(7)
    const secondByte = view.getUint8(1);
    const marker = (secondByte >> 7) & 0x01;
    const payloadType = secondByte & 0x7f;

    // Bytes 2-3: Sequence number
    const sequenceNumber = view.getUint16(2);

    // Bytes 4-7: Timestamp
    const timestamp = view.getUint32(4);

    // Bytes 8-11: SSRC
    const ssrc = view.getUint32(8);

    // Calculate header length
    let headerLength = 12 + csrcCount * 4;

    // Handle extension header
    if (extension) {
      if (data.byteLength < headerLength + 4) return null;
      const extLength = view.getUint16(headerLength + 2);
      headerLength += 4 + extLength * 4;
    }

    // Handle padding
    let payloadLength = data.byteLength - headerLength;
    if (padding) {
      const paddingLength = data[data.byteLength - 1];
      payloadLength -= paddingLength;
    }

    if (payloadLength <= 0) return null;

    const payload = new Uint8Array(
      data.buffer,
      data.byteOffset + headerLength,
      payloadLength,
    );

    return {
      version,
      padding,
      extension,
      marker,
      payloadType,
      sequenceNumber,
      timestamp,
      ssrc,
      payload,
    };
  }

  /**
   * Send hole punch packets to remote endpoint.
   * Call this after getting the remote endpoint from signaling.
   * @param {string} remoteIp - Remote peer's public IP
   * @param {number} remotePort - Remote peer's public port
   * @param {number} count - Number of punch packets to send
   * @param {number} intervalMs - Interval between packets
   */
  async punchHole(remoteIp, remotePort, count = 5, intervalMs = 100) {
    if (!this.writer) {
      throw new Error("Socket not bound - call bind() or start() first");
    }

    this.log(`Punching hole to ${remoteIp}:${remotePort}...`);
    this.punching = true;

    const punchPacket = new Uint8Array([0x00]); // Single null byte

    for (let i = 0; i < count; i++) {
      try {
        await this.writer.write({
          data: punchPacket,
          remoteAddress: remoteIp,
          remotePort: remotePort,
        });
        this.log(`Punch ${i + 1}/${count} sent`);
      } catch (err) {
        this.log(`Punch ${i + 1} failed: ${err.message}`);
      }

      if (i < count - 1) {
        await new Promise((resolve) => setTimeout(resolve, intervalMs));
      }
    }

    this.punching = false;
    this.log("Hole punch complete, waiting for RTP...");
  }

  /**
   * Start punching and receiving simultaneously.
   * Sends punch packets while also listening for incoming RTP.
   * @param {string} remoteIp - Remote peer's public IP
   * @param {number} remotePort - Remote peer's public port
   */
  async startWithPunch(remoteIp, remotePort) {
    if (!this.running) {
      throw new Error(
        "Receive loop not started - call startReceiveLoop() first",
      );
    }

    // Start punching in background (don't await)
    this.punchHole(remoteIp, remotePort, 5, 100);

    // Continue sending periodic punch packets to keep NAT open
    this.keepAliveInterval = setInterval(async () => {
      if (!this.writer || !this.running) {
        clearInterval(this.keepAliveInterval);
        return;
      }
      try {
        await this.writer.write({
          data: new Uint8Array([0x00]),
          remoteAddress: remoteIp,
          remotePort: remotePort,
        });
      } catch (e) {
        // Ignore
      }
    }, 5000); // Every 5 seconds
  }

  async stop() {
    this.log("Stopping RTP receiver...");
    this.running = false;

    // Stop reorder buffer timeout
    if (this.reorderTimeout) {
      clearTimeout(this.reorderTimeout);
      this.reorderTimeout = null;
    }
    this.reorderBuffer.clear();
    this.expectedSeq = -1;

    // Stop keepalive
    if (this.keepAliveInterval) {
      clearInterval(this.keepAliveInterval);
      this.keepAliveInterval = null;
    }

    // Release writer
    if (this.writer) {
      try {
        this.writer.releaseLock();
      } catch (e) {
        // Ignore
      }
      this.writer = null;
    }

    // Cancel and release reader lock first
    if (this.reader) {
      try {
        await this.reader.cancel();
      } catch (e) {
        // Ignore - may already be closed
      }
      try {
        this.reader.releaseLock();
      } catch (e) {
        // Ignore - may already be released
      }
      this.reader = null;
    }

    // Now close socket
    if (this.socket) {
      try {
        await this.socket.close();
        this.log("UDP socket closed");
      } catch (e) {
        // Ignore - may already be closed
      }
      this.socket = null;
    }
  }
}
