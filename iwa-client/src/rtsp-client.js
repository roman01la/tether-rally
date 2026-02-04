/**
 * RTSP Client over TCPSocket (Direct Sockets API)
 * Implements DESCRIBE → SETUP → PLAY flow for H.264/RTP
 */

export class RTSPClient extends EventTarget {
  constructor(url, rtpPort = 5000) {
    super();
    this.url = new URL(url);
    this.rtpPort = rtpPort;
    this.cseq = 0;
    this.session = null;
    this.socket = null;
    this.reader = null;
    this.writer = null;
    this.buffer = "";
    this.sdp = null;
    this.trackUrl = null;
  }

  log(msg) {
    this.dispatchEvent(new CustomEvent("log", { detail: msg }));
  }

  async connect() {
    const host = this.url.hostname;
    const port = parseInt(this.url.port) || 554;

    this.log(`Connecting to ${host}:${port}...`);
    this.log(`TCPSocket available: ${typeof TCPSocket !== "undefined"}`);

    try {
      // Low-latency TCP settings
      this.socket = new TCPSocket(host, port, {
        noDelay: true,           // Disable Nagle's algorithm
        sendBufferSize: 16384,   // Smaller send buffer for lower latency
        receiveBufferSize: 16384 // Smaller receive buffer
      });
      this.log("TCPSocket created (low-latency mode), waiting for connection...");

      const { readable, writable } = await this.socket.opened;
      this.log("TCP socket opened successfully");

      this.reader = readable.getReader();
      this.writer = writable.getWriter();

      this.log("TCP connected, starting RTSP session...");

      // Start read loop
      this.readLoop();

      // RTSP handshake - DESCRIBE and SETUP only
      // Call play() separately after RTP receiver is ready
      await this.describe();
      await this.setup();

      return this.sdp;
    } catch (err) {
      this.log(`TCP connection error: ${err.name}: ${err.message}`);
      throw err;
    }
  }

  async readLoop() {
    const decoder = new TextDecoder();
    try {
      while (true) {
        const { value, done } = await this.reader.read();
        if (done) break;
        this.buffer += decoder.decode(value, { stream: true });
        this.processBuffer();
      }
    } catch (err) {
      this.log(`Read error: ${err.message}`);
    }
  }

  processBuffer() {
    // Check for complete RTSP response (headers + body)
    const headerEnd = this.buffer.indexOf("\r\n\r\n");
    if (headerEnd === -1) return;

    const headers = this.buffer.slice(0, headerEnd);
    const contentLengthMatch = headers.match(/Content-Length:\s*(\d+)/i);
    const contentLength = contentLengthMatch
      ? parseInt(contentLengthMatch[1])
      : 0;

    const totalLength = headerEnd + 4 + contentLength;
    if (this.buffer.length < totalLength) return;

    const response = this.buffer.slice(0, totalLength);
    this.buffer = this.buffer.slice(totalLength);

    this.handleResponse(response);
  }

  handleResponse(response) {
    const [headerSection, body] = response.split("\r\n\r\n");
    const lines = headerSection.split("\r\n");
    const statusLine = lines[0];
    const headers = {};

    for (let i = 1; i < lines.length; i++) {
      const colonIdx = lines[i].indexOf(":");
      if (colonIdx > 0) {
        const key = lines[i].slice(0, colonIdx).trim().toLowerCase();
        const value = lines[i].slice(colonIdx + 1).trim();
        headers[key] = value;
      }
    }

    // Extract session ID if present
    if (headers.session) {
      this.session = headers.session.split(";")[0];
    }

    this.dispatchEvent(
      new CustomEvent("response", {
        detail: { statusLine, headers, body },
      }),
    );
  }

  async sendRequest(method, url, extraHeaders = {}) {
    this.cseq++;
    const headers = {
      CSeq: this.cseq,
      "User-Agent": "IWA-RTSP-Client/1.0",
      ...extraHeaders,
    };

    if (this.session) {
      headers.Session = this.session;
    }

    let request = `${method} ${url} RTSP/1.0\r\n`;
    for (const [key, value] of Object.entries(headers)) {
      request += `${key}: ${value}\r\n`;
    }
    request += "\r\n";

    this.log(`>>> ${method} ${url}`);
    await this.writer.write(new TextEncoder().encode(request));

    // Wait for response
    return new Promise((resolve) => {
      const handler = (e) => {
        this.removeEventListener("response", handler);
        resolve(e.detail);
      };
      this.addEventListener("response", handler);
    });
  }

  async describe() {
    const response = await this.sendRequest("DESCRIBE", this.url.href, {
      Accept: "application/sdp",
    });

    if (!response.statusLine.includes("200")) {
      throw new Error(`DESCRIBE failed: ${response.statusLine}`);
    }

    this.sdp = this.parseSDP(response.body);
    this.log(`SDP parsed: ${this.sdp.videoTrack?.control}`);
    return this.sdp;
  }

  parseSDP(sdpText) {
    const lines = sdpText.split(/\r?\n/);
    const sdp = { videoTrack: null };
    let currentMedia = null;

    for (const line of lines) {
      if (line.startsWith("m=video")) {
        currentMedia = { type: "video" };
        sdp.videoTrack = currentMedia;
      } else if (line.startsWith("a=") && currentMedia) {
        const attr = line.slice(2);
        const eqIdx = attr.indexOf(":");
        if (eqIdx > 0) {
          const key = attr.slice(0, eqIdx);
          const value = attr.slice(eqIdx + 1);

          if (key === "control") {
            currentMedia.control = value;
          } else if (key === "fmtp") {
            // Parse fmtp for sprop-parameter-sets
            const spropMatch = value.match(/sprop-parameter-sets=([^;\s]+)/);
            if (spropMatch) {
              currentMedia.spropParameterSets = spropMatch[1];
            }
          } else if (key === "rtpmap") {
            // Parse rtpmap for payload type and codec
            const rtpMatch = value.match(/(\d+)\s+(\S+)\/(\d+)/);
            if (rtpMatch) {
              currentMedia.payloadType = parseInt(rtpMatch[1]);
              currentMedia.codec = rtpMatch[2];
              currentMedia.clockRate = parseInt(rtpMatch[3]);
            }
          }
        }
      }
    }

    return sdp;
  }

  async setup() {
    if (!this.sdp?.videoTrack) {
      throw new Error("No video track in SDP");
    }

    // Build track URL
    const control = this.sdp.videoTrack.control;
    if (control.startsWith("rtsp://")) {
      this.trackUrl = control;
    } else {
      // Relative URL
      const base = this.url.href.endsWith("/")
        ? this.url.href
        : this.url.href + "/";
      this.trackUrl = new URL(control, base).href;
    }

    const response = await this.sendRequest("SETUP", this.trackUrl, {
      Transport: `RTP/AVP;unicast;client_port=${this.rtpPort}-${this.rtpPort + 1}`,
    });

    if (!response.statusLine.includes("200")) {
      throw new Error(`SETUP failed: ${response.statusLine}`);
    }

    // Parse server_port from Transport header
    const transport = response.headers.transport || "";
    this.log(`Transport: ${transport}`);

    const serverPortMatch = transport.match(/server_port=(\d+)/);
    this.serverRtpPort = serverPortMatch ? parseInt(serverPortMatch[1]) : 8000;
    this.log(`Server RTP port: ${this.serverRtpPort}`);

    this.log(`Session: ${this.session}`);
    return response;
  }

  async play() {
    const response = await this.sendRequest("PLAY", this.url.href, {
      Range: "npt=0.000-",
    });

    if (!response.statusLine.includes("200")) {
      throw new Error(`PLAY failed: ${response.statusLine}`);
    }

    this.log("PLAY started - RTP stream should be flowing");
    this.dispatchEvent(new CustomEvent("playing"));
    return response;
  }

  async teardown() {
    // Send TEARDOWN to gracefully end the session
    if (this.session && this.writer) {
      try {
        this.log("Sending TEARDOWN...");
        // Use a timeout to avoid hanging if server doesn't respond
        const teardownPromise = this.sendRequest("TEARDOWN", this.url.href);
        const timeoutPromise = new Promise((_, reject) =>
          setTimeout(() => reject(new Error("TEARDOWN timeout")), 2000),
        );
        await Promise.race([teardownPromise, timeoutPromise]);
        this.log("TEARDOWN sent");
      } catch (err) {
        this.log(`TEARDOWN error (ignored): ${err.message}`);
      }
    }
    await this.close();
  }

  async close() {
    this.log("Closing TCP connection...");

    // Release reader lock first
    if (this.reader) {
      try {
        await this.reader.cancel();
      } catch (err) {
        // Ignore - may already be closed
      }
      try {
        this.reader.releaseLock();
      } catch (err) {
        // Ignore - may already be released
      }
      this.reader = null;
    }

    // Release writer lock
    if (this.writer) {
      try {
        await this.writer.close();
      } catch (err) {
        // Ignore - may already be closed
      }
      try {
        this.writer.releaseLock();
      } catch (err) {
        // Ignore - may already be released
      }
      this.writer = null;
    }

    // Now close socket
    if (this.socket) {
      try {
        await this.socket.close();
        this.log("TCP socket closed");
      } catch (err) {
        // Ignore - may already be closed
      }
      this.socket = null;
    }

    this.session = null;
  }
}
