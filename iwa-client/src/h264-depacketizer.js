/**
 * H.264 RTP Depacketizer (RFC 6184)
 * Reassembles H.264 NAL units from RTP packets
 *
 * Handles:
 * - Single NAL unit packets (type 1-23)
 * - FU-A fragmentation (type 28)
 * - STAP-A aggregation (type 24)
 */

export class H264Depacketizer extends EventTarget {
  constructor() {
    super();
    this.fuBuffer = null; // For FU-A reassembly
    this.fuTimestamp = null;
    this.lastSeq = -1;
    this.DISCONTINUITY_THRESHOLD = 5; // Reset FU-A if gap exceeds this
  }

  log(msg) {
    this.dispatchEvent(new CustomEvent("log", { detail: msg }));
  }

  // Process an RTP packet and emit complete NAL units
  processPacket(rtpPacket) {
    const { payload, timestamp, sequenceNumber, marker } = rtpPacket;

    if (payload.length === 0) return;

    // Track sequence and handle discontinuities
    // Small gaps are handled by reorder buffer, but large gaps indicate
    // stream disruption - reset FU-A to avoid emitting corrupted NALs
    if (this.lastSeq !== -1) {
      const expected = (this.lastSeq + 1) & 0xffff;
      if (sequenceNumber !== expected) {
        // Calculate gap with wrap-around handling
        const forwardGap = (sequenceNumber - expected) & 0xffff;
        const backwardGap = (expected - sequenceNumber) & 0xffff;
        const gap = Math.min(forwardGap, backwardGap);

        // Emit discontinuity event so consumer can discard corrupted access unit
        this.dispatchEvent(
          new CustomEvent("discontinuity", {
            detail: { expected, got: sequenceNumber, gap },
          }),
        );

        // Only log significant gaps (>2 packets) to reduce noise
        if (gap > 2) {
          this.log(
            `Seq discontinuity: expected ${expected}, got ${sequenceNumber} (gap: ${gap})`,
          );
        }

        // Large gap indicates serious disruption - reset FU-A state
        if (gap > this.DISCONTINUITY_THRESHOLD && this.fuBuffer !== null) {
          this.log(`Discarding incomplete FU-A due to large gap`);
          this.fuBuffer = null;
          this.fuTimestamp = null;
        }
      }
    }
    this.lastSeq = sequenceNumber;

    // First byte of payload is the NAL unit header (or FU indicator)
    const nalHeader = payload[0];
    const nalType = nalHeader & 0x1f;

    if (nalType >= 1 && nalType <= 23) {
      // Single NAL unit packet
      this.emitNAL(payload, timestamp);
    } else if (nalType === 28) {
      // FU-A (Fragmentation Unit)
      this.processFUA(payload, timestamp, marker);
    } else if (nalType === 24) {
      // STAP-A (Single-time Aggregation Packet)
      this.processSTAPA(payload, timestamp);
    } else {
      // Unsupported type (FU-B=29, STAP-B=25, MTAP=26-27 are rare)
      this.log(`Unsupported NAL type: ${nalType}`);
    }
  }

  processFUA(payload, timestamp, marker) {
    if (payload.length < 2) return;

    const fuIndicator = payload[0];
    const fuHeader = payload[1];

    const startBit = (fuHeader >> 7) & 0x01;
    const endBit = (fuHeader >> 6) & 0x01;
    const nalType = fuHeader & 0x1f;

    // Reconstruct NAL header: forbidden_bit + nri from FU indicator, type from FU header
    const nalHeader = (fuIndicator & 0xe0) | nalType;

    const fragmentData = payload.slice(2);

    if (startBit) {
      // Start of fragmented NAL
      this.fuBuffer = [nalHeader, ...fragmentData];
      this.fuTimestamp = timestamp;
    } else if (this.fuBuffer !== null) {
      // Continuation
      if (timestamp !== this.fuTimestamp) {
        // Timestamp changed mid-fragment, discard
        this.fuBuffer = null;
        return;
      }
      this.fuBuffer.push(...fragmentData);

      if (endBit) {
        // End of fragmented NAL
        this.emitNAL(new Uint8Array(this.fuBuffer), timestamp);
        this.fuBuffer = null;
      }
    }
  }

  processSTAPA(payload, timestamp) {
    // STAP-A: multiple NAL units in one packet
    // Format: [STAP header] [size1 (2 bytes)] [NAL1] [size2 (2 bytes)] [NAL2] ...
    let offset = 1; // Skip STAP-A header

    while (offset + 2 < payload.length) {
      const nalSize = (payload[offset] << 8) | payload[offset + 1];
      offset += 2;

      if (offset + nalSize > payload.length) {
        this.log("STAP-A: NAL size exceeds packet");
        break;
      }

      const nalUnit = payload.slice(offset, offset + nalSize);
      this.emitNAL(nalUnit, timestamp);
      offset += nalSize;
    }
  }

  emitNAL(nalUnit, timestamp) {
    const nalType = nalUnit[0] & 0x1f;

    this.dispatchEvent(
      new CustomEvent("nal", {
        detail: {
          data: nalUnit,
          timestamp,
          type: nalType,
          isKeyframe: nalType === 5, // IDR
          isSPS: nalType === 7,
          isPPS: nalType === 8,
        },
      }),
    );
  }

  reset() {
    this.fuBuffer = null;
    this.fuTimestamp = null;
    this.lastSeq = -1;
  }
}

/**
 * Parse SPS/PPS from sprop-parameter-sets (base64 encoded, comma separated)
 * Returns { sps: Uint8Array, pps: Uint8Array }
 */
export function parseSPropParameterSets(sprop) {
  const parts = sprop.split(",");
  let sps = null;
  let pps = null;

  for (const part of parts) {
    const data = base64ToUint8Array(part.trim());
    if (data.length === 0) continue;

    const nalType = data[0] & 0x1f;
    if (nalType === 7) {
      sps = data;
    } else if (nalType === 8) {
      pps = data;
    }
  }

  return { sps, pps };
}

function base64ToUint8Array(base64) {
  const binaryString = atob(base64);
  const bytes = new Uint8Array(binaryString.length);
  for (let i = 0; i < binaryString.length; i++) {
    bytes[i] = binaryString.charCodeAt(i);
  }
  return bytes;
}

/**
 * Build AVCC decoder description from SPS/PPS
 * Format: [1][profile][compat][level][0xFF][0xE1][sps_len][sps][1][pps_len][pps]
 */
export function buildAVCCDescription(sps, pps) {
  if (!sps || !pps) {
    throw new Error("SPS and PPS required for AVCC description");
  }

  const buffer = new ArrayBuffer(11 + sps.length + pps.length);
  const view = new DataView(buffer);
  const arr = new Uint8Array(buffer);

  let offset = 0;

  // Configuration version
  view.setUint8(offset++, 1);

  // Profile, compatibility, level from SPS
  view.setUint8(offset++, sps[1]); // profile_idc
  view.setUint8(offset++, sps[2]); // constraint flags
  view.setUint8(offset++, sps[3]); // level_idc

  // Length size minus one (3 = 4-byte lengths)
  view.setUint8(offset++, 0xff);

  // Number of SPS (1, with reserved bits)
  view.setUint8(offset++, 0xe1);

  // SPS length and data
  view.setUint16(offset, sps.length);
  offset += 2;
  arr.set(sps, offset);
  offset += sps.length;

  // Number of PPS
  view.setUint8(offset++, 1);

  // PPS length and data
  view.setUint16(offset, pps.length);
  offset += 2;
  arr.set(pps, offset);

  return new Uint8Array(buffer);
}
