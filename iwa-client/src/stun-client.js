/**
 * STUN Client for IWA Direct Sockets
 *
 * Implements RFC 5389 STUN Binding Request/Response
 * Uses dual-query to detect symmetric NAT
 */

// STUN message types
const BINDING_REQUEST = 0x0001;
const BINDING_RESPONSE = 0x0101;

// STUN attribute types
const MAPPED_ADDRESS = 0x0001;
const XOR_MAPPED_ADDRESS = 0x0020;

// STUN magic cookie (RFC 5389)
const MAGIC_COOKIE = 0x2112a442;

// Default STUN servers for NAT detection
const STUN_SERVERS = [
  { host: "stun.cloudflare.com", port: 3478 },
  { host: "stun.l.google.com", port: 19302 },
];

/**
 * Generate random transaction ID (12 bytes)
 */
function generateTransactionId() {
  const id = new Uint8Array(12);
  crypto.getRandomValues(id);
  return id;
}

/**
 * Build STUN Binding Request (20 bytes header, no attributes)
 */
function buildBindingRequest(transactionId) {
  const buffer = new ArrayBuffer(20);
  const view = new DataView(buffer);

  // Message Type: Binding Request (0x0001)
  view.setUint16(0, BINDING_REQUEST, false);

  // Message Length: 0 (no attributes)
  view.setUint16(2, 0, false);

  // Magic Cookie
  view.setUint32(4, MAGIC_COOKIE, false);

  // Transaction ID (12 bytes)
  const arr = new Uint8Array(buffer);
  arr.set(transactionId, 8);

  return new Uint8Array(buffer);
}

/**
 * Parse STUN Binding Response, extract XOR-MAPPED-ADDRESS
 * Returns { ip: string, port: number } or null
 */
function parseBindingResponse(data, transactionId) {
  if (data.byteLength < 20) return null;

  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);

  // Check message type
  const msgType = view.getUint16(0, false);
  if (msgType !== BINDING_RESPONSE) return null;

  // Check magic cookie
  const cookie = view.getUint32(4, false);
  if (cookie !== MAGIC_COOKIE) return null;

  // Check transaction ID
  for (let i = 0; i < 12; i++) {
    if (data[8 + i] !== transactionId[i]) return null;
  }

  // Parse attributes
  const msgLength = view.getUint16(2, false);
  let offset = 20;
  const end = 20 + msgLength;

  while (offset + 4 <= end) {
    const attrType = view.getUint16(offset, false);
    const attrLength = view.getUint16(offset + 2, false);
    const attrStart = offset + 4;

    if (attrType === XOR_MAPPED_ADDRESS && attrLength >= 8) {
      // Family (1 byte padding + 1 byte family)
      const family = view.getUint8(attrStart + 1);

      if (family === 0x01) {
        // IPv4
        // XOR'd port (with upper 16 bits of magic cookie)
        const xorPort = view.getUint16(attrStart + 2, false);
        const port = xorPort ^ (MAGIC_COOKIE >>> 16);

        // XOR'd address (with magic cookie)
        const xorAddr = view.getUint32(attrStart + 4, false);
        const addr = xorAddr ^ MAGIC_COOKIE;

        const ip = [
          (addr >>> 24) & 0xff,
          (addr >>> 16) & 0xff,
          (addr >>> 8) & 0xff,
          addr & 0xff,
        ].join(".");

        return { ip, port };
      }
      // IPv6 not supported for now
    } else if (attrType === MAPPED_ADDRESS && attrLength >= 8) {
      // Fallback to non-XOR mapped address (older servers)
      const family = view.getUint8(attrStart + 1);

      if (family === 0x01) {
        const port = view.getUint16(attrStart + 2, false);
        const addr = view.getUint32(attrStart + 4, false);

        const ip = [
          (addr >>> 24) & 0xff,
          (addr >>> 16) & 0xff,
          (addr >>> 8) & 0xff,
          addr & 0xff,
        ].join(".");

        return { ip, port };
      }
    }

    // Move to next attribute (aligned to 4 bytes)
    offset = attrStart + Math.ceil(attrLength / 4) * 4;
  }

  return null;
}

/**
 * Query a single STUN server
 * @param {ReadableStream} readable - Socket readable stream
 * @param {WritableStreamDefaultWriter} writer - Writer for the socket
 * @param {string} host - STUN server hostname
 * @param {number} port - STUN server port
 * @param {number} timeout - Timeout in ms
 * @returns {Promise<{ip: string, port: number} | null>}
 */
async function queryStunServer(readable, writer, host, port, timeout = 3000) {
  const transactionId = generateTransactionId();
  const request = buildBindingRequest(transactionId);

  // Resolve hostname using DNS-over-HTTPS
  const { addresses } = await fetch(
    `https://dns.google/resolve?name=${host}&type=A`,
  )
    .then((r) => r.json())
    .then((data) => ({
      addresses:
        data.Answer?.filter((a) => a.type === 1).map((a) => a.data) || [],
    }));

  if (addresses.length === 0) {
    throw new Error(`Failed to resolve ${host}`);
  }

  const serverIp = addresses[0];

  // Send STUN request
  await writer.write({
    data: request,
    remoteAddress: serverIp,
    remotePort: port,
  });

  // Read responses with timeout
  const startTime = Date.now();
  const reader = readable.getReader();

  try {
    while (Date.now() - startTime < timeout) {
      const readPromise = reader.read();
      const timeoutPromise = new Promise((resolve) =>
        setTimeout(
          () => resolve({ timeout: true }),
          Math.max(100, timeout - (Date.now() - startTime)),
        ),
      );

      const result = await Promise.race([readPromise, timeoutPromise]);

      if (result.timeout) {
        // Retry once
        await writer.write({
          data: request,
          remoteAddress: serverIp,
          remotePort: port,
        });
        continue;
      }

      if (result.done) break;

      const { data, remoteAddress, remotePort } = result.value;

      // Only process responses from the server we queried
      if (remoteAddress !== serverIp || remotePort !== port) continue;

      const parsed = parseBindingResponse(new Uint8Array(data), transactionId);
      if (parsed) {
        return parsed;
      }
    }
  } finally {
    reader.releaseLock();
  }

  return null;
}

/**
 * Discover public endpoint with NAT type detection
 *
 * Queries two different STUN servers from the same socket.
 * If both return different ports, it's symmetric NAT.
 *
 * @param {ReadableStream} readable - Socket readable stream
 * @param {WritableStreamDefaultWriter} writer - Socket writer
 * @returns {Promise<{publicIp: string, publicPort: number, isSymmetric: boolean}>}
 */
export async function discoverEndpoint(readable, writer) {
  // Query STUN servers sequentially (same socket, can't read in parallel)
  const result1 = await queryStunServer(
    readable,
    writer,
    STUN_SERVERS[0].host,
    STUN_SERVERS[0].port,
  );
  const result2 = await queryStunServer(
    readable,
    writer,
    STUN_SERVERS[1].host,
    STUN_SERVERS[1].port,
  );

  if (!result1 && !result2) {
    throw new Error("Failed to contact any STUN server");
  }

  // Use first successful result as public endpoint
  const primary = result1 || result2;

  // Check for symmetric NAT (different ports for different destinations)
  let isSymmetric = false;
  if (result1 && result2) {
    isSymmetric = result1.port !== result2.port;
  }

  return {
    publicIp: primary.ip,
    publicPort: primary.port,
    isSymmetric,
  };
}

/**
 * Simple single-server STUN query (for cases where NAT detection not needed)
 * @param {ReadableStream} readable - Socket readable stream
 * @param {WritableStreamDefaultWriter} writer - Socket writer
 * @returns {Promise<{publicIp: string, publicPort: number}>}
 */
export async function getPublicEndpoint(readable, writer) {
  const result = await queryStunServer(
    readable,
    writer,
    STUN_SERVERS[0].host,
    STUN_SERVERS[0].port,
  );

  if (!result) {
    throw new Error("STUN query failed");
  }

  return {
    publicIp: result.ip,
    publicPort: result.port,
  };
}
