"""
STUN Client for Pi

Implements RFC 5389 STUN Binding Request/Response
Uses dual-query to detect symmetric NAT

Usage:
    from stun_client import discover_endpoint, get_public_endpoint
    
    # Full NAT detection
    endpoint = await discover_endpoint(sock)
    print(f"Public: {endpoint['ip']}:{endpoint['port']}, Symmetric: {endpoint['is_symmetric']}")
    
    # Simple single query
    endpoint = await get_public_endpoint(sock)
"""

import asyncio
import socket
import struct
import os
import logging

logger = logging.getLogger(__name__)

# STUN message types
BINDING_REQUEST = 0x0001
BINDING_RESPONSE = 0x0101

# STUN attribute types
MAPPED_ADDRESS = 0x0001
XOR_MAPPED_ADDRESS = 0x0020

# STUN magic cookie (RFC 5389)
MAGIC_COOKIE = 0x2112A442

# Default STUN servers for NAT detection
STUN_SERVERS = [
    ("stun.cloudflare.com", 3478),
    ("stun.l.google.com", 19302),
]


def generate_transaction_id() -> bytes:
    """Generate random 12-byte transaction ID."""
    return os.urandom(12)


def build_binding_request(transaction_id: bytes) -> bytes:
    """Build STUN Binding Request (20 bytes header, no attributes)."""
    return struct.pack(
        ">HHI",
        BINDING_REQUEST,  # Message type
        0,                # Message length (no attributes)
        MAGIC_COOKIE,     # Magic cookie
    ) + transaction_id


def parse_binding_response(data: bytes, transaction_id: bytes) -> dict | None:
    """
    Parse STUN Binding Response, extract XOR-MAPPED-ADDRESS.
    Returns {'ip': str, 'port': int} or None.
    """
    if len(data) < 20:
        return None

    # Parse header
    msg_type, msg_length, cookie = struct.unpack(">HHI", data[:8])

    if msg_type != BINDING_RESPONSE:
        return None
    if cookie != MAGIC_COOKIE:
        return None
    if data[8:20] != transaction_id:
        return None

    # Parse attributes
    offset = 20
    end = 20 + msg_length

    while offset + 4 <= end:
        attr_type, attr_length = struct.unpack(">HH", data[offset:offset + 4])
        attr_start = offset + 4

        if attr_type == XOR_MAPPED_ADDRESS and attr_length >= 8:
            # Reserved (1 byte) + Family (1 byte) + Port (2 bytes) + Address (4 bytes)
            _, family, xor_port, xor_addr = struct.unpack(
                ">BBHI", data[attr_start:attr_start + 8]
            )

            if family == 0x01:  # IPv4
                port = xor_port ^ (MAGIC_COOKIE >> 16)
                addr = xor_addr ^ MAGIC_COOKIE

                ip = socket.inet_ntoa(struct.pack(">I", addr))
                return {"ip": ip, "port": port}

        elif attr_type == MAPPED_ADDRESS and attr_length >= 8:
            # Fallback to non-XOR mapped address
            _, family, port, addr = struct.unpack(
                ">BBHI", data[attr_start:attr_start + 8]
            )

            if family == 0x01:  # IPv4
                ip = socket.inet_ntoa(struct.pack(">I", addr))
                return {"ip": ip, "port": port}

        # Move to next attribute (aligned to 4 bytes)
        offset = attr_start + ((attr_length + 3) // 4) * 4

    return None


async def query_stun_server(
    sock: socket.socket,
    host: str,
    port: int,
    timeout: float = 3.0
) -> dict | None:
    """
    Query a single STUN server.
    
    Args:
        sock: Bound UDP socket
        host: STUN server hostname
        port: STUN server port
        timeout: Timeout in seconds
        
    Returns:
        {'ip': str, 'port': int} or None
    """
    loop = asyncio.get_event_loop()
    transaction_id = generate_transaction_id()
    request = build_binding_request(transaction_id)

    # Resolve hostname
    try:
        infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        if not infos:
            logger.warning(f"Failed to resolve {host}")
            return None
        server_addr = infos[0][4]  # (ip, port)
    except Exception as e:
        logger.warning(f"DNS resolution failed for {host}: {e}")
        return None

    # Send request
    try:
        sock.sendto(request, server_addr)
    except Exception as e:
        logger.warning(f"Failed to send STUN request to {host}: {e}")
        return None

    # Wait for response with retries
    sock.setblocking(False)
    start_time = asyncio.get_event_loop().time()

    while True:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= timeout:
            return None

        remaining = timeout - elapsed

        try:
            # Use asyncio to wait for socket to be readable
            await asyncio.wait_for(
                loop.sock_recv(sock, 1024),
                timeout=min(remaining, 0.5)
            )
        except asyncio.TimeoutError:
            # Retry
            try:
                sock.sendto(request, server_addr)
            except:
                pass
            continue
        except Exception as e:
            logger.warning(f"Error receiving STUN response: {e}")
            continue

    return None


async def query_stun_server_simple(
    sock: socket.socket,
    host: str,
    port: int,
    timeout: float = 3.0
) -> dict | None:
    """
    Query a single STUN server (simpler implementation using threads).
    
    Args:
        sock: Bound UDP socket
        host: STUN server hostname  
        port: STUN server port
        timeout: Timeout in seconds
        
    Returns:
        {'ip': str, 'port': int} or None
    """
    loop = asyncio.get_event_loop()
    transaction_id = generate_transaction_id()
    request = build_binding_request(transaction_id)

    # Resolve hostname
    try:
        infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        if not infos:
            logger.warning(f"Failed to resolve {host}")
            return None
        server_addr = infos[0][4]  # (ip, port)
    except Exception as e:
        logger.warning(f"DNS resolution failed for {host}: {e}")
        return None

    def do_stun():
        """Blocking STUN query."""
        sock.settimeout(timeout)
        retries = 3
        
        try:
            for attempt in range(retries):
                try:
                    sock.sendto(request, server_addr)
                    data, addr = sock.recvfrom(1024)
                    
                    result = parse_binding_response(data, transaction_id)
                    if result:
                        return result
                        
                except socket.timeout:
                    continue
                except Exception as e:
                    logger.warning(f"STUN query attempt {attempt + 1} failed: {e}")
                    continue
            
            return None
        finally:
            # Restore non-blocking mode for asyncio compatibility
            sock.setblocking(False)

    # Run blocking operation in thread pool
    return await loop.run_in_executor(None, do_stun)


async def discover_endpoint(sock: socket.socket) -> dict:
    """
    Discover public endpoint with NAT type detection.
    
    Queries two different STUN servers from the same socket.
    If both return different ports, it's symmetric NAT.
    
    Args:
        sock: Bound UDP socket
        
    Returns:
        {
            'ip': str,
            'port': int,
            'is_symmetric': bool
        }
        
    Raises:
        RuntimeError: If all STUN servers fail
    """
    # Query both servers (not in parallel - same socket)
    result1 = await query_stun_server_simple(
        sock, STUN_SERVERS[0][0], STUN_SERVERS[0][1]
    )
    result2 = await query_stun_server_simple(
        sock, STUN_SERVERS[1][0], STUN_SERVERS[1][1]
    )

    if not result1 and not result2:
        raise RuntimeError("Failed to contact any STUN server")

    # Use first successful result as public endpoint
    primary = result1 or result2

    # Check for symmetric NAT (different ports for different destinations)
    is_symmetric = False
    if result1 and result2:
        is_symmetric = result1["port"] != result2["port"]
        if is_symmetric:
            logger.warning(
                f"Symmetric NAT detected: port {result1['port']} vs {result2['port']}"
            )

    logger.info(
        f"STUN discovery: {primary['ip']}:{primary['port']} "
        f"(symmetric={is_symmetric})"
    )

    return {
        "ip": primary["ip"],
        "port": primary["port"],
        "is_symmetric": is_symmetric,
    }


async def get_public_endpoint(sock: socket.socket) -> dict:
    """
    Simple single-server STUN query (for cases where NAT detection not needed).
    
    Args:
        sock: Bound UDP socket
        
    Returns:
        {'ip': str, 'port': int}
        
    Raises:
        RuntimeError: If STUN query fails
    """
    result = await query_stun_server_simple(
        sock, STUN_SERVERS[0][0], STUN_SERVERS[0][1]
    )

    if not result:
        raise RuntimeError("STUN query failed")

    logger.info(f"Public endpoint: {result['ip']}:{result['port']}")
    return result


# For testing
if __name__ == "__main__":
    async def main():
        logging.basicConfig(level=logging.INFO)
        
        # Create and bind socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        local_port = sock.getsockname()[1]
        print(f"Local port: {local_port}")
        
        try:
            endpoint = await discover_endpoint(sock)
            print(f"Public IP: {endpoint['ip']}")
            print(f"Public Port: {endpoint['port']}")
            print(f"Symmetric NAT: {endpoint['is_symmetric']}")
        finally:
            sock.close()

    asyncio.run(main())
