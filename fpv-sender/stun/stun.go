// Package stun implements a minimal STUN client for NAT discovery (RFC 5389).
package stun

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"time"
)

// STUN message types
const (
	bindingRequest  = 0x0001
	bindingResponse = 0x0101
)

// STUN attributes
const (
	attrMappedAddress    = 0x0001
	attrXorMappedAddress = 0x0020
)

// STUN magic cookie (RFC 5389)
const magicCookie = 0x2112A442

// Default STUN servers
var DefaultServers = []string{
	"stun.cloudflare.com:3478",
	"stun.l.google.com:19302",
	"stun1.l.google.com:19302",
}

// Errors
var (
	ErrNoResponse   = errors.New("no STUN response received")
	ErrInvalidReply = errors.New("invalid STUN reply")
)

// Result contains the discovered endpoints.
type Result struct {
	LocalAddr  *net.UDPAddr // Local bound address
	PublicAddr *net.UDPAddr // Server-reflexive (public) address
	Server     string       // STUN server that responded
}

// Discover performs STUN binding to discover the public endpoint.
// It uses the provided UDP connection (which should already be bound).
func Discover(ctx context.Context, conn *net.UDPConn, servers []string) (*Result, error) {
	if servers == nil {
		servers = DefaultServers
	}

	localAddr := conn.LocalAddr().(*net.UDPAddr)

	for _, server := range servers {
		serverAddr, err := net.ResolveUDPAddr("udp4", server)
		if err != nil {
			continue
		}

		publicAddr, err := doBinding(ctx, conn, serverAddr)
		if err != nil {
			continue
		}

		return &Result{
			LocalAddr:  localAddr,
			PublicAddr: publicAddr,
			Server:     server,
		}, nil
	}

	return nil, ErrNoResponse
}

// doBinding sends a STUN binding request and waits for response.
func doBinding(ctx context.Context, conn *net.UDPConn, server *net.UDPAddr) (*net.UDPAddr, error) {
	// Generate transaction ID (12 bytes)
	txnID := make([]byte, 12)
	if _, err := rand.Read(txnID); err != nil {
		return nil, err
	}

	// Build request: type (2) + length (2) + magic cookie (4) + txn_id (12) = 20 bytes
	req := make([]byte, 20)
	binary.BigEndian.PutUint16(req[0:2], bindingRequest)
	binary.BigEndian.PutUint16(req[2:4], 0) // length = 0 (no attributes)
	binary.BigEndian.PutUint32(req[4:8], magicCookie)
	copy(req[8:20], txnID)

	// Set read deadline
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = time.Now().Add(2 * time.Second)
	}
	conn.SetReadDeadline(deadline)

	// Send request (with retries)
	for attempt := 0; attempt < 3; attempt++ {
		if _, err := conn.WriteToUDP(req, server); err != nil {
			return nil, err
		}

		// Wait for response
		buf := make([]byte, 1024)
		n, _, err := conn.ReadFromUDP(buf)
		if err != nil {
			if nerr, ok := err.(net.Error); ok && nerr.Timeout() {
				continue // Retry on timeout
			}
			return nil, err
		}

		// Parse response
		addr, err := parseResponse(buf[:n], txnID)
		if err != nil {
			continue
		}
		return addr, nil
	}

	return nil, ErrNoResponse
}

// parseResponse parses a STUN binding response.
func parseResponse(buf []byte, expectedTxnID []byte) (*net.UDPAddr, error) {
	if len(buf) < 20 {
		return nil, ErrInvalidReply
	}

	msgType := binary.BigEndian.Uint16(buf[0:2])
	if msgType != bindingResponse {
		return nil, ErrInvalidReply
	}

	msgLen := binary.BigEndian.Uint16(buf[2:4])
	magic := binary.BigEndian.Uint32(buf[4:8])
	if magic != magicCookie {
		return nil, ErrInvalidReply
	}

	// Check transaction ID
	if len(buf) < 20 {
		return nil, ErrInvalidReply
	}
	for i := 0; i < 12; i++ {
		if buf[8+i] != expectedTxnID[i] {
			return nil, ErrInvalidReply
		}
	}

	// Parse attributes
	offset := 20
	end := 20 + int(msgLen)
	if end > len(buf) {
		return nil, ErrInvalidReply
	}

	for offset+4 <= end {
		attrType := binary.BigEndian.Uint16(buf[offset : offset+2])
		attrLen := binary.BigEndian.Uint16(buf[offset+2 : offset+4])
		attrData := buf[offset+4 : offset+4+int(attrLen)]

		if attrType == attrXorMappedAddress && attrLen >= 8 {
			// XOR-MAPPED-ADDRESS (preferred)
			family := attrData[1]
			if family == 0x01 { // IPv4
				xport := binary.BigEndian.Uint16(attrData[2:4])
				port := xport ^ uint16(magicCookie>>16)

				xaddr := binary.BigEndian.Uint32(attrData[4:8])
				addr := xaddr ^ magicCookie

				ip := net.IPv4(
					byte(addr>>24),
					byte(addr>>16),
					byte(addr>>8),
					byte(addr),
				)
				return &net.UDPAddr{IP: ip, Port: int(port)}, nil
			}
		} else if attrType == attrMappedAddress && attrLen >= 8 {
			// MAPPED-ADDRESS (fallback)
			family := attrData[1]
			if family == 0x01 { // IPv4
				port := binary.BigEndian.Uint16(attrData[2:4])
				ip := net.IPv4(attrData[4], attrData[5], attrData[6], attrData[7])
				return &net.UDPAddr{IP: ip, Port: int(port)}, nil
			}
		}

		// Move to next attribute (padded to 4-byte boundary)
		offset += 4 + int((attrLen+3) & ^uint16(3))
	}

	return nil, ErrInvalidReply
}

// DiscoverWithNewSocket creates a new UDP socket, performs STUN, and returns both.
func DiscoverWithNewSocket(ctx context.Context, localPort int) (*net.UDPConn, *Result, error) {
	addr := &net.UDPAddr{IP: net.IPv4zero, Port: localPort}
	conn, err := net.ListenUDP("udp4", addr)
	if err != nil {
		return nil, nil, fmt.Errorf("failed to create UDP socket: %w", err)
	}

	result, err := Discover(ctx, conn, nil)
	if err != nil {
		conn.Close()
		return nil, nil, err
	}

	return conn, result, nil
}
