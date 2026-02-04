import { RTSPClient } from "./rtsp-client.js";
import { RTPReceiver } from "./rtp-receiver.js";
import {
  H264Depacketizer,
  parseSPropParameterSets,
  buildAVCCDescription,
} from "./h264-depacketizer.js";
import { H264Decoder } from "./decoder.js";
import { discoverEndpoint } from "./stun-client.js";

const RTP_PORT = 5000;

// DOM elements
const urlInput = document.getElementById("url");
const tokenInput = document.getElementById("token");
const connectBtn = document.getElementById("connect");
const connectPunchBtn = document.getElementById("connect-punch");
const disconnectBtn = document.getElementById("disconnect");
const canvas = document.getElementById("video");
const logEl = document.getElementById("log");
const statsEl = document.getElementById("stats");
const natStatusEl = document.getElementById("nat-status");

// Components
let rtspClient = null;
let rtpReceiver = null;
let depacketizer = null;
let decoder = null;
let statsInterval = null;

// Frame stats
let lastFrameCount = 0;
let lastStatsTime = Date.now();

function log(msg, className = "") {
  const line = document.createElement("div");
  if (className) line.className = className;
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
  console.log(msg);
}

function updateStats() {
  if (!rtpReceiver) {
    statsEl.textContent = "Not connected";
    return;
  }

  const now = Date.now();
  const elapsed = (now - lastStatsTime) / 1000;
  const rtp = rtpReceiver.getStats();
  const frameCount = decoder?.frameCount || 0;
  const fps =
    elapsed > 0 ? Math.round((frameCount - lastFrameCount) / elapsed) : 0;

  // Format bitrate nicely
  const bitrateStr =
    rtp.bitrate >= 1000
      ? `${(rtp.bitrate / 1000).toFixed(1)} Mbps`
      : `${rtp.bitrate} kbps`;

  // Use textContent (CSP doesn't allow innerHTML in IWA)
  statsEl.textContent = `Bitrate: ${bitrateStr} | FPS: ${fps} | Frames: ${frameCount} | Pkts: ${rtp.packetsReceived} | Lost: ${rtp.packetsLost} (${rtp.lossPercent}%) | Jitter: ${rtp.jitterMs}ms`;

  lastStatsTime = now;
  lastFrameCount = frameCount;
  rtpReceiver.resetPeriod();
}

async function connect() {
  const url = urlInput.value.trim();
  if (!url) {
    log("Please enter RTSP URL", "error");
    return;
  }

  try {
    connectBtn.disabled = true;
    log(`Connecting to ${url}...`);

    // Initialize RTSP client first
    rtspClient = new RTSPClient(url, RTP_PORT);
    depacketizer = new H264Depacketizer();
    decoder = new H264Decoder(canvas);

    // Wire up logging
    rtspClient.addEventListener("log", (e) => log(`[RTSP] ${e.detail}`));
    depacketizer.addEventListener("log", (e) => log(`[H264] ${e.detail}`));
    decoder.addEventListener("log", (e) => log(`[DEC] ${e.detail}`));

    // NAL accumulator for access unit assembly
    let currentTimestamp = null;
    let currentNALs = [];
    let accessUnitCorrupted = false; // Track if current access unit has missing packets

    // Handle packet loss - discard current access unit and reset decoder
    depacketizer.addEventListener("discontinuity", (e) => {
      accessUnitCorrupted = true;
      // Reset decoder to wait for next keyframe - prevents feeding corrupted delta frames
      decoder.resetToKeyframe();
    });

    // Handle depacketized NALs
    depacketizer.addEventListener("nal", (e) => {
      const { data, timestamp, type, isKeyframe, isSPS, isPPS } = e.detail;

      // Skip SPS/PPS from stream (we got them from SDP)
      if (isSPS || isPPS) return;

      // Assemble NALs into access units by timestamp
      if (currentTimestamp !== null && timestamp !== currentTimestamp) {
        // New timestamp = new access unit, decode previous (if not corrupted)
        if (currentNALs.length > 0 && !accessUnitCorrupted) {
          decoder.decodeAccessUnit(currentNALs, currentTimestamp);
        } else if (accessUnitCorrupted && currentNALs.length > 0) {
          // Silently discard corrupted access unit
        }
        currentNALs = [];
        accessUnitCorrupted = false; // Reset for new access unit
      }

      currentTimestamp = timestamp;
      currentNALs.push(data);

      // If marker bit was set (handled in RTP), we could flush here
      // For now, we rely on timestamp changes
    });

    // Connect RTSP and get SDP (DESCRIBE + SETUP only)
    const sdp = await rtspClient.connect();

    if (!sdp.videoTrack?.spropParameterSets) {
      throw new Error("No sprop-parameter-sets in SDP");
    }

    // Parse SPS/PPS from SDP
    const { sps, pps } = parseSPropParameterSets(
      sdp.videoTrack.spropParameterSets,
    );
    if (!sps || !pps) {
      throw new Error("Failed to parse SPS/PPS from SDP");
    }

    log(`SPS: ${sps.length} bytes, PPS: ${pps.length} bytes`);

    // Build AVCC description and configure decoder
    const description = buildAVCCDescription(sps, pps);
    decoder.configure(sps, pps, description);

    // Create RTP receiver
    rtpReceiver = new RTPReceiver(RTP_PORT);
    rtpReceiver.addEventListener("log", (e) => log(`[RTP] ${e.detail}`));

    // Handle RTP packets
    rtpReceiver.addEventListener("packet", (e) => {
      depacketizer.processPacket(e.detail);

      // Flush on marker bit (end of frame)
      if (e.detail.marker && currentNALs.length > 0) {
        if (!accessUnitCorrupted) {
          decoder.decodeAccessUnit(currentNALs, currentTimestamp);
        }
        currentNALs = [];
        currentTimestamp = null;
        accessUnitCorrupted = false;
      }
    });

    // Determine bind address based on server hostname
    // Use IPv6 for localhost, IPv4 for remote servers
    const serverHost = new URL(url).hostname;
    const isLocalhost =
      serverHost === "localhost" ||
      serverHost === "127.0.0.1" ||
      serverHost === "::1";
    const bindAddress = isLocalhost ? "::1" : "0.0.0.0";

    // Start RTP receiver BEFORE sending PLAY
    // This ensures UDP socket is bound before server starts sending
    await rtpReceiver.start(bindAddress);

    // Now send PLAY to start the stream
    await rtspClient.play();

    disconnectBtn.disabled = false;
    log("Connected! Waiting for video...", "success");

    // Start stats update
    lastStatsTime = Date.now();
    lastFrameCount = 0;
    statsInterval = setInterval(updateStats, 1000);
  } catch (err) {
    log(`Connection failed: ${err.message}`, "error");
    console.error(err);
    disconnect();
  }
}

/**
 * Connect via UDP hole punching (for public internet access)
 * Uses signaling via existing Cloudflare Tunnel to Pi
 */
async function connectViaHolePunch() {
  const url = urlInput.value.trim();
  const token = tokenInput?.value?.trim();

  if (!url) {
    log("Please enter server URL", "error");
    return;
  }
  if (!token) {
    log("Please enter access token", "error");
    return;
  }

  try {
    connectBtn.disabled = true;
    connectPunchBtn.disabled = true;
    log(`Connecting via hole punch to ${url}...`);

    // Initialize components
    depacketizer = new H264Depacketizer();
    decoder = new H264Decoder(canvas);

    depacketizer.addEventListener("log", (e) => log(`[H264] ${e.detail}`));
    decoder.addEventListener("log", (e) => log(`[DEC] ${e.detail}`));

    // NAL accumulator for access unit assembly
    let currentTimestamp = null;
    let currentNALs = [];
    let accessUnitCorrupted = false; // Track if current access unit has missing packets

    // Handle packet loss - discard current access unit and reset decoder
    depacketizer.addEventListener("discontinuity", (e) => {
      accessUnitCorrupted = true;
      // Reset decoder to wait for next keyframe - prevents feeding corrupted delta frames
      decoder.resetToKeyframe();
    });

    depacketizer.addEventListener("nal", (e) => {
      const { data, timestamp, isSPS, isPPS } = e.detail;

      // Capture SPS/PPS for decoder configuration
      if (isSPS) {
        if (!decoder.sps) {
          log(`Received SPS from stream: ${data.length} bytes`);
        }
        decoder.sps = data;
        // Try to configure if we have both
        if (decoder.sps && decoder.pps && !decoder.configured) {
          const description = buildAVCCDescription(decoder.sps, decoder.pps);
          decoder.configure(decoder.sps, decoder.pps, description);
        }
        return;
      }
      if (isPPS) {
        if (!decoder.pps) {
          log(`Received PPS from stream: ${data.length} bytes`);
        }
        decoder.pps = data;
        // Try to configure if we have both
        if (decoder.sps && decoder.pps && !decoder.configured) {
          const description = buildAVCCDescription(decoder.sps, decoder.pps);
          decoder.configure(decoder.sps, decoder.pps, description);
        }
        return;
      }

      if (currentTimestamp !== null && timestamp !== currentTimestamp) {
        if (currentNALs.length > 0 && !accessUnitCorrupted) {
          decoder.decodeAccessUnit(currentNALs, currentTimestamp);
        }
        currentNALs = [];
        accessUnitCorrupted = false;
      }

      currentTimestamp = timestamp;
      currentNALs.push(data);
    });

    // Step 1: Create RTP receiver and bind UDP socket
    rtpReceiver = new RTPReceiver(RTP_PORT);
    rtpReceiver.addEventListener("log", (e) => log(`[RTP] ${e.detail}`));

    rtpReceiver.addEventListener("packet", (e) => {
      depacketizer.processPacket(e.detail);

      if (e.detail.marker && currentNALs.length > 0) {
        if (!accessUnitCorrupted) {
          decoder.decodeAccessUnit(currentNALs, currentTimestamp);
        }
        currentNALs = [];
        currentTimestamp = null;
        accessUnitCorrupted = false;
      }
    });

    // Step 1: Bind UDP socket (don't start receive loop yet)
    await rtpReceiver.bind("0.0.0.0");

    // Step 2: Discover our public endpoint via STUN
    log("Discovering public endpoint via STUN...");
    if (natStatusEl) natStatusEl.textContent = "Querying STUN...";

    let publicEndpoint;
    try {
      publicEndpoint = await discoverEndpoint(
        rtpReceiver.readable,
        rtpReceiver.writer,
      );
      log(
        `Public endpoint: ${publicEndpoint.publicIp}:${publicEndpoint.publicPort}`,
      );

      if (publicEndpoint.isSymmetric) {
        log(
          "WARNING: Symmetric NAT detected - hole punching may fail!",
          "error",
        );
        if (natStatusEl) {
          natStatusEl.textContent = "Symmetric NAT (may fail)";
          natStatusEl.style.color = "#f55";
        }
      } else {
        if (natStatusEl) {
          natStatusEl.textContent = "Cone NAT (OK)";
          natStatusEl.style.color = "#5f5";
        }
      }
    } catch (stunErr) {
      log(`STUN failed: ${stunErr.message}`, "error");
      throw stunErr;
    }

    // Step 3: Call /stream/punch to exchange endpoints
    log("Requesting hole punch from server...");

    const punchUrl = `${url}/stream/punch?token=${encodeURIComponent(token)}`;
    const punchResp = await fetch(punchUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        clientIp: publicEndpoint.publicIp,
        clientPort: publicEndpoint.publicPort,
      }),
    });

    if (!punchResp.ok) {
      const errText = await punchResp.text();
      throw new Error(`Punch request failed: ${punchResp.status} - ${errText}`);
    }

    const punchData = await punchResp.json();
    log(`Pi endpoint: ${punchData.piIp}:${punchData.piPort}`);

    if (punchData.natType === "symmetric") {
      log("WARNING: Pi has symmetric NAT - connection may fail!", "error");
    }

    // Step 4: Configure decoder with SDP info from server
    const sdpInfo = punchData.sdpInfo;
    if (sdpInfo && sdpInfo.spropParameterSets) {
      const { sps, pps } = parseSPropParameterSets(sdpInfo.spropParameterSets);
      if (sps && pps) {
        log(`SPS: ${sps.length} bytes, PPS: ${pps.length} bytes`);
        const description = buildAVCCDescription(sps, pps);
        decoder.configure(sps, pps, description);
      } else {
        log("No SPS/PPS in server response, will configure from stream");
      }
    } else {
      log("No codec info from server, will configure from stream");
    }

    // Step 5: Start receive loop and hole punching
    log("Starting hole punch...");
    rtpReceiver.startReceiveLoop();
    await rtpReceiver.startWithPunch(punchData.piIp, punchData.piPort);

    disconnectBtn.disabled = false;
    log("Hole punch initiated! Waiting for video...", "success");

    // Start stats update
    lastStatsTime = Date.now();
    lastFrameCount = 0;
    statsInterval = setInterval(updateStats, 1000);
  } catch (err) {
    log(`Hole punch failed: ${err.message}`, "error");
    console.error(err);
    disconnect();
  }
}

async function disconnect() {
  // Stop stats updates
  if (statsInterval) {
    clearInterval(statsInterval);
    statsInterval = null;
  }

  // Clear depacketizer first to stop processing
  if (depacketizer) {
    depacketizer.reset();
    depacketizer = null;
  }

  if (rtpReceiver) {
    await rtpReceiver.stop();
    rtpReceiver = null;
  }

  if (rtspClient) {
    await rtspClient.teardown().catch(() => {});
    rtspClient = null;
  }

  if (decoder) {
    decoder.close();
    decoder = null;
  }

  connectBtn.disabled = false;
  connectPunchBtn.disabled = false;
  disconnectBtn.disabled = true;
  statsEl.textContent = "Not connected";
  if (natStatusEl) natStatusEl.textContent = "";
  log("Disconnected");
}

// Event listeners
connectBtn.addEventListener("click", connect);
connectPunchBtn?.addEventListener("click", connectViaHolePunch);
disconnectBtn.addEventListener("click", disconnect);

urlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !connectBtn.disabled) {
    connect();
  }
});

// Cleanup on app close/navigation
// Use multiple events for reliability across different scenarios
window.addEventListener("beforeunload", () => {
  // Synchronous cleanup attempt
  if (rtspClient || rtpReceiver) {
    console.log("App closing, cleaning up connections...");
  }
});

window.addEventListener("pagehide", async () => {
  // More reliable on mobile and for bfcache
  await disconnect();
});

document.addEventListener("visibilitychange", async () => {
  // Optional: disconnect when tab becomes hidden (saves resources)
  // Uncomment if you want aggressive cleanup:
  // if (document.hidden) {
  //   await disconnect();
  // }
});

// Also handle unload as fallback
window.addEventListener("unload", () => {
  // Last-ditch synchronous cleanup
  // Note: async operations may not complete here
  if (rtpReceiver?.socket) {
    try {
      rtpReceiver.socket.close();
    } catch (e) {}
  }
  if (rtspClient?.socket) {
    try {
      rtspClient.socket.close();
    } catch (e) {}
  }
});

log("Ready. Enter RTSP URL and click Connect.");
