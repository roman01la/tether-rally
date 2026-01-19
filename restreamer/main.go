package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/pion/webrtc/v4"
)

var (
	ffmpegCmd   *exec.Cmd
	mediamtxCmd *exec.Cmd
	mu          sync.Mutex

	camWhepURL        = getEnv("CAM_WHEP_URL", "https://cam.example.com/cam/whep")
	telemetryOfferURL = getEnv("TELEMETRY_OFFER_URL", "") // e.g., https://control.example.com/telemetry/offer
	tokenSecret       = getEnv("TOKEN_SECRET", "")        // Shared secret for generating access tokens
	turnUsername      = getEnv("TURN_USERNAME", "")       // Cloudflare TURN username
	turnCredential    = getEnv("TURN_CREDENTIAL", "")     // Cloudflare TURN password
	youtubeRTMPURL    = getEnv("YOUTUBE_RTMP_URL", "rtmp://a.rtmp.youtube.com/live2")
	youtubeStreamKey  = os.Getenv("YOUTUBE_STREAM_KEY")
	controlSecret     = getEnv("CONTROL_SECRET", "changeme")

	// Telemetry state
	telemetryMu      sync.RWMutex
	currentTelemetry Telemetry
	telemetryPC      *webrtc.PeerConnection
)

// Telemetry data received from Pi
type Telemetry struct {
	RaceTimeMs int32
	Throttle   int16
	Steering   int16
	LastUpdate time.Time
}

// CMD_TELEM = 0x07, format: seq(2) + cmd(1) + race_time(4) + throttle(2) + steering(2)
const CMD_TELEM = 0x07

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// generateToken creates an HMAC-SHA256 signed token (same format as generate-token.js)
func generateToken(durationMinutes int) string {
	if tokenSecret == "" {
		return ""
	}

	expiryTime := time.Now().Unix() + int64(durationMinutes*60)
	expiryHex := fmt.Sprintf("%08x", expiryTime)

	h := hmac.New(sha256.New, []byte(tokenSecret))
	h.Write([]byte(expiryHex))
	signature := hex.EncodeToString(h.Sum(nil))[:16] // First 16 chars

	return expiryHex + signature
}

// ----- Telemetry Client -----

func updateTelemetryFile() error {
	telemetryMu.RLock()
	t := currentTelemetry
	telemetryMu.RUnlock()

	// Format time as MM:SS.mmm
	totalMs := t.RaceTimeMs
	mins := totalMs / 60000
	secs := (totalMs % 60000) / 1000
	ms := totalMs % 1000

	// Convert throttle/steering to percentage
	thrPct := int(float64(t.Throttle) / 32767 * 100)
	strPct := int(float64(t.Steering) / 32767 * 100)

	// FFmpeg drawtext textfile requires escaping: % -> %%, : -> \:
	// Using 'pct' suffix instead of % symbol to avoid escaping issues
	content := fmt.Sprintf("TIME %02d:%02d.%03d  THR %d  STR %d",
		mins, secs, ms, thrPct, strPct)

	// Atomic write: write to temp file, then rename
	tmpFile := "/tmp/telemetry.txt.tmp"
	if err := os.WriteFile(tmpFile, []byte(content), 0644); err != nil {
		return err
	}
	return os.Rename(tmpFile, "/tmp/telemetry.txt")
}

func parseTelemetryMessage(data []byte) {
	// Format: seq(2) + cmd(1) + race_time(4) + throttle(2) + steering(2) = 11 bytes
	if len(data) < 11 {
		return
	}

	cmd := data[2]
	if cmd != CMD_TELEM {
		return
	}

	reader := bytes.NewReader(data[3:])
	var raceTime uint32
	var throttle, steering int16

	binary.Read(reader, binary.LittleEndian, &raceTime)
	binary.Read(reader, binary.LittleEndian, &throttle)
	binary.Read(reader, binary.LittleEndian, &steering)

	telemetryMu.Lock()
	currentTelemetry = Telemetry{
		RaceTimeMs: int32(raceTime),
		Throttle:   throttle,
		Steering:   steering,
		LastUpdate: time.Now(),
	}
	telemetryMu.Unlock()

	// Update telemetry file for FFmpeg
	if err := updateTelemetryFile(); err != nil {
		log.Printf("Error updating telemetry file: %v", err)
	}
}

func startTelemetryClient() error {
	if telemetryOfferURL == "" {
		log.Println("TELEMETRY_OFFER_URL not set, telemetry overlay disabled")
		return nil
	}

	log.Printf("Starting telemetry client, connecting to %s", telemetryOfferURL)

	// Create peer connection with TURN servers for NAT traversal
	iceServers := []webrtc.ICEServer{
		{URLs: []string{"stun:stun.cloudflare.com:3478"}},
	}

	// Add Cloudflare TURN if credentials available
	if turnUsername != "" && turnCredential != "" {
		iceServers = append(iceServers, webrtc.ICEServer{
			URLs:       []string{"turn:turn.cloudflare.com:3478?transport=udp"},
			Username:   turnUsername,
			Credential: turnCredential,
		})
		iceServers = append(iceServers, webrtc.ICEServer{
			URLs:       []string{"turn:turn.cloudflare.com:3478?transport=tcp"},
			Username:   turnUsername,
			Credential: turnCredential,
		})
		log.Println("Using Cloudflare TURN for telemetry connection")
	} else {
		log.Println("WARNING: No TURN credentials, telemetry may fail behind NAT")
	}

	config := webrtc.Configuration{
		ICEServers: iceServers,
	}

	pc, err := webrtc.NewPeerConnection(config)
	if err != nil {
		return fmt.Errorf("failed to create peer connection: %v", err)
	}
	telemetryPC = pc

	// Create data channel for telemetry
	dc, err := pc.CreateDataChannel("control", &webrtc.DataChannelInit{
		Ordered:        boolPtr(false),
		MaxRetransmits: uint16Ptr(0),
	})
	if err != nil {
		return fmt.Errorf("failed to create data channel: %v", err)
	}

	dc.OnOpen(func() {
		log.Println("Telemetry data channel opened")
	})

	dc.OnMessage(func(msg webrtc.DataChannelMessage) {
		parseTelemetryMessage(msg.Data)
	})

	dc.OnClose(func() {
		log.Println("Telemetry data channel closed")
	})

	pc.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		log.Printf("Telemetry connection state: %s", state)
		if state == webrtc.PeerConnectionStateFailed || state == webrtc.PeerConnectionStateClosed {
			// Reconnect after delay
			go func() {
				time.Sleep(5 * time.Second)
				if telemetryPC != nil {
					telemetryPC.Close()
					telemetryPC = nil
				}
				startTelemetryClient()
			}()
		}
	})

	// Create offer
	offer, err := pc.CreateOffer(nil)
	if err != nil {
		return fmt.Errorf("failed to create offer: %v", err)
	}

	// Set local description
	if err := pc.SetLocalDescription(offer); err != nil {
		return fmt.Errorf("failed to set local description: %v", err)
	}

	// Wait for ICE gathering
	gatherComplete := webrtc.GatheringCompletePromise(pc)
	<-gatherComplete

	// Send offer to Pi's control endpoint with generated token
	offerURL := telemetryOfferURL
	if tokenSecret != "" {
		token := generateToken(60) // 60 minute token
		offerURL += "?token=" + url.QueryEscape(token)
		log.Printf("Generated access token for control endpoint")
	}

	resp, err := http.Post(offerURL, "application/sdp", strings.NewReader(pc.LocalDescription().SDP))
	if err != nil {
		return fmt.Errorf("failed to send offer: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("offer rejected: %s - %s", resp.Status, string(body))
	}

	// Read answer
	answerSDP, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("failed to read answer: %v", err)
	}

	answer := webrtc.SessionDescription{
		Type: webrtc.SDPTypeAnswer,
		SDP:  string(answerSDP),
	}

	if err := pc.SetRemoteDescription(answer); err != nil {
		return fmt.Errorf("failed to set remote description: %v", err)
	}

	log.Println("Telemetry client connected")
	return nil
}

func boolPtr(b bool) *bool {
	return &b
}

func uint16Ptr(v uint16) *uint16 {
	return &v
}

// ----- MediaMTX & FFmpeg -----

func writeMediaMTXConfig() error {
	// Strip protocol from URL - MediaMTX uses whep:// (HTTP) or wheps:// (HTTPS)
	whepURL := camWhepURL
	scheme := "whep"
	if strings.HasPrefix(whepURL, "https://") {
		scheme = "wheps"
		whepURL = strings.TrimPrefix(whepURL, "https://")
	} else {
		whepURL = strings.TrimPrefix(whepURL, "http://")
	}

	config := fmt.Sprintf(`logLevel: info
logDestinations: [stdout]

api: yes
apiAddress: 127.0.0.1:9997

rtsp: yes
rtspAddress: :8554

webrtc: yes
webrtcAddress: :8889

paths:
  car:
    source: %s://%s
    sourceOnDemand: no
`, scheme, whepURL)

	return os.WriteFile("/tmp/mediamtx.yml", []byte(config), 0644)
}

func startMediaMTX() error {
	mu.Lock()
	defer mu.Unlock()

	if mediamtxCmd != nil && mediamtxCmd.Process != nil {
		// Already running
		if err := mediamtxCmd.Process.Signal(syscall.Signal(0)); err == nil {
			return nil
		}
	}

	if err := writeMediaMTXConfig(); err != nil {
		return err
	}

	mediamtxCmd = exec.Command("mediamtx", "/tmp/mediamtx.yml")
	mediamtxCmd.Stdout = os.Stdout
	mediamtxCmd.Stderr = os.Stderr

	if err := mediamtxCmd.Start(); err != nil {
		return err
	}

	log.Printf("MediaMTX started (PID: %d)", mediamtxCmd.Process.Pid)
	return nil
}

// waitForStream polls MediaMTX API until the stream has tracks ready
func waitForStream(path string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		resp, err := http.Get(fmt.Sprintf("http://127.0.0.1:9997/v3/paths/get/%s", path))
		if err == nil {
			if resp.StatusCode == 200 {
				var result map[string]interface{}
				if json.NewDecoder(resp.Body).Decode(&result) == nil {
					// Check if stream is ready (has tracks)
					if ready, ok := result["ready"].(bool); ok && ready {
						log.Printf("Stream '%s' is ready", path)
						resp.Body.Close()
						return nil
					}
					log.Printf("Waiting for stream '%s'... (ready=%v)", path, result["ready"])
				}
			}
			resp.Body.Close()
		}
		time.Sleep(2 * time.Second)
	}
	return fmt.Errorf("timeout waiting for stream '%s'", path)
}

func startFFmpeg() error {
	mu.Lock()
	defer mu.Unlock()

	if youtubeStreamKey == "" {
		return fmt.Errorf("YOUTUBE_STREAM_KEY not set")
	}

	if ffmpegCmd != nil && ffmpegCmd.Process != nil {
		if err := ffmpegCmd.Process.Signal(syscall.Signal(0)); err == nil {
			return fmt.Errorf("already streaming")
		}
	}

	// Start MediaMTX first (unlocked call)
	mu.Unlock()
	if err := startMediaMTX(); err != nil {
		mu.Lock()
		return err
	}

	// Wait for stream to be ready (up to 30 seconds)
	if err := waitForStream("car", 30*time.Second); err != nil {
		mu.Lock()
		return err
	}
	mu.Lock()

	rtmpURL := fmt.Sprintf("%s/%s", youtubeRTMPURL, youtubeStreamKey)

	// Build FFmpeg command with optional telemetry overlay
	args := []string{
		"-hide_banner", "-loglevel", "info",
		"-rtsp_transport", "tcp",
		"-i", "rtsp://127.0.0.1:8554/car",
		"-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100", // silent audio
	}

	// Add telemetry overlay if control URL is configured
	if telemetryOfferURL != "" {
		// Use drawtext filter with reload=1 to read telemetry.txt
		args = append(args,
			"-vf", "drawtext=fontfile=/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf:"+
				"textfile=/tmp/telemetry.txt:reload=1:"+
				"x=20:y=h-50:fontsize=24:fontcolor=white:"+
				"box=1:boxcolor=black@0.6:boxborderw=8",
		)
	}

	args = append(args,
		"-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
		"-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k",
		"-pix_fmt", "yuv420p",
		"-g", "60", "-keyint_min", "60",
		"-c:a", "aac", "-b:a", "128k",
		"-shortest",
		"-f", "flv", rtmpURL,
	)

	// YouTube requires audio, so we generate silent audio for video-only streams
	ffmpegCmd = exec.Command("ffmpeg", args...)
	ffmpegCmd.Stdout = os.Stdout
	ffmpegCmd.Stderr = os.Stderr

	if err := ffmpegCmd.Start(); err != nil {
		return err
	}

	log.Printf("FFmpeg started (PID: %d)", ffmpegCmd.Process.Pid)
	return nil
}

func stopFFmpeg() {
	mu.Lock()
	defer mu.Unlock()

	if ffmpegCmd != nil && ffmpegCmd.Process != nil {
		ffmpegCmd.Process.Kill()
		ffmpegCmd.Wait()
		ffmpegCmd = nil
		log.Println("FFmpeg stopped")
	}
}

func isStreaming() bool {
	mu.Lock()
	defer mu.Unlock()

	if ffmpegCmd == nil || ffmpegCmd.Process == nil {
		return false
	}
	return ffmpegCmd.Process.Signal(syscall.Signal(0)) == nil
}

func checkAuth(r *http.Request) bool {
	auth := r.Header.Get("Authorization")
	return auth == "Bearer "+controlSecret
}

func corsMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type")

		if r.Method == "OPTIONS" {
			w.WriteHeader(http.StatusOK)
			return
		}

		next(w, r)
	}
}

func jsonResponse(w http.ResponseWriter, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(data)
}

func main() {
	log.Printf("Starting restreamer...")
	log.Printf("CAM_WHEP_URL: %s", camWhepURL)

	// Initialize telemetry file with default values
	os.WriteFile("/tmp/telemetry.txt", []byte("TIME 00:00.000  THR 0%  STR 0%"), 0644)

	// Start telemetry client if configured
	if telemetryOfferURL != "" {
		log.Printf("TELEMETRY_OFFER_URL: %s", telemetryOfferURL)
		go func() {
			// Retry loop for telemetry client
			for {
				if err := startTelemetryClient(); err != nil {
					log.Printf("Telemetry client error: %v, retrying in 5s...", err)
					time.Sleep(5 * time.Second)
				} else {
					break
				}
			}
		}()
	}

	http.HandleFunc("/health", corsMiddleware(func(w http.ResponseWriter, r *http.Request) {
		jsonResponse(w, map[string]string{"status": "ok"})
	}))

	http.HandleFunc("/status", corsMiddleware(func(w http.ResponseWriter, r *http.Request) {
		telemetryMu.RLock()
		telemetryAge := time.Since(currentTelemetry.LastUpdate).Seconds()
		telemetryMu.RUnlock()

		jsonResponse(w, map[string]interface{}{
			"streaming":       isStreaming(),
			"telemetry_age_s": telemetryAge,
		})
	}))

	http.HandleFunc("/start", corsMiddleware(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if !checkAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}

		if err := startFFmpeg(); err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			jsonResponse(w, map[string]string{"error": err.Error()})
			return
		}
		jsonResponse(w, map[string]bool{"started": true})
	}))

	http.HandleFunc("/stop", corsMiddleware(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if !checkAuth(r) {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}

		stopFFmpeg()
		jsonResponse(w, map[string]bool{"stopped": true})

		// Exit the process so Fly.io machine stops
		go func() {
			time.Sleep(500 * time.Millisecond) // Allow response to be sent
			log.Println("Shutting down after stop request...")
			if mediamtxCmd != nil && mediamtxCmd.Process != nil {
				mediamtxCmd.Process.Kill()
			}
			if telemetryPC != nil {
				telemetryPC.Close()
			}
			os.Exit(0)
		}()
	}))

	// Graceful shutdown
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigChan
		log.Println("Shutting down...")
		stopFFmpeg()
		if mediamtxCmd != nil && mediamtxCmd.Process != nil {
			mediamtxCmd.Process.Kill()
		}
		if telemetryPC != nil {
			telemetryPC.Close()
		}
		os.Exit(0)
	}()

	port := getEnv("PORT", "8080")
	log.Printf("Control server listening on :%s", port)

	if !strings.Contains(port, ":") {
		port = ":" + port
	}
	log.Fatal(http.ListenAndServe(port, nil))
}
