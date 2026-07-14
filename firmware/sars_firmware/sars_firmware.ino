#include "esp_camera.h"
#include <WiFi.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <Adafruit_NeoPixel.h>
#include "esp_http_server.h"
#include "driver/gpio.h"
#include "driver/i2s_pdm.h"
#include <Preferences.h>
#include "credentials.h"

// ================================================================
//  KAMERA-PINS — OV2640 auf XIAO ESP32-S3 Sense
// ================================================================
#define PWDN_GPIO_NUM   -1
#define RESET_GPIO_NUM  -1
#define XCLK_GPIO_NUM    10
#define SIOD_GPIO_NUM    40
#define SIOC_GPIO_NUM    39
#define Y9_GPIO_NUM      48
#define Y8_GPIO_NUM      11
#define Y7_GPIO_NUM      12
#define Y6_GPIO_NUM      14
#define Y5_GPIO_NUM      16
#define Y4_GPIO_NUM      18
#define Y3_GPIO_NUM      17
#define Y2_GPIO_NUM      15
#define VSYNC_GPIO_NUM   38
#define HREF_GPIO_NUM    47
#define PCLK_GPIO_NUM    13

// ================================================================
//  KONFIGURATION
// ================================================================

// Pins
const int PIN_SDA        = 5;   // D4 (GPIO5) — OLED SDA
const int PIN_SCL        = 6;   // D5 (GPIO6) — OLED SCL
const int PIN_BUZZER     = 4;   // D3 (GPIO4) — active-HIGH
const int PIN_NEOPIXEL   = 44;  // D7 (GPIO44) — NeoPixel data
const int NUM_PIXELS     = 5;

// NeoPixel strip
Adafruit_NeoPixel strip(NUM_PIXELS, PIN_NEOPIXEL, NEO_GRB + NEO_KHZ800);

portMUX_TYPE g_btnMux = portMUX_INITIALIZER_UNLOCKED;

// State
enum PostureLevel { GOOD, WARNING, BAD, NO_PERSON };
enum BreakLedState { BRK_NONE, BRK_DUE, BRK_ON_BREAK, BRK_OVER };
volatile PostureLevel g_postureLevel = NO_PERSON;
volatile BreakLedState g_breakLedState = BRK_NONE;
volatile uint32_t g_lastStateMs = 0;
bool g_staConnected = false;

// Break info received from PC
char g_breakText[64] = "";
bool g_breakDue = false;

volatile bool g_btnCalPressed = false;
volatile bool g_btnSnoozePressed = false;

enum BuzzState { BUZZ_IDLE, BUZZ_ON, BUZZ_OFF };
BuzzState g_buzzState = BUZZ_IDLE;
uint32_t g_buzzTransitionMs = 0;
int g_buzzOnDuration = 0;
int g_buzzOffDuration = 0;
int g_buzzRepeatLeft = 0;

uint32_t g_lastBuzzMs = 0;
uint32_t g_badSinceMs = 0;
uint32_t g_warnSinceMs = 0;
bool g_wasBad = false;
bool g_wasWarning = false;

httpd_handle_t g_webSrv    = nullptr;
httpd_handle_t g_streamSrv = nullptr;

// Microphone — built-in PDM on XIAO ESP32-S3 Sense
#define MIC_CLK_PIN  GPIO_NUM_42
#define MIC_DATA_PIN GPIO_NUM_41
i2s_chan_handle_t g_micHandle = nullptr;
bool g_micInited = false;

// Clap detection state machine
int g_clapThreshold = 3000;      // runtime-adjustable, default overridden by NVS
#define CLAP_MIN_GAP_MS   80
#define CLAP_MAX_GAP_MS   500
#define CLAP_WINDOW_MS    2500
#define CLAP_SILENCE_MS   800
#define CLAP_MAX_DUR_MS   500     // max onset duration before force-release

int g_clapCount = 0;
uint32_t g_lastClapMs = 0;
uint32_t g_clapWindowStart = 0;
bool g_inClap = false;
uint32_t g_lastBuzzEndMs = 0;

// Audio level tracking for dashboard
volatile float g_smoothedRms = 0.0f;
volatile float g_noiseFloor = 500.0f;
// Raw diagnostics
volatile int g_rawRms = 0;
volatile int g_rawMin = 0;
volatile int g_rawMax = 0;
volatile int g_rawSamples = 0;
volatile int g_rawDcOffset = 0;
bool g_micAutoMode = false;
float g_micAutoMultiplier = 5.0f;
volatile int g_peakRms = 0;
uint32_t g_peakDecayMs = 0;
Preferences g_prefs;

// OLED — SSD1306 128x64 I2C
U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);
PostureLevel g_lastOledLevel = NO_PERSON;
char g_lastOledBreakText[64] = "";
uint32_t g_lastOledUpdateMs = 0;
bool g_oledForceUpdate = true;  // force first draw

// ================================================================
//  API AUTH CHECK
// ================================================================

static bool checkAuth(httpd_req_t *req) {
    if (strlen(API_KEY) == 0) return true;
    char buf[128] = {0};
    if (httpd_req_get_hdr_value_str(req, "X-API-Key", buf, sizeof(buf)) != ESP_OK) {
        httpd_resp_set_status(req, "401 Unauthorized");
        httpd_resp_sendstr(req, "Missing API key");
        return false;
    }
    if (strcmp(buf, API_KEY) != 0) {
        httpd_resp_set_status(req, "403 Forbidden");
        httpd_resp_sendstr(req, "Invalid API key");
        return false;
    }
    return true;
}

// ================================================================
//  NEOPIXEL FEEDBACK
// ================================================================

void updateNeoPixels() {
  uint32_t color;
  switch (g_postureLevel) {
    case GOOD:
      color = strip.Color(0, 40, 0);     // green
      g_wasBad = false;
      g_wasWarning = false;
      break;
    case WARNING:
      color = strip.Color(40, 25, 0);    // amber
      g_wasBad = false;
      if (!g_wasWarning) {
        g_warnSinceMs = millis();
        g_wasWarning = true;
      }
      break;
    case BAD:
      color = strip.Color(40, 0, 0);     // red
      g_wasWarning = false;
      if (!g_wasBad) {
        g_badSinceMs = millis();
        g_wasBad = true;
      }
      break;
    case NO_PERSON:
    default:
      color = strip.Color(2, 2, 2);      // dim white
      g_wasBad = false;
      g_wasWarning = false;
      break;
  }

  // Break LED overrides
  if (g_breakLedState == BRK_DUE) {
    // Blue blink: time for a break
    bool blinkOn = (millis() / 500) % 2 == 0;
    color = blinkOn ? strip.Color(0, 0, 40) : strip.Color(0, 0, 0);
  } else if (g_breakLedState == BRK_ON_BREAK) {
    // Solid white: on break
    color = strip.Color(30, 30, 30);
  } else if (g_breakLedState == BRK_OVER) {
    // White blink: break over, come back
    bool blinkOn = (millis() / 300) % 2 == 0;
    color = blinkOn ? strip.Color(40, 40, 40) : strip.Color(0, 0, 0);
  }

  for (int i = 0; i < NUM_PIXELS; i++) {
    strip.setPixelColor(i, color);
  }
  strip.show();
}

// ================================================================
//  BUZZER — non-blocking state machine
// ================================================================

// Ensure GPIO4 is configured as output for buzzer.
// Runs once at first call to guarantee pin configuration after setup.
void ensureBuzzerPin() {
  static bool inited = false;
  if (!inited) {
    pinMode(PIN_BUZZER, OUTPUT);
    digitalWrite(PIN_BUZZER, LOW);
    inited = true;
  }
}

// Start a non-blocking buzz sequence: repeats of onMs HIGH / offMs LOW
void startBuzz(int onMs, int offMs, int repeats) {
  ensureBuzzerPin();
  g_buzzOnDuration = onMs;
  g_buzzOffDuration = offMs;
  g_buzzRepeatLeft = repeats;
  g_buzzState = BUZZ_ON;
  g_buzzTransitionMs = millis();
  digitalWrite(PIN_BUZZER, HIGH);
}

// Tick the buzzer state machine — called from loop(), never blocks
void tickBuzzer() {
  if (g_buzzState == BUZZ_IDLE) return;
  uint32_t now = millis();

  if (g_buzzState == BUZZ_ON && (now - g_buzzTransitionMs >= (uint32_t)g_buzzOnDuration)) {
    digitalWrite(PIN_BUZZER, LOW);
    g_buzzRepeatLeft--;
    if (g_buzzRepeatLeft <= 0) {
      g_buzzState = BUZZ_IDLE;
      g_lastBuzzEndMs = now;
    } else {
      g_buzzState = BUZZ_OFF;
      g_buzzTransitionMs = now;
    }
  } else if (g_buzzState == BUZZ_OFF && (now - g_buzzTransitionMs >= (uint32_t)g_buzzOffDuration)) {
    g_buzzState = BUZZ_ON;
    g_buzzTransitionMs = now;
    digitalWrite(PIN_BUZZER, HIGH);
  }
}

// Legacy blocking buzz — used only during setup and clap-action feedback
// where blocking is acceptable (one-shot, not in main loop)
void doBuzz(int onMs) {
  ensureBuzzerPin();
  digitalWrite(PIN_BUZZER, HIGH);
  delay(onMs);
  digitalWrite(PIN_BUZZER, LOW);
}

void handleBuzzer() {
  // If a non-blocking buzz sequence is in progress, let it finish first
  if (g_buzzState != BUZZ_IDLE) return;

  uint32_t now = millis();

  // Don't buzz too soon after a clap action (prevents mic self-trigger)
  if (now - g_lastBuzzEndMs < CLAP_SILENCE_MS) return;

  // WARNING posture: slow single beep every 8 seconds (after 5s of sustained warning)
  if (g_wasWarning && !g_wasBad && (now - g_warnSinceMs > 5000)) {
    if (now - g_lastBuzzMs > 8000) {
      startBuzz(80, 0, 1);
      g_lastBuzzMs = now;
    }
  }

  // BAD posture: fast double beep every 3 seconds (after 5s of sustained bad)
  if (g_wasBad && (now - g_badSinceMs > 5000)) {
    if (now - g_lastBuzzMs > 3000) {
      startBuzz(80, 80, 2);
      g_lastBuzzMs = now;
    }
  }

  // Break reminder: two beeps every 10 seconds
  if (g_breakDue && (now - g_lastBuzzMs > 10000)) {
    startBuzz(200, 200, 2);
    g_lastBuzzMs = now;
  }
}

// ================================================================
//  OLED DISPLAY
// ================================================================

void updateOLED() {
  uint32_t now = millis();

  // Throttle: at most every 500ms
  if (!g_oledForceUpdate && (now - g_lastOledUpdateMs < 500)) return;

  // Check if anything changed
  char localBreakText[64];
  taskENTER_CRITICAL(&g_btnMux);
  memcpy(localBreakText, g_breakText, sizeof(g_breakText));
  taskEXIT_CRITICAL(&g_btnMux);

  PostureLevel currentLevel = g_postureLevel;

  if (!g_oledForceUpdate &&
      currentLevel == g_lastOledLevel &&
      strcmp(localBreakText, g_lastOledBreakText) == 0) {
    return;  // nothing changed, skip redraw
  }

  g_lastOledLevel = currentLevel;
  strncpy(g_lastOledBreakText, localBreakText, sizeof(g_lastOledBreakText) - 1);
  g_lastOledBreakText[sizeof(g_lastOledBreakText) - 1] = '\0';
  g_lastOledUpdateMs = now;
  g_oledForceUpdate = false;

  u8g2.clearBuffer();

  // Title bar
  u8g2.setFont(u8g2_font_6x10_tr);
  u8g2.drawStr(1, 9, "SARS Posture Monitor");
  u8g2.drawHLine(0, 12, 128);

  // Posture status
  u8g2.setFont(u8g2_font_7x14B_tr);
  switch (currentLevel) {
    case GOOD:
      u8g2.drawStr(4, 30, "> Good Posture");
      break;
    case WARNING:
      u8g2.drawStr(4, 30, "> Sit Back!");
      break;
    case BAD:
      u8g2.drawStr(4, 30, "> BAD POSTURE!");
      break;
    case NO_PERSON:
      u8g2.setFont(u8g2_font_6x10_tr);
      u8g2.drawStr(4, 28, "No person detected");
      break;
  }

  // Break info
  u8g2.setFont(u8g2_font_6x10_tr);
  if (localBreakText[0] != '\0') {
    u8g2.drawStr(4, 46, localBreakText);
  }

  // Connection status + mic threshold
  u8g2.setFont(u8g2_font_5x7_tr);
  if (g_staConnected) {
    char ipLine[32];
    snprintf(ipLine, sizeof(ipLine), "IP: %s", WiFi.localIP().toString().c_str());
    u8g2.drawStr(4, 56, ipLine);
  } else {
    u8g2.drawStr(4, 56, "AP: SARS-Kamera");
  }
  char micLine[32];
  snprintf(micLine, sizeof(micLine), "T:%d %s", g_clapThreshold, g_micAutoMode ? "Auto" : "Man");
  u8g2.drawStr(4, 64, micLine);

  u8g2.sendBuffer();
}

// ================================================================
//  CLAP DETECTION
// ================================================================

void processClaps() {
  if (!g_micInited) return;

  // Don't listen right after a buzzer beep (prevents self-trigger)
  if (millis() - g_lastBuzzEndMs < CLAP_SILENCE_MS) return;

  // Read a small chunk of mic data
  const int numSamples = 512;
  int16_t samples[numSamples];
  size_t got = 0;
  esp_err_t err = i2s_channel_read(g_micHandle, samples, sizeof(samples), &got, pdMS_TO_TICKS(20));
  if (err != ESP_OK || got == 0) return;

  int actualSamples = got / sizeof(int16_t);

  // Compute DC-offset-removed RMS
  int32_t sum = 0;
  for (int i = 0; i < actualSamples; i++) sum += samples[i];
  int16_t dcOffset = sum / actualSamples;

  int64_t rmsSum = 0;
  for (int i = 0; i < actualSamples; i++) {
    int32_t v = samples[i] - dcOffset;
    rmsSum += v * v;
  }
  int rms = (int)sqrt((double)rmsSum / actualSamples);

  // Store raw diagnostics
  g_rawRms = rms;
  g_rawSamples = actualSamples;
  g_rawDcOffset = dcOffset;
  int16_t sMin = samples[0], sMax = samples[0];
  for (int i = 1; i < actualSamples; i++) {
    if (samples[i] < sMin) sMin = samples[i];
    if (samples[i] > sMax) sMax = samples[i];
  }
  g_rawMin = sMin;
  g_rawMax = sMax;

  // Update smoothed RMS for dashboard (EMA, alpha=0.3 for faster response)
  g_smoothedRms = 0.3f * rms + 0.7f * g_smoothedRms;

  // Peak tracker: holds max for 300ms then decays
  if (rms > g_peakRms) {
    g_peakRms = rms;
    g_peakDecayMs = millis();
  } else if (millis() - g_peakDecayMs > 300) {
    g_peakRms = (int)(g_peakRms * 0.7f);
    if (g_peakRms < (int)g_smoothedRms) g_peakRms = (int)g_smoothedRms;
  }

  // Update noise floor (slow-rise / fast-fall)
  if (rms < g_noiseFloor) {
    g_noiseFloor = (float)rms;
  } else if (rms < g_noiseFloor * 1.5f) {
    g_noiseFloor = 0.005f * rms + 0.995f * g_noiseFloor;
  }

  // Auto mode: threshold = noise floor * multiplier (no artificial floor)
  if (g_micAutoMode) {
    int autoThresh = (int)(g_noiseFloor * g_micAutoMultiplier);
    if (autoThresh < 30) autoThresh = 30;
    if (autoThresh > 25000) autoThresh = 25000;
    g_clapThreshold = autoThresh;
  }

  uint32_t now = millis();
  static uint32_t clapOnsetMs = 0;

  // Detect clap onset — mark start time but DON'T count yet
  if (rms > g_clapThreshold && !g_inClap) {
    g_inClap = true;
    clapOnsetMs = now;
  }

  // Detect clap release — count if spike was short
  if (g_inClap && rms < (g_clapThreshold * 3 / 4)) {
    g_inClap = false;
    uint32_t duration = now - clapOnsetMs;

    if (duration < 400) {
      // Short spike = real clap. Now apply timing logic.
      if (g_clapCount == 0) {
        g_clapWindowStart = now;
        g_clapCount = 1;
        g_lastClapMs = now;
        Serial.printf("[CLAP] #1 (dur:%dms, RMS peak in window)\n", (int)duration);
      } else if (now - g_lastClapMs >= CLAP_MIN_GAP_MS && now - g_lastClapMs <= CLAP_MAX_GAP_MS) {
        g_clapCount++;
        g_lastClapMs = now;
        Serial.printf("[CLAP] #%d (dur:%dms, gap:%dms)\n", g_clapCount, (int)duration, (int)(now - g_lastClapMs));
      } else if (now - g_lastClapMs > CLAP_MAX_GAP_MS) {
        g_clapCount = 1;
        g_clapWindowStart = now;
        g_lastClapMs = now;
        Serial.printf("[CLAP] #1 reset (dur:%dms, prev gap too long)\n", (int)duration);
      }
    } else {
      // Long spike = sustained noise, ignore
      Serial.printf("[NOISE] Rejected: %dms above threshold (not a clap)\n", (int)duration);
    }
  }

  // If onset lasts >CLAP_MAX_DUR_MS without release, force-clear (sustained loud noise)
  if (g_inClap && (now - clapOnsetMs > CLAP_MAX_DUR_MS)) {
    g_inClap = false;
    Serial.printf("[NOISE] Forced release: >%dms sustained\n", CLAP_MAX_DUR_MS);
  }

  // Check if clap window expired — process the count
  // 3 claps = calibrate, 4+ claps = snooze (raised from 2/3 to reduce false triggers)
  if (g_clapCount > 0 && (now - g_lastClapMs > CLAP_MAX_GAP_MS + 100)) {
    if (g_clapCount == 3) {
      Serial.println("[CLAP] Triple clap -> CALIBRATE");
      taskENTER_CRITICAL(&g_btnMux);
      g_btnCalPressed = true;
      taskEXIT_CRITICAL(&g_btnMux);
      doBuzz(150); delay(150); doBuzz(150); delay(150); doBuzz(150);
      g_lastBuzzEndMs = millis();
    } else if (g_clapCount >= 4) {
      Serial.println("[CLAP] Quad clap -> SNOOZE");
      taskENTER_CRITICAL(&g_btnMux);
      g_btnSnoozePressed = true;
      taskEXIT_CRITICAL(&g_btnMux);
      doBuzz(120); delay(100); doBuzz(120); delay(100); doBuzz(120); delay(100); doBuzz(120);
      g_lastBuzzEndMs = millis();
    }
    g_clapCount = 0;
  }

  // Safety: reset if window is too long
  if (g_clapCount > 0 && (now - g_clapWindowStart > CLAP_WINDOW_MS)) {
    g_clapCount = 0;
  }
}

// ================================================================
//  MJPEG STREAM HANDLER
// ================================================================
#define STREAM_BOUNDARY "sars_stream"
#define STREAM_MAX_CAM_FAILURES 50
static const char* MIME_MJPEG = "multipart/x-mixed-replace;boundary=" STREAM_BOUNDARY;
static const char* PART_SEP   = "\r\n--" STREAM_BOUNDARY "\r\n";
static const char* PART_HDR   = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

esp_err_t streamHandler(httpd_req_t* req) {
  char hdrBuf[64];
  esp_err_t res = ESP_OK;

  res = httpd_resp_set_type(req, MIME_MJPEG);
  if (res != ESP_OK) return res;

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Cache-Control", "no-cache, no-store, must-revalidate");

  Serial.println("[Stream] Client connected");

  int camFailures = 0;
  while (true) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) {
      camFailures++;
      if (camFailures >= STREAM_MAX_CAM_FAILURES) {
        Serial.printf("[Stream] Camera failed %d consecutive times, closing stream\n", camFailures);
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Camera capture failed");
        return ESP_FAIL;
      }
      delay(10);
      continue;
    }
    camFailures = 0;  // reset on success

    size_t hlen = snprintf(hdrBuf, sizeof(hdrBuf), PART_HDR, fb->len);

    res = httpd_resp_send_chunk(req, PART_SEP, strlen(PART_SEP));
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, hdrBuf, hlen);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char*)fb->buf, fb->len);

    esp_camera_fb_return(fb);

    if (res != ESP_OK) break;
  }

  Serial.println("[Stream] Client disconnected");
  return res;
}

// ================================================================
//  POST /state — receive posture + break state from PC
// ================================================================

esp_err_t stateHandler(httpd_req_t* req) {
  if (!checkAuth(req)) return ESP_OK;
  char buf[512];
  int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
  if (len <= 0) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty body");
    return ESP_FAIL;
  }
  buf[len] = '\0';

  // Parse posture level — search for "level":"xxx" specifically
  char* lvl = strstr(buf, "\"level\":\"");
  if (lvl) {
    lvl += 9; // skip past "level":"
    if (strncmp(lvl, "good", 4) == 0)         g_postureLevel = GOOD;
    else if (strncmp(lvl, "warning", 7) == 0)  g_postureLevel = WARNING;
    else if (strncmp(lvl, "bad", 3) == 0)      g_postureLevel = BAD;
    else                                        g_postureLevel = NO_PERSON;
  } else {
    g_postureLevel = NO_PERSON;
  }

  // Parse break_state ("none", "due", "on_break", "over")
  if (strstr(buf, "\"break_state\":\"due\""))         g_breakLedState = BRK_DUE;
  else if (strstr(buf, "\"break_state\":\"on_break\"")) g_breakLedState = BRK_ON_BREAK;
  else if (strstr(buf, "\"break_state\":\"over\""))    g_breakLedState = BRK_OVER;
  else if (strstr(buf, "\"break_state\":\"none\""))    g_breakLedState = BRK_NONE;

  g_breakDue = (g_breakLedState == BRK_DUE);

  // Parse break_text
  char* bt = strstr(buf, "\"break_text\":\"");
  if (bt) {
    bt += 14;
    char* end = strchr(bt, '"');
    if (end && (end - bt) < (int)sizeof(g_breakText)) {
      taskENTER_CRITICAL(&g_btnMux);
      memcpy(g_breakText, bt, end - bt);
      g_breakText[end - bt] = '\0';
      taskEXIT_CRITICAL(&g_btnMux);
    }
  } else {
    taskENTER_CRITICAL(&g_btnMux);
    g_breakText[0] = '\0';
    taskEXIT_CRITICAL(&g_btnMux);
  }

  // Parse clap_threshold if present
  char* ct = strstr(buf, "\"clap_threshold\":");
  if (ct) {
    ct += 17;
    int newThresh = atoi(ct);
    if (newThresh >= 30 && newThresh <= 25000 && newThresh != g_clapThreshold) {
      g_clapThreshold = newThresh;
      g_micAutoMode = false;
      g_prefs.begin("sars", false);
      g_prefs.putInt("clapThresh", g_clapThreshold);
      g_prefs.putBool("micAuto", false);
      g_prefs.end();
      Serial.printf("[CONFIG] Clap threshold -> %d (saved to NVS)\n", g_clapThreshold);
    }
  }

  // Parse mic_auto mode if present
  char* ma = strstr(buf, "\"mic_auto\":");
  if (ma) {
    ma += 11;
    bool newAuto = (strncmp(ma, "true", 4) == 0);
    if (newAuto != g_micAutoMode) {
      g_micAutoMode = newAuto;
      g_prefs.begin("sars", false);
      g_prefs.putBool("micAuto", g_micAutoMode);
      g_prefs.end();
      Serial.printf("[CONFIG] Mic auto mode -> %s\n", g_micAutoMode ? "ON" : "OFF");
    }
  }

  // Parse mic_auto_multiplier if present
  char* mm = strstr(buf, "\"mic_auto_mult\":");
  if (mm) {
    mm += 16;
    float newMult = atof(mm);
    if (newMult >= 1.5f && newMult <= 10.0f && newMult != g_micAutoMultiplier) {
      g_micAutoMultiplier = newMult;
      g_prefs.begin("sars", false);
      g_prefs.putFloat("micAutoMul", g_micAutoMultiplier);
      g_prefs.end();
      Serial.printf("[CONFIG] Auto multiplier -> %.1f\n", g_micAutoMultiplier);
    }
  }

  g_lastStateMs = millis();

  bool calPressed, snoozePressed;
  taskENTER_CRITICAL(&g_btnMux);
  calPressed = g_btnCalPressed;
  snoozePressed = g_btnSnoozePressed;
  g_btnCalPressed = false;
  g_btnSnoozePressed = false;
  taskEXIT_CRITICAL(&g_btnMux);

  // Return button/clap states + audio data
  char resp[192];
  snprintf(resp, sizeof(resp),
    "{\"ok\":true,\"cal\":%s,\"snooze\":%s,\"rms\":%d,\"peak\":%d,\"thresh\":%d,\"floor\":%d,\"mic_auto\":%s}",
    calPressed ? "true" : "false",
    snoozePressed ? "true" : "false",
    (int)g_smoothedRms,
    g_peakRms,
    g_clapThreshold,
    (int)g_noiseFloor,
    g_micAutoMode ? "true" : "false"
  );

  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_send(req, resp, strlen(resp));
  return ESP_OK;
}

// ================================================================
//  MICROPHONE — PDM (ESP-IDF 5.x driver)
// ================================================================

bool initMic() {
  i2s_chan_config_t chanCfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  esp_err_t err = i2s_new_channel(&chanCfg, NULL, &g_micHandle);
  if (err != ESP_OK) {
    Serial.printf("[MIC] Channel create failed: 0x%X\n", err);
    return false;
  }

  i2s_pdm_rx_config_t pdmCfg = {
    .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(16000),
    .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
    .gpio_cfg = {
      .clk = MIC_CLK_PIN,
      .din = MIC_DATA_PIN,
      .invert_flags = { .clk_inv = false },
    },
  };

  err = i2s_channel_init_pdm_rx_mode(g_micHandle, &pdmCfg);
  if (err != ESP_OK) {
    Serial.printf("[MIC] PDM RX init failed: 0x%X\n", err);
    i2s_del_channel(g_micHandle);
    g_micHandle = nullptr;
    return false;
  }

  err = i2s_channel_enable(g_micHandle);
  if (err != ESP_OK) {
    Serial.printf("[MIC] Channel enable failed: 0x%X\n", err);
    i2s_del_channel(g_micHandle);
    g_micHandle = nullptr;
    return false;
  }

  g_micInited = true;
  Serial.println("[MIC] PDM RX initialized (16kHz, 16-bit, mono)");
  return true;
}

// ================================================================
//  GET /status
// ================================================================

esp_err_t statusHandler(httpd_req_t* req) {
  char json[512];
  snprintf(json, sizeof(json),
    "{\"posture\":\"%s\",\"uptime\":%lu,\"freeHeap\":%lu,\"wifi\":\"%s\",\"ip\":\"%s\",\"mic\":\"%s\""
    ",\"rms\":%d,\"peak\":%d,\"clapThreshold\":%d,\"noiseFloor\":%d,\"micAuto\":%s"
    ",\"rawRms\":%d,\"rawMin\":%d,\"rawMax\":%d,\"rawSamples\":%d,\"dcOffset\":%d,\"clapCount\":%d}",
    g_postureLevel == GOOD ? "good" : g_postureLevel == WARNING ? "warning" :
    g_postureLevel == BAD ? "bad" : "no_person",
    millis() / 1000UL,
    (unsigned long)ESP.getFreeHeap(),
    g_staConnected ? "sta" : "ap",
    g_staConnected ? WiFi.localIP().toString().c_str() : WiFi.softAPIP().toString().c_str(),
    g_micInited ? "ready" : "not initialized",
    (int)g_smoothedRms, g_peakRms, g_clapThreshold, (int)g_noiseFloor,
    g_micAutoMode ? "true" : "false",
    g_rawRms, g_rawMin, g_rawMax, g_rawSamples, g_rawDcOffset, g_clapCount
  );
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, json, strlen(json));
}

// ================================================================
//  POST /config — direct threshold control (browser calls this)
// ================================================================

esp_err_t configHandler(httpd_req_t* req) {
  if (!checkAuth(req)) return ESP_OK;
  char buf[256];
  int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
  if (len <= 0) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty body");
    return ESP_FAIL;
  }
  buf[len] = '\0';

  char* ct = strstr(buf, "\"threshold\":");
  if (ct) {
    ct += 12;
    int newThresh = atoi(ct);
    if (newThresh >= 30 && newThresh <= 25000) {
      g_clapThreshold = newThresh;
      g_micAutoMode = false;
      g_prefs.begin("sars", false);
      g_prefs.putInt("clapThresh", g_clapThreshold);
      g_prefs.putBool("micAuto", false);
      g_prefs.end();
      Serial.printf("[CONFIG] Threshold -> %d (direct)\n", g_clapThreshold);
    }
  }

  char* ma = strstr(buf, "\"auto\":");
  if (ma) {
    ma += 7;
    bool newAuto = (strncmp(ma, "true", 4) == 0);
    g_micAutoMode = newAuto;
    g_prefs.begin("sars", false);
    g_prefs.putBool("micAuto", g_micAutoMode);
    g_prefs.end();
    Serial.printf("[CONFIG] Auto -> %s (direct)\n", g_micAutoMode ? "ON" : "OFF");
  }

  char resp[96];
  snprintf(resp, sizeof(resp),
    "{\"ok\":true,\"thresh\":%d,\"auto\":%s}",
    g_clapThreshold, g_micAutoMode ? "true" : "false");
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_send(req, resp, strlen(resp));
  return ESP_OK;
}

// Handle CORS preflight for /config
esp_err_t configOptionsHandler(httpd_req_t* req) {
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "POST, OPTIONS");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type, X-API-Key");
  httpd_resp_send(req, "", 0);
  return ESP_OK;
}

// ================================================================
//  GET /buzzer — test beep
// ================================================================

esp_err_t buzzerHandler(httpd_req_t* req) {
  if (!checkAuth(req)) return ESP_OK;
  ensureBuzzerPin();

  // Single short test beep — 100ms, non-blocking for httpd thread
  digitalWrite(PIN_BUZZER, HIGH);
  delay(100);
  digitalWrite(PIN_BUZZER, LOW);
  g_lastBuzzEndMs = millis();
  Serial.println("[BUZZER] Test beep (single 100ms)");

  char resp[64];
  snprintf(resp, sizeof(resp), "{\"ok\":true,\"buzz\":true,\"pin\":%d}", digitalRead(PIN_BUZZER));
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_send(req, resp, strlen(resp));
  return ESP_OK;
}

// ================================================================
//  SERVERS
// ================================================================

void startServers() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 80;
  cfg.max_uri_handlers = 12;

  if (httpd_start(&g_webSrv, &cfg) == ESP_OK) {
    httpd_uri_t uStatus = { "/status", HTTP_GET, statusHandler, nullptr };
    httpd_uri_t uState  = { "/state",  HTTP_POST, stateHandler, nullptr };
    httpd_uri_t uConfig = { "/config", HTTP_POST, configHandler, nullptr };
    httpd_uri_t uConfigOpt = { "/config", HTTP_OPTIONS, configOptionsHandler, nullptr };
    httpd_uri_t uBuzzerGet = { "/buzzer", HTTP_GET, buzzerHandler, nullptr };
    httpd_uri_t uBuzzerPost = { "/buzzer", HTTP_POST, buzzerHandler, nullptr };
    httpd_register_uri_handler(g_webSrv, &uStatus);
    httpd_register_uri_handler(g_webSrv, &uState);
    httpd_register_uri_handler(g_webSrv, &uConfig);
    httpd_register_uri_handler(g_webSrv, &uConfigOpt);
    httpd_register_uri_handler(g_webSrv, &uBuzzerGet);
    httpd_register_uri_handler(g_webSrv, &uBuzzerPost);
    Serial.println("[Server] Web server started (port 80)");
  }

  cfg.server_port = 81;
  cfg.ctrl_port = 32769;
  if (httpd_start(&g_streamSrv, &cfg) == ESP_OK) {
    httpd_uri_t uStream = { "/stream", HTTP_GET, streamHandler, nullptr };
    httpd_register_uri_handler(g_streamSrv, &uStream);
    Serial.println("[Server] Stream server started (port 81)");
  }
}

// ================================================================
//  CAMERA INIT
// ================================================================

bool initCamera() {
  camera_config_t cc = {};
  cc.ledc_channel  = LEDC_CHANNEL_0;
  cc.ledc_timer    = LEDC_TIMER_0;
  cc.pin_d0        = Y2_GPIO_NUM;
  cc.pin_d1        = Y3_GPIO_NUM;
  cc.pin_d2        = Y4_GPIO_NUM;
  cc.pin_d3        = Y5_GPIO_NUM;
  cc.pin_d4        = Y6_GPIO_NUM;
  cc.pin_d5        = Y7_GPIO_NUM;
  cc.pin_d6        = Y8_GPIO_NUM;
  cc.pin_d7        = Y9_GPIO_NUM;
  cc.pin_xclk      = XCLK_GPIO_NUM;
  cc.pin_pclk      = PCLK_GPIO_NUM;
  cc.pin_vsync     = VSYNC_GPIO_NUM;
  cc.pin_href      = HREF_GPIO_NUM;
  cc.pin_sccb_sda  = SIOD_GPIO_NUM;
  cc.pin_sccb_scl  = SIOC_GPIO_NUM;
  cc.pin_pwdn      = PWDN_GPIO_NUM;
  cc.pin_reset     = RESET_GPIO_NUM;
  cc.xclk_freq_hz  = 20000000;
  cc.pixel_format  = PIXFORMAT_JPEG;
  cc.frame_size    = FRAMESIZE_QVGA;
  cc.grab_mode     = CAMERA_GRAB_LATEST;

  if (psramFound()) {
    cc.fb_location  = CAMERA_FB_IN_PSRAM;
    cc.fb_count     = 2;
    cc.jpeg_quality = 12;
  } else {
    cc.fb_location  = CAMERA_FB_IN_DRAM;
    cc.fb_count     = 1;
    cc.jpeg_quality = 15;
  }

  esp_err_t err = esp_camera_init(&cc);
  if (err != ESP_OK) {
    Serial.printf("[Camera] Init FAILED: 0x%x\n", err);
    return false;
  }

  sensor_t* s = esp_camera_sensor_get();
  if (s) {
    s->set_vflip(s, 1);
    s->set_hmirror(s, 1);
  }

  Serial.println("[Camera] Init OK");
  return true;
}

// ================================================================
//  WIFI
// ================================================================

bool connectWiFi() {
  Serial.printf("[WiFi] Connecting to '%s'...\n", WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    g_staConnected = true;
    Serial.printf("[WiFi] Connected! IP: %s\n", WiFi.localIP().toString().c_str());
    // Green flash on NeoPixels
    for (int i = 0; i < NUM_PIXELS; i++) strip.setPixelColor(i, strip.Color(0, 30, 0));
    strip.show();
    delay(500);
    strip.clear(); strip.show();
    return true;
  }

  Serial.printf("[WiFi] STA failed - starting AP '%s'\n", AP_SSID);
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS);
  delay(500);
  g_staConnected = false;
  Serial.printf("[WiFi] AP IP: %s\n", WiFi.softAPIP().toString().c_str());
  return false;
}

// ================================================================
//  SETUP + LOOP
// ================================================================

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=============================");
  Serial.println("  SARS - Posture Assistant");
  Serial.println("=============================");

  // D3 = GPIO4 — buzzer
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);

  // Init NeoPixel
  strip.begin();
  strip.setBrightness(40);
  strip.clear();
  strip.show();

  // Init OLED
  Wire.begin(PIN_SDA, PIN_SCL);
  u8g2.begin();
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_7x14B_tr);
  u8g2.drawStr(20, 35, "SARS v2.0");
  u8g2.setFont(u8g2_font_6x10_tr);
  u8g2.drawStr(25, 52, "Starting...");
  u8g2.sendBuffer();

  if (!initCamera()) {
    Serial.println("[FATAL] Camera init failed");
    u8g2.clearBuffer();
    u8g2.drawStr(4, 35, "CAMERA ERROR!");
    u8g2.sendBuffer();
    for (int i = 0; i < NUM_PIXELS; i++) strip.setPixelColor(i, strip.Color(40, 0, 0));
    strip.show();
    while (true) delay(100);
  }

  initMic();

  // Load saved mic settings from NVS
  g_prefs.begin("sars", true);
  g_clapThreshold = g_prefs.getInt("clapThresh", 3000);
  g_micAutoMode = g_prefs.getBool("micAuto", false);
  g_micAutoMultiplier = g_prefs.getFloat("micAutoMul", 3.0f);
  g_prefs.end();
  if (g_clapThreshold < 30 || g_clapThreshold > 25000) g_clapThreshold = 80;
  if (g_micAutoMultiplier < 1.5f || g_micAutoMultiplier > 10.0f) g_micAutoMultiplier = 3.0f;
  Serial.printf("[CONFIG] Clap threshold: %d, auto: %s, mult: %.1f\n",
    g_clapThreshold, g_micAutoMode ? "ON" : "OFF", g_micAutoMultiplier);

  connectWiFi();
  startServers();
  g_oledForceUpdate = true;
  updateOLED();

  Serial.println("[SARS] Ready.");

  // Startup beep — re-init GPIO4 in case camera/WiFi/server init reclaimed it
  ensureBuzzerPin();
  doBuzz(200);
  g_lastBuzzEndMs = millis();
  Serial.println("[SARS] Buzzer OK.");
}

void loop() {
  processClaps();
  handleBuzzer();
  tickBuzzer();
  updateNeoPixels();
  updateOLED();

  // Timeout: if no state received for 30s, clear
  if (g_lastStateMs > 0 && millis() - g_lastStateMs > 30000) {
    g_postureLevel = NO_PERSON;
    taskENTER_CRITICAL(&g_btnMux);
    g_breakText[0] = '\0';
    taskEXIT_CRITICAL(&g_btnMux);
    g_breakDue = false;
    g_breakLedState = BRK_NONE;
    g_lastStateMs = 0;
  }

  delay(50);
}
