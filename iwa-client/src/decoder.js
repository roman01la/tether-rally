/**
 * WebCodecs VideoDecoder wrapper for H.264
 * Uses WebGL with desynchronized mode for lowest latency rendering
 */

// WebGL shaders for video rendering
const VERTEX_SHADER = `
  attribute vec2 a_position;
  attribute vec2 a_texCoord;
  varying vec2 v_texCoord;
  void main() {
    gl_Position = vec4(a_position, 0.0, 1.0);
    v_texCoord = a_texCoord;
  }
`;

const FRAGMENT_SHADER = `
  precision mediump float;
  uniform sampler2D u_texture;
  varying vec2 v_texCoord;
  void main() {
    gl_FragColor = texture2D(u_texture, v_texCoord);
  }
`;

export class H264Decoder extends EventTarget {
  constructor(canvas) {
    super();
    this.canvas = canvas;
    this.gl = null;
    this.glProgram = null;
    this.glTexture = null;
    this.decoder = null;
    this.configured = false;
    this.gotKeyframe = false; // Track if we've received first keyframe
    this.frameCount = 0;
    this.pendingFrames = [];
    this.sps = null;
    this.pps = null;
    this.description = null; // Store AVCC description for reconfiguration
    this.recovering = false; // Prevent recursive recovery attempts

    // WFB-ng style: deadline-based frame dropping
    this.MAX_DECODE_QUEUE = 2; // Max frames queued in decoder
    this.MAX_FRAME_AGE_MS = 50; // Drop frames older than 50ms
    this.droppedFrames = 0;

    this.initWebGL();
  }

  initWebGL() {
    // desynchronized: true bypasses VSync for lowest latency
    this.gl = this.canvas.getContext("webgl", {
      alpha: false,
      antialias: false,
      depth: false,
      desynchronized: true, // Key for low latency - bypasses compositor
      preserveDrawingBuffer: false,
      powerPreference: "high-performance",
    });

    if (!this.gl) {
      console.error("WebGL not available, falling back to 2D");
      this.ctx = this.canvas.getContext("2d");
      return;
    }

    const gl = this.gl;

    // Compile shaders
    const vertShader = gl.createShader(gl.VERTEX_SHADER);
    gl.shaderSource(vertShader, VERTEX_SHADER);
    gl.compileShader(vertShader);

    const fragShader = gl.createShader(gl.FRAGMENT_SHADER);
    gl.shaderSource(fragShader, FRAGMENT_SHADER);
    gl.compileShader(fragShader);

    // Create program
    this.glProgram = gl.createProgram();
    gl.attachShader(this.glProgram, vertShader);
    gl.attachShader(this.glProgram, fragShader);
    gl.linkProgram(this.glProgram);
    gl.useProgram(this.glProgram);

    // Full-screen quad geometry
    const positions = new Float32Array([
      -1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1,
    ]);
    const texCoords = new Float32Array([0, 1, 1, 1, 0, 0, 0, 0, 1, 1, 1, 0]);

    const posBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);
    const posLoc = gl.getAttribLocation(this.glProgram, "a_position");
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

    const texBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, texBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, texCoords, gl.STATIC_DRAW);
    const texLoc = gl.getAttribLocation(this.glProgram, "a_texCoord");
    gl.enableVertexAttribArray(texLoc);
    gl.vertexAttribPointer(texLoc, 2, gl.FLOAT, false, 0, 0);

    // Create texture
    this.glTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.glTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

    this.log("WebGL initialized with desynchronized mode");
  }

  log(msg) {
    this.dispatchEvent(new CustomEvent("log", { detail: msg }));
  }

  configure(sps, pps, description) {
    this.sps = sps;
    this.pps = pps;
    this.description = description; // Store for reconfiguration

    // Build codec string from SPS
    // Format: avc1.PPCCLL (profile, constraint, level in hex)
    const profile = sps[1].toString(16).padStart(2, "0");
    const constraint = sps[2].toString(16).padStart(2, "0");
    const level = sps[3].toString(16).padStart(2, "0");
    this.codec = `avc1.${profile}${constraint}${level}`;

    this.log(`Configuring decoder: ${this.codec}`);

    this.createDecoder();
    this.configured = true;
    this.gotKeyframe = false; // Reset - need keyframe after configure
    this.log("Decoder configured");
  }

  createDecoder() {
    // Close existing decoder if any
    if (this.decoder) {
      try {
        this.decoder.close();
      } catch (e) {
        // Ignore - may already be closed
      }
    }

    this.decoder = new VideoDecoder({
      output: (frame) => this.handleFrame(frame),
      error: (err) => this.handleDecoderError(err),
    });

    this.decoder.configure({
      codec: this.codec,
      description: this.description,
      // Low-latency decoding settings
      optimizeForLatency: true, // Prioritize latency over throughput
      hardwareAcceleration: "prefer-hardware", // Use GPU when available
    });
  }

  handleDecoderError(err) {
    this.log(`Decoder error: ${err.message}`);
    this.dispatchEvent(
      new CustomEvent("error", {
        detail: { message: err.message, recoverable: this.canRecover() },
      }),
    );
  }

  canRecover() {
    return this.sps && this.pps && this.description && this.codec;
  }

  /**
   * Reset decoder state to wait for next keyframe.
   * Call this on discontinuity to prevent feeding corrupted delta frames.
   * Flushes decoder and sets gotKeyframe = false.
   */
  async resetToKeyframe() {
    if (!this.decoder || this.decoder.state === "closed") {
      return;
    }

    this.log("Resetting to keyframe after discontinuity");
    this.gotKeyframe = false;

    try {
      await this.decoder.flush();
    } catch (err) {
      // Ignore flush errors - decoder may be in bad state
      this.log(`Flush during reset failed: ${err.message}`);
    }
  }

  /**
   * Attempt to recover the decoder after an error.
   * Returns true if recovery was successful.
   */
  tryRecover() {
    if (this.recovering) {
      return false; // Already recovering
    }

    if (!this.canRecover()) {
      this.log("Cannot recover - missing SPS/PPS/description");
      return false;
    }

    this.recovering = true;
    this.log("Attempting decoder recovery...");

    try {
      this.createDecoder();
      this.gotKeyframe = false; // Must wait for next keyframe
      this.log("Decoder recovered, waiting for keyframe");
      this.recovering = false;
      return true;
    } catch (err) {
      this.log(`Recovery failed: ${err.message}`);
      this.recovering = false;
      return false;
    }
  }

  handleFrame(frame) {
    this.frameCount++;

    // WFB-ng style: drop frames if decoder queue is backed up
    // This prevents the "fast forward" effect after network hiccups
    if (this.decoder && this.decoder.decodeQueueSize > 1) {
      // Too many frames queued - drop this one to catch up
      frame.close();
      this.droppedFrames++;
      return;
    }

    // Update canvas size if needed
    if (
      this.canvas.width !== frame.displayWidth ||
      this.canvas.height !== frame.displayHeight
    ) {
      this.canvas.width = frame.displayWidth;
      this.canvas.height = frame.displayHeight;
      if (this.gl) {
        this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      }
      this.log(
        `Canvas resized to ${frame.displayWidth}x${frame.displayHeight}`,
      );
    }

    // Render frame
    if (this.gl) {
      // WebGL path - upload VideoFrame directly as texture (zero-copy on supported platforms)
      this.gl.bindTexture(this.gl.TEXTURE_2D, this.glTexture);
      this.gl.texImage2D(
        this.gl.TEXTURE_2D,
        0,
        this.gl.RGBA,
        this.gl.RGBA,
        this.gl.UNSIGNED_BYTE,
        frame,
      );
      this.gl.drawArrays(this.gl.TRIANGLES, 0, 6);
    } else {
      // Canvas 2D fallback
      this.ctx.drawImage(frame, 0, 0);
    }
    frame.close();

    if (this.frameCount % 100 === 0) {
      this.log(`Decoded ${this.frameCount} frames`);
    }
  }

  // Feed a NAL unit (with Annex B start code prepended)
  decodeNAL(nalData, timestamp, isKeyframe) {
    if (!this.configured) {
      this.log("Decoder not configured, skipping NAL");
      return;
    }

    // For AVCC format, we need length-prefixed NALs, not Annex B
    // WebCodecs with description expects AVCC format
    const chunk = this.createAVCCChunk(nalData, timestamp, isKeyframe);

    try {
      this.decoder.decode(chunk);
    } catch (err) {
      this.log(`Decode error: ${err.message}`);
    }
  }

  createAVCCChunk(nalData, timestamp, isKeyframe) {
    // AVCC format: 4-byte length prefix + NAL data
    const buffer = new ArrayBuffer(4 + nalData.length);
    const view = new DataView(buffer);
    view.setUint32(0, nalData.length);
    new Uint8Array(buffer, 4).set(nalData);

    return new EncodedVideoChunk({
      type: isKeyframe ? "key" : "delta",
      timestamp: timestamp, // RTP timestamp in clock units
      data: buffer,
    });
  }

  // Decode a complete access unit (may contain multiple NALs)
  decodeAccessUnit(nalUnits, timestamp, arrivalTime = null) {
    if (!this.configured || nalUnits.length === 0) return;

    // WFB-ng style: check if frame is too old (deadline-based dropping)
    if (arrivalTime !== null) {
      const age = performance.now() - arrivalTime;
      if (age > this.MAX_FRAME_AGE_MS) {
        this.droppedFrames++;
        if (this.droppedFrames % 100 === 1) {
          this.log(
            `Dropped late frame (age=${age.toFixed(1)}ms, total dropped=${this.droppedFrames})`,
          );
        }
        return;
      }
    }

    // WFB-ng style: check decoder queue depth to prevent backup
    if (this.decoder && this.decoder.decodeQueueSize > this.MAX_DECODE_QUEUE) {
      this.droppedFrames++;
      // Only drop non-keyframes to avoid losing sync
      const isKeyframe = nalUnits.some((n) => (n[0] & 0x1f) === 5);
      if (!isKeyframe) {
        if (this.droppedFrames % 100 === 1) {
          this.log(
            `Dropped frame (queue=${this.decoder.decodeQueueSize}, total dropped=${this.droppedFrames})`,
          );
        }
        return;
      }
    }

    // Check decoder state and recover if needed
    if (!this.decoder || this.decoder.state === "closed") {
      this.log("Decoder closed, attempting recovery");
      if (!this.tryRecover()) {
        return; // Recovery failed
      }
    }

    // Check if this is a keyframe (contains IDR)
    const isKeyframe = nalUnits.some((n) => (n[0] & 0x1f) === 5);

    // Must wait for keyframe after configure/flush/recovery
    if (!this.gotKeyframe) {
      if (!isKeyframe) {
        // Silently skip - waiting for keyframe
        return;
      }
      this.gotKeyframe = true;
      this.log("First keyframe received, starting decode");
    }

    // Build AVCC data: concatenate length-prefixed NALs
    let totalLength = 0;
    for (const nal of nalUnits) {
      totalLength += 4 + nal.length;
    }

    const buffer = new ArrayBuffer(totalLength);
    const view = new DataView(buffer);
    const arr = new Uint8Array(buffer);

    let offset = 0;
    for (const nal of nalUnits) {
      view.setUint32(offset, nal.length);
      arr.set(nal, offset + 4);
      offset += 4 + nal.length;
    }

    const chunk = new EncodedVideoChunk({
      type: isKeyframe ? "key" : "delta",
      timestamp,
      data: buffer,
    });

    try {
      this.decoder.decode(chunk);
    } catch (err) {
      this.log(`Decode error: ${err.message}`);
      // Try recovery on synchronous decode errors too
      if (this.decoder.state === "closed") {
        this.tryRecover();
      }
    }
  }

  async flush() {
    if (this.decoder) {
      await this.decoder.flush();
    }
  }

  close() {
    if (this.decoder) {
      this.decoder.close();
      this.decoder = null;
    }
    this.configured = false;
  }
}
