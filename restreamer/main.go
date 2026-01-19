package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"
)

var (
	ffmpegCmd   *exec.Cmd
	mediamtxCmd *exec.Cmd
	mu          sync.Mutex

	camWhepURL       = getEnv("CAM_WHEP_URL", "https://cam.example.com/cam/whep")
	youtubeRTMPURL   = getEnv("YOUTUBE_RTMP_URL", "rtmp://a.rtmp.youtube.com/live2")
	youtubeStreamKey = os.Getenv("YOUTUBE_STREAM_KEY")
	controlSecret    = getEnv("CONTROL_SECRET", "changeme")
)

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

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

	// YouTube requires audio, so we generate silent audio for video-only streams
	ffmpegCmd = exec.Command("ffmpeg",
		"-hide_banner", "-loglevel", "info",
		"-rtsp_transport", "tcp",
		"-i", "rtsp://127.0.0.1:8554/car",
		"-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100", // silent audio
		"-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
		"-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k",
		"-pix_fmt", "yuv420p",
		"-g", "60", "-keyint_min", "60",
		"-c:a", "aac", "-b:a", "128k",
		"-shortest",
		"-f", "flv", rtmpURL,
	)
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

	http.HandleFunc("/health", corsMiddleware(func(w http.ResponseWriter, r *http.Request) {
		jsonResponse(w, map[string]string{"status": "ok"})
	}))

	http.HandleFunc("/status", corsMiddleware(func(w http.ResponseWriter, r *http.Request) {
		jsonResponse(w, map[string]bool{"streaming": isStreaming()})
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
		os.Exit(0)
	}()

	port := getEnv("PORT", "8080")
	log.Printf("Control server listening on :%s", port)

	if !strings.Contains(port, ":") {
		port = ":" + port
	}
	log.Fatal(http.ListenAndServe(port, nil))
}
