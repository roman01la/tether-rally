/**
 * ESP32 RC Car Controller - UDP Version (WebRTC DataChannel)
 *
 * This version receives control commands via UDP from the Raspberry Pi,
 * which acts as a WebRTC DataChannel relay.
 *
 * Protocol:
 * - Receives: seq(2) + cmd(1) + payload
 * - CMD_CTRL (0x01): thr(2) + str(2)
 * - Broadcasts beacon every second for Pi discovery
 */

#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_wifi.h>
#include <math.h>

// Local configuration (copy config.h.example to config.h)
#include "config.h"

// ----- Wi-Fi -----
const char *ssid = WIFI_SSID;
const char *password = WIFI_PASSWORD;

// ----- UDP -----
WiFiUDP udp;
const uint16_t UDP_PORT = 4210;    // Listen for commands
const uint16_t BEACON_PORT = 4211; // Broadcast beacon
const uint32_t BEACON_INTERVAL_MS = 1000;

// DAC pins
static const int PIN_THR_DAC = 25;
static const int PIN_STR_DAC = 26;

// Voltage calibration
static const float THR_V_BACK = 1.20f;
static const float THR_V_NEU = 1.69f;
static const float THR_V_FWD = 2.82f;

static const float STR_V_RIGHT = 0.22f;
static const float STR_V_CTR = 1.66f;
static const float STR_V_LEFT = 3.05f;

static const float VREF = 3.30f;

// Safety limits (enforced on ESP32, not browser)
static const float THR_FWD_LIMIT = 0.50f;  // Max forward throttle (50%)
static const float THR_BACK_LIMIT = 0.30f; // Max backward throttle (30%)
static const float STR_LIMIT = 1.0f;       // Max steering (100%)

// Behavior
static const uint32_t TIMEOUT_HOLD_MS = 80;     // Hold last value briefly
static const uint32_t TIMEOUT_NEUTRAL_MS = 250; // Then go neutral
static const float DEADBAND = 0.05f;
static const float SLEW_PER_SEC = 8.0f; // Max change per second
static const float EMA_ALPHA = 0.25f;   // EMA smoothing (0.15-0.35 typical at 200Hz)
static const uint32_t LOOP_DT_MS = 5;   // 200 Hz output loop

// Protocol commands
static const uint8_t CMD_PING = 0x00;
static const uint8_t CMD_CTRL = 0x01;
static const uint8_t CMD_PONG = 0x02;

// State (shared between tasks - marked volatile)
volatile float target_thr = 0.0f;
volatile float target_str = 0.0f;
volatile uint32_t last_cmd_ms = 0;
volatile bool firstPacket = true;
volatile uint16_t lastSeq = 0;

// Local to control loop (not shared)
float filtered_thr = 0.0f; // EMA filtered target
float filtered_str = 0.0f;
float out_thr = 0.0f; // Slew-limited output
float out_str = 0.0f;
uint32_t last_loop_ms = 0;
uint32_t last_beacon_ms = 0;
bool in_hold_state = false; // For staged timeout

// FreeRTOS task handle
TaskHandle_t udpTaskHandle = NULL;

// Track sender for PONG responses
IPAddress lastSenderIP;
uint16_t lastSenderPort = 0;

// ----- Helper functions -----

static inline float clampf(float x, float lo, float hi)
{
  if (x < lo)
    return lo;
  if (x > hi)
    return hi;
  return x;
}

static inline int volts_to_dac(float v)
{
  v = clampf(v, 0.0f, VREF);
  int code = (int)lroundf((v / VREF) * 255.0f);
  if (code < 0)
    code = 0;
  if (code > 255)
    code = 255;
  return code;
}

static float thr_norm_to_volts(float n)
{
  n = clampf(n, -1.0f, 1.0f);
  if (fabsf(n) < DEADBAND)
    return THR_V_NEU;
  if (n >= 0.0f)
    return THR_V_NEU + n * (THR_V_FWD - THR_V_NEU);
  return THR_V_NEU + n * (THR_V_NEU - THR_V_BACK);
}

static float str_norm_to_volts(float n)
{
  n = clampf(n, -1.0f, 1.0f);
  if (fabsf(n) < DEADBAND)
    return STR_V_CTR;
  if (n >= 0.0f)
    return STR_V_CTR + n * (STR_V_LEFT - STR_V_CTR);
  return STR_V_CTR + n * (STR_V_CTR - STR_V_RIGHT);
}

static void write_outputs(float thr_n, float str_n)
{
  dacWrite(PIN_THR_DAC, volts_to_dac(thr_norm_to_volts(thr_n)));
  dacWrite(PIN_STR_DAC, volts_to_dac(str_norm_to_volts(str_n)));
}

static void force_neutral()
{
  target_thr = 0.0f;
  target_str = 0.0f;
  filtered_thr = 0.0f;
  filtered_str = 0.0f;
  out_thr = 0.0f;
  out_str = 0.0f;
  in_hold_state = false;
  write_outputs(0.0f, 0.0f);
}

// ----- Packet handling -----

void handlePacket(uint8_t *data, size_t len)
{
  // Minimum packet: seq(2) + cmd(1) = 3 bytes
  if (len < 3)
    return;

  // Parse sequence number (little-endian)
  uint16_t seq = data[0] | (data[1] << 8);
  uint8_t cmd = data[2];

  // Drop old packets (with wraparound handling)
  // Accept if: seq > lastSeq, OR wraparound (lastSeq > 60000 && seq < 5000)
  if (!firstPacket)
  {
    bool isNewer = (seq > lastSeq) || (lastSeq > 60000 && seq < 5000);
    if (!isNewer)
    {
      return; // Drop old/duplicate packet
    }
  }
  firstPacket = false;
  lastSeq = seq;

  if (cmd == CMD_CTRL && len >= 7)
  {
    // Parse throttle and steering (little-endian int16)
    int16_t thr_raw = (int16_t)(data[3] | (data[4] << 8));
    int16_t str_raw = (int16_t)(data[5] | (data[6] << 8));

    // Normalize to -1.0 to 1.0
    float thr_norm = clampf((float)thr_raw / 32767.0f, -1.0f, 1.0f);
    float str_norm = clampf((float)str_raw / 32767.0f, -1.0f, 1.0f);

    // Apply safety limits (ESP32 is the authority, not browser)
    // Asymmetric throttle: forward and backward have different limits
    if (thr_norm >= 0.0f)
      target_thr = clampf(thr_norm, 0.0f, THR_FWD_LIMIT);
    else
      target_thr = clampf(thr_norm, -THR_BACK_LIMIT, 0.0f);
    target_str = clampf(str_norm, -STR_LIMIT, STR_LIMIT);
    last_cmd_ms = millis();
  }
  else if (cmd == CMD_PING && len >= 7)
  {
    // Echo back as PONG to sender
    // PONG format: cmd(1) + timestamp(4)
    uint8_t pong[5];
    pong[0] = CMD_PONG;
    memcpy(&pong[1], &data[3], 4); // Copy timestamp

    udp.beginPacket(lastSenderIP, lastSenderPort);
    udp.write(pong, 5);
    udp.endPacket();

    last_cmd_ms = millis();
  }
}

// ----- Beacon -----

void sendBeacon()
{
  // Broadcast "ARRMA" on beacon port for Pi discovery
  udp.beginPacket(IPAddress(255, 255, 255, 255), BEACON_PORT);
  udp.write((uint8_t *)"ARRMA", 5);
  udp.endPacket();
}

// ----- UDP Receive Task (runs on Core 0) -----

void udpReceiveTask(void *parameter)
{
  Serial.println("UDP task started on core " + String(xPortGetCoreID()));

  while (true)
  {
    // Drain all pending UDP packets
    int packetSize;
    while ((packetSize = udp.parsePacket()) > 0)
    {
      // Store sender info for PONG responses
      lastSenderIP = udp.remoteIP();
      lastSenderPort = udp.remotePort();

      uint8_t buf[32];
      int len = udp.read(buf, sizeof(buf));
      if (len > 0)
      {
        handlePacket(buf, len);
      }
    }

    // Small yield to prevent watchdog and allow other tasks
    vTaskDelay(1); // 1 tick = 1ms, allows ~1000 checks/sec
  }
}

// ----- Setup -----

void setup()
{
  Serial.begin(115200);
  Serial.println("\n\nARRMA RC Controller (UDP/WebRTC DataChannel)");

  // Set DAC pins to neutral
  force_neutral();

  // Connect to WiFi
  Serial.print("Connecting to WiFi: ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30)
  {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED)
  {
    Serial.println("\nWiFi connected!");
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());

    // Disable WiFi power saving for lower latency
    // Disable WiFi power saving for lower latency
    WiFi.setSleep(false);
    esp_wifi_set_ps(WIFI_PS_NONE); // Extra: disable modem sleep

    // Start UDP listener
    udp.begin(UDP_PORT);
    Serial.print("UDP listening on port ");
    Serial.println(UDP_PORT);

    // Create UDP receive task on Core 0 (WiFi core)
    // Control loop stays on Core 1 (app core) in loop()
    xTaskCreatePinnedToCore(
        udpReceiveTask, // Task function
        "UDP_RX",       // Task name
        4096,           // Stack size
        NULL,           // Parameters
        2,              // Priority (higher than loop)
        &udpTaskHandle, // Task handle
        0               // Core 0 (WiFi core)
    );
    Serial.println("UDP task created on Core 0");

    // Send initial beacon
    sendBeacon();
    last_beacon_ms = millis();
  }
  else
  {
    Serial.println("\nWiFi connection failed!");
  }
}

// ----- Main loop -----

void loop()
{
  uint32_t now = millis();

  // Check WiFi and reconnect if needed
  if (WiFi.status() != WL_CONNECTED)
  {
    Serial.println("WiFi lost, reconnecting...");
    force_neutral();
    WiFi.reconnect();
    delay(1000);
    return;
  }

  // UDP receive is now handled by udpReceiveTask on Core 0

  // Staged failsafe timeout
  uint32_t time_since_cmd = now - last_cmd_ms;
  if (last_cmd_ms > 0)
  {
    if (time_since_cmd > TIMEOUT_NEUTRAL_MS)
    {
      // Stage 2: Go neutral after 250ms
      target_thr = 0.0f;
      target_str = 0.0f;
      firstPacket = true; // Reset seq tracking
      in_hold_state = false;
    }
    else if (time_since_cmd > TIMEOUT_HOLD_MS)
    {
      // Stage 1: Hold last value (do nothing, keep current targets)
      in_hold_state = true;
    }
    else
    {
      in_hold_state = false;
    }
  }

  // Send beacon periodically for Pi discovery
  if (now - last_beacon_ms > BEACON_INTERVAL_MS)
  {
    last_beacon_ms = now;
    sendBeacon();
  }

  // Control loop at 200 Hz with EMA + slew rate limiting
  if (now - last_loop_ms >= LOOP_DT_MS)
  {
    float dt = (now - last_loop_ms) / 1000.0f;
    last_loop_ms = now;

    // Step 1: EMA filter on targets (smooths network jitter)
    filtered_thr += EMA_ALPHA * (target_thr - filtered_thr);
    filtered_str += EMA_ALPHA * (target_str - filtered_str);

    // Step 2: Slew rate limit (prevents sudden jumps)
    float max_delta = SLEW_PER_SEC * dt;

    float thr_diff = filtered_thr - out_thr;
    if (fabsf(thr_diff) <= max_delta)
      out_thr = filtered_thr;
    else
      out_thr += (thr_diff > 0 ? max_delta : -max_delta);

    float str_diff = filtered_str - out_str;
    if (fabsf(str_diff) <= max_delta)
      out_str = filtered_str;
    else
      out_str += (str_diff > 0 ? max_delta : -max_delta);

    write_outputs(out_thr, out_str);
  }
}
