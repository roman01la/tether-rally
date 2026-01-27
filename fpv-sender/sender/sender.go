// Package sender implements the video packetizer and UDP sender.
package sender

import (
	"fmt"
	"net"
	"sync/atomic"
	"time"

	"fpv-sender/h264"
	"fpv-sender/protocol"
)

// Pacing configuration
const (
	// Target ~2Mbps = 250KB/s = ~208 packets/s at 1200 bytes
	// So ~4.8ms between packets, but we send in bursts per frame
	// At 60fps with ~5 packets/frame, pace within the 16ms frame time
	PacketPaceInterval = 200 * time.Microsecond // 200µs between packets
)

// Config holds sender configuration.
type Config struct {
	MaxPayloadSize int // Max UDP payload (default 1200)
	StreamID       uint32
}

// DefaultConfig returns the default sender configuration.
func DefaultConfig() Config {
	return Config{
		MaxPayloadSize: protocol.MaxPayloadSize,
		StreamID:       1,
	}
}

// Packetizer fragments Access Units into UDP packets.
type Packetizer struct {
	config    Config
	sessionID uint32
	frameID   uint32
	startTime time.Time
	buf       []byte // Reusable packet buffer
}

// NewPacketizer creates a new packetizer.
func NewPacketizer(sessionID uint32, config Config) *Packetizer {
	if config.MaxPayloadSize == 0 {
		config.MaxPayloadSize = protocol.MaxPayloadSize
	}
	return &Packetizer{
		config:    config,
		sessionID: sessionID,
		startTime: time.Now(),
		buf:       make([]byte, config.MaxPayloadSize),
	}
}

// MaxFragmentPayload returns the max payload bytes per fragment.
func (p *Packetizer) MaxFragmentPayload() int {
	return p.config.MaxPayloadSize - protocol.VideoFragmentHeaderSize
}

// Packetize fragments an Access Unit and sends each fragment via the provided function.
// The sendFn receives the complete packet ready to send.
// Returns the number of fragments sent.
func (p *Packetizer) Packetize(au *h264.AccessUnit, sendFn func([]byte) error) (int, error) {
	data := au.Data
	maxPayload := p.MaxFragmentPayload()

	// Calculate fragment count
	fragCount := (len(data) + maxPayload - 1) / maxPayload
	if fragCount == 0 {
		fragCount = 1
	}
	if fragCount > 65535 {
		return 0, fmt.Errorf("AU too large: %d bytes would need %d fragments", len(data), fragCount)
	}

	// Get current frame ID and increment
	frameID := atomic.AddUint32(&p.frameID, 1) - 1

	// Calculate timestamp (ms since start)
	tsMs := uint32(time.Since(p.startTime).Milliseconds())

	// Build flags
	var flags uint8
	if au.IsKeyframe {
		flags |= protocol.FlagKeyframe
	}
	if au.HasSPSPPS {
		flags |= protocol.FlagSPSPPS
	}

	// Send each fragment with pacing to avoid burst loss
	sent := 0
	for i := 0; i < fragCount; i++ {
		start := i * maxPayload
		end := start + maxPayload
		if end > len(data) {
			end = len(data)
		}
		payload := data[start:end]

		frag := protocol.VideoFragment{
			SessionID:  p.sessionID,
			StreamID:   p.config.StreamID,
			FrameID:    frameID,
			FragIndex:  uint16(i),
			FragCount:  uint16(fragCount),
			TsMs:       tsMs,
			Flags:      flags,
			Codec:      protocol.CodecH264,
			PayloadLen: uint16(len(payload)),
			Payload:    payload,
		}

		n, err := frag.Marshal(p.buf)
		if err != nil {
			return sent, err
		}

		if err := sendFn(p.buf[:n]); err != nil {
			// Per spec: if send fails, drop remainder and continue to next AU
			return sent, err
		}
		sent++

		// Pace packets to avoid overwhelming the network
		if i < fragCount-1 {
			time.Sleep(PacketPaceInterval)
		}
	}

	return sent, nil
}

// FrameID returns the current frame ID.
func (p *Packetizer) FrameID() uint32 {
	return atomic.LoadUint32(&p.frameID)
}

// Stats holds sender statistics.
type Stats struct {
	FramesSent    uint64
	FragmentsSent uint64
	BytesSent     uint64
	SendErrors    uint64
	KeyframesSent uint64
}

// Sender manages the UDP connection and sends video.
type Sender struct {
	conn       *net.UDPConn
	peerAddr   *net.UDPAddr
	packetizer *Packetizer
	stats      Stats
}

// NewSender creates a new sender.
func NewSender(conn *net.UDPConn, peerAddr *net.UDPAddr, sessionID uint32) *Sender {
	return &Sender{
		conn:       conn,
		peerAddr:   peerAddr,
		packetizer: NewPacketizer(sessionID, DefaultConfig()),
	}
}

// SendAccessUnit sends a complete Access Unit.
func (s *Sender) SendAccessUnit(au *h264.AccessUnit) error {
	n, err := s.packetizer.Packetize(au, func(packet []byte) error {
		_, err := s.conn.WriteToUDP(packet, s.peerAddr)
		if err != nil {
			atomic.AddUint64(&s.stats.SendErrors, 1)
			return err
		}
		atomic.AddUint64(&s.stats.BytesSent, uint64(len(packet)))
		return nil
	})

	atomic.AddUint64(&s.stats.FragmentsSent, uint64(n))
	if err == nil {
		atomic.AddUint64(&s.stats.FramesSent, 1)
		if au.IsKeyframe {
			atomic.AddUint64(&s.stats.KeyframesSent, 1)
		}
	}
	return err
}

// SendKeepalive sends a keepalive packet.
func (s *Sender) SendKeepalive(sessionID uint32, seq uint32, echoTsMs uint32) error {
	k := protocol.Keepalive{
		SessionID: sessionID,
		TsMs:      uint32(time.Since(s.packetizer.startTime).Milliseconds()),
		Seq:       seq,
		EchoTsMs:  echoTsMs,
	}

	buf := make([]byte, protocol.KeepaliveHeaderSize)
	_, err := k.Marshal(buf)
	if err != nil {
		return err
	}

	_, err = s.conn.WriteToUDP(buf, s.peerAddr)
	return err
}

// SendProbe sends a probe packet for hole punching.
func (s *Sender) SendProbe(sessionID uint32, seq uint32, nonce uint64) error {
	p := protocol.Probe{
		SessionID: sessionID,
		TsMs:      uint32(time.Since(s.packetizer.startTime).Milliseconds()),
		ProbeSeq:  seq,
		Nonce:     nonce,
		Role:      protocol.RolePi,
		Flags:     0,
	}

	buf := make([]byte, protocol.ProbeHeaderSize)
	_, err := p.Marshal(buf)
	if err != nil {
		return err
	}

	_, err = s.conn.WriteToUDP(buf, s.peerAddr)
	return err
}

// Stats returns current statistics.
func (s *Sender) Stats() Stats {
	return Stats{
		FramesSent:    atomic.LoadUint64(&s.stats.FramesSent),
		FragmentsSent: atomic.LoadUint64(&s.stats.FragmentsSent),
		BytesSent:     atomic.LoadUint64(&s.stats.BytesSent),
		SendErrors:    atomic.LoadUint64(&s.stats.SendErrors),
		KeyframesSent: atomic.LoadUint64(&s.stats.KeyframesSent),
	}
}

// SetPeerAddr updates the peer address (after hole punching).
func (s *Sender) SetPeerAddr(addr *net.UDPAddr) {
	s.peerAddr = addr
}
