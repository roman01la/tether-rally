/**
 * FEC Decoder - JavaScript wrapper for zfec WASM module
 *
 * Provides Reed-Solomon 8+2 FEC decoding for RTP packet recovery.
 *
 * Usage:
 *   const decoder = new FECDecoder();
 *   await decoder.init();
 *   const recovered = decoder.decode(k, n, packets, indices, packetSize);
 */

// Import the Emscripten-generated module
import createFecModule from "./fec.js";

export class FECDecoder {
  constructor() {
    this.module = null;
    this.fecCodec = null;
    this.currentK = 0;
    this.currentN = 0;

    // WASM function wrappers
    this._fec_init = null;
    this._fec_new = null;
    this._fec_free = null;
    this._fec_decode = null;
  }

  /**
   * Initialize the WASM module
   */
  async init() {
    if (this.module) return;

    this.module = await createFecModule();

    // Verify WASM memory is accessible
    if (!this.module.HEAPU8) {
      throw new Error("FEC WASM module loaded but memory not accessible");
    }

    // Wrap C functions
    this._fec_init = this.module.cwrap("fec_init", null, []);
    this._fec_new = this.module.cwrap("fec_new", "number", [
      "number",
      "number",
    ]);
    this._fec_free = this.module.cwrap("fec_free", null, ["number"]);

    // Initialize FEC library
    this._fec_init();

    console.log(
      "FEC decoder initialized, HEAPU8 size:",
      this.module.HEAPU8.length,
    );
  }

  /**
   * Ensure codec is created for given k,n parameters
   */
  ensureCodec(k, n) {
    if (this.currentK === k && this.currentN === n && this.fecCodec) {
      return;
    }

    // Free old codec
    if (this.fecCodec) {
      this._fec_free(this.fecCodec);
    }

    // Create new codec
    this.fecCodec = this._fec_new(k, n);
    if (!this.fecCodec) {
      throw new Error(`Failed to create FEC codec for k=${k}, n=${n}`);
    }

    this.currentK = k;
    this.currentN = n;
  }

  /**
   * Decode FEC-protected packets
   *
   * @param {number} k - Number of data packets required
   * @param {number} n - Total number of packets (data + parity)
   * @param {Uint8Array[]} packets - Array of k packets (data or parity)
   * @param {number[]} indices - Block indices of the packets
   * @param {number} packetSize - Size of each packet (all must be same size, padded)
   * @returns {Uint8Array[]|null} - Array of k recovered data packets, or null if decode fails
   */
  decode(k, n, packets, indices, packetSize) {
    // Return null instead of throwing if not properly initialized
    // This allows graceful fallback to raw packets
    if (!this.module) {
      console.warn("FEC decode: decoder not initialized");
      return null;
    }

    if (packets.length < k) {
      console.warn(
        `FEC decode: need at least ${k} packets, got ${packets.length}`,
      );
      return null;
    }

    this.ensureCodec(k, n);

    const M = this.module;

    // Validate WASM memory is accessible
    if (!M.HEAPU8) {
      console.warn("FEC decode: WASM memory not accessible");
      return null;
    }

    // Validate packetSize is reasonable
    if (packetSize <= 0 || packetSize > 65536) {
      console.warn(`FEC decode: invalid packetSize ${packetSize}`);
      return null;
    }

    // Validate all packets exist and are valid Uint8Arrays
    for (let i = 0; i < k; i++) {
      if (!packets[i] || !(packets[i] instanceof Uint8Array)) {
        console.warn(`FEC decode: invalid packet at index ${i}`);
        return null;
      }
    }

    // Allocate WASM memory for input packets (array of pointers)
    const inPtrsPtr = M._malloc(k * 4);
    if (!inPtrsPtr) {
      throw new Error("FEC decode: failed to allocate input pointer array");
    }
    const inPtrs = [];
    for (let i = 0; i < k; i++) {
      const ptr = M._malloc(packetSize);
      if (!ptr) {
        // Free already allocated memory
        for (const p of inPtrs) M._free(p);
        M._free(inPtrsPtr);
        throw new Error(
          `FEC decode: failed to allocate memory for packet ${i}`,
        );
      }
      const pkt = packets[i];
      const copyLen = Math.min(pkt.length, packetSize);
      M.HEAPU8.set(pkt.subarray(0, copyLen), ptr);
      M.setValue(inPtrsPtr + i * 4, ptr, "i32");
      inPtrs.push(ptr);
    }

    // Allocate memory for index array
    const indexPtr = M._malloc(k * 4);
    for (let i = 0; i < k; i++) {
      M.setValue(indexPtr + i * 4, indices[i], "i32");
    }

    // Count how many primary blocks need reconstruction
    let missingCount = 0;
    for (let i = 0; i < k; i++) {
      if (indices[i] >= k) {
        missingCount++;
      }
    }

    // Allocate memory for output packets
    const outPtrsPtr = M._malloc(missingCount * 4);
    const outPtrs = [];
    for (let i = 0; i < missingCount; i++) {
      const ptr = M._malloc(packetSize);
      M.setValue(outPtrsPtr + i * 4, ptr, "i32");
      outPtrs.push(ptr);
    }

    // Call fec_decode
    // Note: fec_decode modifies the input buffers in-place, placing
    // recovered data into the positions that had parity blocks
    M.ccall(
      "fec_decode",
      null,
      ["number", "number", "number", "number", "number"],
      [this.fecCodec, inPtrsPtr, outPtrsPtr, indexPtr, packetSize],
    );

    // Build result: after decode, we need to extract the k data packets
    // The decode function reconstructs missing primary blocks into outPtrs
    // Initialize with empty arrays to prevent undefined access in case of edge cases
    const result = new Array(k);
    for (let i = 0; i < k; i++) {
      result[i] = new Uint8Array(packetSize);
    }

    // Track which primary blocks are missing
    const receivedPrimary = new Set();
    for (let i = 0; i < k; i++) {
      if (indices[i] < k) {
        receivedPrimary.add(indices[i]);
      }
    }

    // First, place received primary blocks
    for (let i = 0; i < k; i++) {
      if (indices[i] < k) {
        // This input is a primary block - copy from input buffer
        const primaryIdx = indices[i];
        result[primaryIdx] = new Uint8Array(
          M.HEAPU8.buffer,
          inPtrs[i],
          packetSize,
        ).slice();
      }
    }

    // Then, fill in recovered blocks from output
    // outPtrs contains recovered primary blocks in order of missing indices
    let outIdx = 0;
    for (let i = 0; i < k; i++) {
      if (!receivedPrimary.has(i)) {
        // This primary block was missing, get from recovery output
        if (outIdx < outPtrs.length) {
          result[i] = new Uint8Array(
            M.HEAPU8.buffer,
            outPtrs[outIdx],
            packetSize,
          ).slice();
          outIdx++;
        } else {
          // Should not happen if we have k packets, but guard anyway
          console.error(`FEC decode: missing recovery for block ${i}`);
          result[i] = new Uint8Array(packetSize);
        }
      }
    }

    // Free WASM memory
    for (const ptr of inPtrs) M._free(ptr);
    for (const ptr of outPtrs) M._free(ptr);
    M._free(inPtrsPtr);
    M._free(outPtrsPtr);
    M._free(indexPtr);

    return result;
  }

  /**
   * Clean up resources
   */
  destroy() {
    if (this.fecCodec && this._fec_free) {
      this._fec_free(this.fecCodec);
      this.fecCodec = null;
    }
    this.module = null;
  }
}

/**
 * FEC Group Buffer - Collects packets for a FEC group and triggers decode
 */
export class FECGroupBuffer {
  constructor(decoder, onGroupDecoded) {
    this.decoder = decoder;
    this.onGroupDecoded = onGroupDecoded;
    this.groups = new Map(); // group_id -> { packets, indices, maxSize, k, n, timer }
    this.TIMEOUT_MS = 24; // 1.5 frames at 60fps - more time to collect packets
    this.MAX_GROUPS = 6; // Max groups to buffer

    // Pre-allocated padding buffers for FEC decode (reused across groups)
    // Pool of buffers by size bucket (rounded up to nearest 256 bytes)
    this.bufferPool = new Map(); // size -> array of Uint8Arrays
    this.MAX_POOL_SIZE = 16; // Max buffers per size bucket
  }

  /**
   * Get a buffer of at least the requested size from pool, or allocate new
   */
  getBuffer(size) {
    // Round up to nearest 256 bytes for better pooling
    const bucket = Math.ceil(size / 256) * 256;
    const pool = this.bufferPool.get(bucket);
    if (pool && pool.length > 0) {
      const buf = pool.pop();
      // Zero out only the portion we'll use (partial clear is faster)
      buf.fill(0, 0, size);
      return buf;
    }
    return new Uint8Array(bucket);
  }

  /**
   * Return a buffer to the pool for reuse
   */
  releaseBuffer(buf) {
    if (!buf) return;
    const bucket = buf.length;
    let pool = this.bufferPool.get(bucket);
    if (!pool) {
      pool = [];
      this.bufferPool.set(bucket, pool);
    }
    if (pool.length < this.MAX_POOL_SIZE) {
      pool.push(buf);
    }
    // If pool is full, buffer is GC'd
  }

  /**
   * Add a packet to the buffer
   * @param {number} groupId - FEC group ID
   * @param {number} index - Packet index within group
   * @param {number} k - Data packets per group
   * @param {number} n - Total packets per group
   * @param {Uint8Array} payload - RTP packet payload
   */
  addPacket(groupId, index, k, n, payload) {
    // Clean up old groups if too many
    while (this.groups.size >= this.MAX_GROUPS) {
      const oldestId = this.groups.keys().next().value;
      this.flushGroup(oldestId, true);
    }

    // Get or create group
    let group = this.groups.get(groupId);
    if (!group) {
      group = {
        packets: new Map(),
        k,
        n,
        maxSize: 0,
        timer: setTimeout(
          () => this.flushGroup(groupId, false),
          this.TIMEOUT_MS,
        ),
      };
      this.groups.set(groupId, group);
    }

    // Store packet
    group.packets.set(index, payload);
    if (payload.length > group.maxSize) {
      group.maxSize = payload.length;
    }

    // If we have enough packets to decode, flush immediately
    if (group.packets.size >= k) {
      this.flushGroup(groupId, false);
    }
  }

  /**
   * Flush a group - attempt decode if possible
   */
  flushGroup(groupId, force) {
    const group = this.groups.get(groupId);
    if (!group) return;

    // Clear timeout
    if (group.timer) {
      clearTimeout(group.timer);
    }
    this.groups.delete(groupId);

    const { packets, k, n, maxSize } = group;

    // Need at least k packets to decode
    if (packets.size < k) {
      // Only log severe packet loss (less than half received), not normal network jitter
      if (!force && packets.size < k / 2) {
        console.warn(
          `FEC group ${groupId}: only ${packets.size}/${k} packets, cannot decode`,
        );
      }
      // Emit what we have (data packets only, in order)
      const dataPackets = [];
      for (let i = 0; i < k; i++) {
        if (packets.has(i)) {
          dataPackets.push({ index: i, data: packets.get(i) });
        }
      }
      this.onGroupDecoded(groupId, dataPackets, false);
      return;
    }

    // Prepare for decode
    // IMPORTANT: zfec requires that data packets are at their correct row positions
    // i.e., if we have data packet with index i, it must be at inPackets[i]
    // Missing data packets must be filled with parity packets
    const inPackets = new Array(k);
    const indices = new Array(k);

    // First pass: place all received data packets at their correct positions
    for (let i = 0; i < k; i++) {
      if (packets.has(i)) {
        const padded = this.getBuffer(maxSize);
        padded.set(packets.get(i));
        inPackets[i] = padded;
        indices[i] = i;
      }
    }

    // Second pass: fill missing positions with parity packets
    let parityIdx = k;
    for (let i = 0; i < k; i++) {
      if (!inPackets[i]) {
        // Find next available parity packet
        while (parityIdx < n && !packets.has(parityIdx)) {
          parityIdx++;
        }
        if (parityIdx >= n) {
          // Not enough parity packets to fill the gap
          break;
        }
        const padded = this.getBuffer(maxSize);
        padded.set(packets.get(parityIdx));
        inPackets[i] = padded;
        indices[i] = parityIdx;
        parityIdx++;
      }
    }

    // Final check: all k slots must be filled
    const filledCount = inPackets.filter((p) => p !== undefined).length;
    if (filledCount < k) {
      console.warn(
        `FEC group ${groupId}: only gathered ${filledCount}/${k} valid packets after filtering`,
      );
      // Release allocated buffers
      for (const buf of inPackets) {
        this.releaseBuffer(buf);
      }
      // Emit raw data packets as fallback
      const dataPackets = [];
      for (let i = 0; i < k; i++) {
        if (packets.has(i)) {
          dataPackets.push({ index: i, data: packets.get(i) });
        }
      }
      this.onGroupDecoded(groupId, dataPackets, false);
      return;
    }

    // Validate maxSize is reasonable
    if (maxSize <= 0) {
      console.warn(
        `FEC group ${groupId}: invalid maxSize ${maxSize}, skipping decode`,
      );
      // Release allocated buffers
      for (const buf of inPackets) {
        this.releaseBuffer(buf);
      }
      const dataPackets = [];
      for (let i = 0; i < k; i++) {
        if (packets.has(i)) {
          dataPackets.push({ index: i, data: packets.get(i) });
        }
      }
      this.onGroupDecoded(groupId, dataPackets, false);
      return;
    }

    // Decode
    try {
      const decoded = this.decoder.decode(k, n, inPackets, indices, maxSize);

      // Return input buffers to pool
      for (const buf of inPackets) {
        this.releaseBuffer(buf);
      }

      // If decode returned null (validation failed), fall back to raw packets
      if (!decoded) {
        const dataPackets = [];
        for (let i = 0; i < k; i++) {
          if (packets.has(i)) {
            dataPackets.push({ index: i, data: packets.get(i) });
          }
        }
        this.onGroupDecoded(groupId, dataPackets, false);
        return;
      }

      // Emit decoded packets with their original sizes
      // Note: We lose original size info, so emit full padded size
      // The RTP parser will handle this (RTP has its own length)
      // Filter out any undefined entries to prevent downstream errors
      const dataPackets = [];
      for (let i = 0; i < decoded.length; i++) {
        if (decoded[i] && decoded[i].length > 0) {
          dataPackets.push({ index: i, data: decoded[i] });
        }
      }
      this.onGroupDecoded(groupId, dataPackets, true);
    } catch (err) {
      // Return input buffers to pool even on error
      for (const buf of inPackets) {
        this.releaseBuffer(buf);
      }
      console.error(`FEC decode error for group ${groupId}:`, err);
      // Emit raw packets as fallback
      const dataPackets = [];
      for (let i = 0; i < k; i++) {
        if (packets.has(i)) {
          dataPackets.push({ index: i, data: packets.get(i) });
        }
      }
      this.onGroupDecoded(groupId, dataPackets, false);
    }
  }

  /**
   * Flush all pending groups
   */
  flushAll() {
    for (const groupId of this.groups.keys()) {
      this.flushGroup(groupId, true);
    }
  }

  /**
   * Destroy the buffer and clear all timers
   */
  destroy() {
    for (const group of this.groups.values()) {
      if (group.timer) {
        clearTimeout(group.timer);
      }
    }
    this.groups.clear();
  }
}
