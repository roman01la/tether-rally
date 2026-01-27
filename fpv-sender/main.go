// FPV Sender - Pi side of the ultra-low-latency video link
//
// This program:
// 1. Discovers its public endpoint via STUN
// 2. Exchanges candidates with receiver via signaling server
// 3. Performs UDP hole punching
// 4. Reads H.264 from rpicam-vid and sends fragments to receiver
//
// Usage:
//   FPV_SIGNAL_URL=https://signal.example.com/fpv ./fpv-sender
//
// Or for local testing:
//   rpicam-vid -t 0 --codec h264 --inline -o - | ./fpv-sender --local 192.168.1.100:9000

package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/binary"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"fpv-sender/h264"
	"fpv-sender/protocol"
	"fpv-sender/sender"
	"fpv-sender/stun"
)

// Configuration from FPV_PLAN.md
const (
	// Video settings - 960x540@30fps (Pi can sustain this reliably through pipe)
	// Note: 60fps not achievable via stdout pipe - MediaMTX uses direct libcamera
	Width     = 960
	Height    = 540
	FPS       = 30
	Bitrate   = 1_500_000 // 1.5 Mbps
	IDRPeriod = 15        // frames (0.5 second at 30fps)

	// Exposure settings - lock shutter for consistent frame timing
	ShutterUS = 33333 // 1/30s max exposure (ensures 30fps)
	Gain      = 4     // Fixed gain

	// Timing constants
	ProbeIntervalMS      = 20   // 50 Hz probe rate
	PunchWindowMS        = 3000 // 3s window for hole punching
	KeepaliveIntervalMS  = 1000 // 1s keepalive
	SessionIdleTimeoutMS = 3000 // 3s timeout

	// Socket settings
	SocketSendBufSize = 256 * 1024 // 256 KB
)

// State machine states
type State int

const (
	StateIdle State = iota
	StateSTUNGather
	StateExchangeCandidates
	StatePunching
	StateConnected
	StateStreaming
	StateReconnecting
	StateFailed
)

func (s State) String() string {
	names := []string{"IDLE", "STUN_GATHER", "EXCHANGE_CANDIDATES", "PUNCHING", "CONNECTED", "STREAMING", "RECONNECTING", "FAILED"}
	if int(s) < len(names) {
		return names[s]
	}
	return "UNKNOWN"
}

// App holds the application state
type App struct {
	// Configuration
	signalURL   string
	localTarget string // For local testing: direct IP:port
	localPort   int

	// Network
	conn       *net.UDPConn
	localAddr  *net.UDPAddr
	publicAddr *net.UDPAddr
	peerAddr   *net.UDPAddr

	// Session
	sessionID uint32
	nonce     uint64
	state     State

	// Sender
	snd *sender.Sender

	// Statistics
	lastRxTime   time.Time
	lastRxTsMs   uint32
	probeSeq     uint32
	keepaliveSeq uint32

	// Shutdown
	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

func main() {
	// Parse flags
	localTarget := flag.String("local", "", "Direct target IP:port for local testing (skip signaling)")
	localPort := flag.Int("port", 0, "Local UDP port to bind (0 for auto)")
	signalURL := flag.String("signal", os.Getenv("FPV_SIGNAL_URL"), "Signaling server URL")
	flag.Parse()

	app := &App{
		signalURL:   *signalURL,
		localTarget: *localTarget,
		localPort:   *localPort,
	}

	// Setup context with signal handling
	app.ctx, app.cancel = context.WithCancel(context.Background())
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		log.Println("Shutting down...")
		app.cancel()
	}()

	if err := app.Run(); err != nil {
		log.Fatalf("Error: %v", err)
	}
}

func (a *App) Run() error {
	// Generate session ID and nonce
	var buf [12]byte
	if _, err := rand.Read(buf[:]); err != nil {
		return fmt.Errorf("failed to generate random: %w", err)
	}
	a.sessionID = binary.BigEndian.Uint32(buf[0:4])
	a.nonce = binary.BigEndian.Uint64(buf[4:12])

	log.Printf("Session ID: %08x, Nonce: %016x", a.sessionID, a.nonce)

	// Create UDP socket
	addr := &net.UDPAddr{IP: net.IPv4zero, Port: a.localPort}
	conn, err := net.ListenUDP("udp4", addr)
	if err != nil {
		return fmt.Errorf("failed to create UDP socket: %w", err)
	}
	defer conn.Close()
	a.conn = conn
	a.localAddr = conn.LocalAddr().(*net.UDPAddr)

	// Set socket buffer size
	if err := conn.SetWriteBuffer(SocketSendBufSize); err != nil {
		log.Printf("Warning: failed to set send buffer: %v", err)
	}

	log.Printf("Local address: %s", a.localAddr)

	// Direct local mode (for testing)
	if a.localTarget != "" {
		return a.runLocalMode()
	}

	// Full P2P mode with signaling
	return a.runP2PMode()
}

// runLocalMode connects directly to a specified address (for LAN testing)
func (a *App) runLocalMode() error {
	peerAddr, err := net.ResolveUDPAddr("udp4", a.localTarget)
	if err != nil {
		return fmt.Errorf("invalid target address: %w", err)
	}
	a.peerAddr = peerAddr
	a.state = StateConnected

	log.Printf("Local mode: sending to %s", a.peerAddr)

	// Create sender
	a.snd = sender.NewSender(a.conn, a.peerAddr, a.sessionID)

	// Start receiver goroutine (for IDR requests)
	a.wg.Add(1)
	go a.receiveLoop()

	// Start keepalive goroutine
	a.wg.Add(1)
	go a.keepaliveLoop()

	// Start streaming
	a.state = StateStreaming
	return a.streamVideo()
}

// runP2PMode performs full P2P connection with STUN and signaling
func (a *App) runP2PMode() error {
	if a.signalURL == "" {
		return fmt.Errorf("FPV_SIGNAL_URL not set and no --local target specified")
	}

	// Phase 1: STUN discovery
	a.state = StateSTUNGather
	log.Println("Discovering public endpoint via STUN...")

	ctx, cancel := context.WithTimeout(a.ctx, 10*time.Second)
	result, err := stun.Discover(ctx, a.conn, nil)
	cancel()
	if err != nil {
		return fmt.Errorf("STUN discovery failed: %w", err)
	}
	a.publicAddr = result.PublicAddr
	log.Printf("Public address: %s (via %s)", a.publicAddr, result.Server)

	// Phase 2: Exchange candidates via signaling
	a.state = StateExchangeCandidates
	log.Println("Exchanging candidates...")
	// TODO: Implement signaling client
	// For now, return error
	return fmt.Errorf("signaling not yet implemented - use --local for testing")
}

// streamVideo reads from rpicam-vid and sends packets
func (a *App) streamVideo() error {
	// Check if we're reading from stdin (pipe mode)
	stat, _ := os.Stdin.Stat()
	isPipe := (stat.Mode() & os.ModeCharDevice) == 0

	var input io.Reader
	var cmd *exec.Cmd

	if isPipe {
		log.Println("Reading H.264 from stdin...")
		input = os.Stdin
	} else {
		log.Println("Starting rpicam-vid...")
		cmd = exec.CommandContext(a.ctx, "rpicam-vid",
			"-t", "0",
			"--width", fmt.Sprintf("%d", Width),
			"--height", fmt.Sprintf("%d", Height),
			"--framerate", fmt.Sprintf("%d", FPS),
			"--bitrate", fmt.Sprintf("%d", Bitrate),
			"--profile", "baseline",
			"--level", "4.2",
			"--intra", fmt.Sprintf("%d", IDRPeriod),
			"--inline",         // Include SPS/PPS with each IDR
			"--flush",          // Flush output buffers immediately
			"--denoise", "off", // Disable denoising for speed
			// CRITICAL: Lock exposure to guarantee consistent FPS
			"--shutter", fmt.Sprintf("%d", ShutterUS), // Max exposure time
			"--gain", fmt.Sprintf("%d", Gain), // Fixed gain
			"--awbgains", "1.5,1.2", // Lock AWB to reduce hunting
			"--codec", "h264",
			"-n", // No preview
			"-o", "-",
		)
		stdout, err := cmd.StdoutPipe()
		if err != nil {
			return fmt.Errorf("failed to get stdout pipe: %w", err)
		}
		// Don't forward stderr - rpicam-vid is very chatty
		if err := cmd.Start(); err != nil {
			return fmt.Errorf("failed to start rpicam-vid: %w", err)
		}
		defer cmd.Wait()
		input = stdout
	}

	// Create H.264 reader with small buffer for low latency
	reader := h264.NewReader(bufio.NewReaderSize(input, 64*1024))

	// Read and send frames
	log.Println("Streaming...")
	frameCount := uint64(0)
	lastFrameTime := time.Now()
	var totalInterval, maxInterval time.Duration
	var longIntervalCount int

	// Expected interval based on FPS (with 50% margin for "long")
	expectedInterval := time.Second / FPS
	longThreshold := expectedInterval * 3 / 2 // 1.5x expected = "long"

	for {
		select {
		case <-a.ctx.Done():
			return nil
		default:
		}

		au, err := reader.ReadAccessUnit()
		if err != nil {
			if err == io.EOF {
				log.Println("End of stream")
				return nil
			}
			log.Printf("Read error: %v", err)
			continue
		}

		// Track frame timing
		now := time.Now()
		interval := now.Sub(lastFrameTime)
		lastFrameTime = now
		totalInterval += interval
		if interval > maxInterval {
			maxInterval = interval
		}
		frameCount++

		// Count intervals > 1.5x expected (real problems, not normal jitter)
		if interval > longThreshold && frameCount > 10 {
			longIntervalCount++
			// Log details for first 5 long intervals
			if longIntervalCount <= 5 {
				nalTypes := ""
				for _, n := range au.NALs {
					nalTypes += fmt.Sprintf("%d ", n.Type)
				}
				log.Printf("[TIMING] Long interval %v (threshold %v), AU has %d NALs (types: %s), size=%d",
					interval, longThreshold, len(au.NALs), nalTypes, len(au.Data))
			}
		}

		// Log every 60 frames (~2s at 30fps)
		if frameCount%60 == 0 {
			avgInterval := totalInterval / time.Duration(frameCount)
			log.Printf("[TIMING] frame interval: avg=%v max=%v longCount=%d fps=%.1f",
				avgInterval, maxInterval, longIntervalCount, float64(time.Second)/float64(avgInterval))
			maxInterval = 0 // Reset max for next window
			longIntervalCount = 0
		}

		// Log IDR frames to verify SPS/PPS are included
		if au.IsKeyframe {
			nalTypes := ""
			for _, n := range au.NALs {
				nalTypes += fmt.Sprintf("%d ", n.Type)
			}
			log.Printf("[IDR] Keyframe #%d has %d NALs (types: %s), size=%d",
				frameCount, len(au.NALs), nalTypes, len(au.Data))
		}

		if err := a.snd.SendAccessUnit(au); err != nil {
			// Per spec: if send fails, drop and continue
			continue
		}
	}
}

// receiveLoop handles incoming packets (IDR requests, keepalives)
func (a *App) receiveLoop() {
	defer a.wg.Done()

	buf := make([]byte, 1500)
	for {
		select {
		case <-a.ctx.Done():
			return
		default:
		}

		a.conn.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
		n, addr, err := a.conn.ReadFromUDP(buf)
		if err != nil {
			if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
				continue
			}
			log.Printf("Receive error: %v", err)
			continue
		}

		a.lastRxTime = time.Now()

		// Parse message type
		if n < 1 {
			continue
		}
		msgType := buf[0]

		switch msgType {
		case protocol.MsgTypeIDRRequest:
			var req protocol.IDRRequest
			if err := req.Unmarshal(buf[:n]); err == nil {
				log.Printf("IDR request from %s (reason: %d)", addr, req.Reason)
				// TODO: Signal encoder to emit IDR
			}

		case protocol.MsgTypeKeepalive:
			var k protocol.Keepalive
			if err := k.Unmarshal(buf[:n]); err == nil {
				a.lastRxTsMs = k.TsMs
			}

		case protocol.MsgTypeProbe:
			var p protocol.Probe
			if err := p.Unmarshal(buf[:n]); err == nil {
				if p.SessionID == a.sessionID && p.Role == protocol.RoleMac {
					log.Printf("Valid probe from %s", addr)
					// Update peer address (use actual source)
					if a.state == StatePunching {
						a.peerAddr = addr
						a.state = StateConnected
					}
				}
			}
		}
	}
}

// keepaliveLoop sends periodic keepalives
func (a *App) keepaliveLoop() {
	defer a.wg.Done()

	ticker := time.NewTicker(KeepaliveIntervalMS * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-a.ctx.Done():
			return
		case <-ticker.C:
			if a.state >= StateConnected && a.snd != nil {
				seq := atomic.AddUint32(&a.keepaliveSeq, 1)
				if err := a.snd.SendKeepalive(a.sessionID, seq, a.lastRxTsMs); err != nil {
					log.Printf("Keepalive error: %v", err)
				}
			}
		}
	}
}
