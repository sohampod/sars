#include "esp_camera.h"
#include <WiFi.h>
#include <Wire.h>
#include <U8g2lib.h>
#include "esp_http_server.h"
#include "credentials.h"

// ================================================================
//  Camera pins — OV2640 on XIAO ESP32-S3 Sense
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
//  Pin assignments
// ================================================================
const int PIN_LED_RED    = 1;   // D0
const int PIN_LED_GREEN  = 2;   // D1
const int PIN_LED_YELLOW = 3;   // D2
const int PIN_BUZZER     = 4;   // D3
const int PIN_SDA        = 5;   // D4 — OLED
const int PIN_SCL        = 6;   // D5 — OLED
const int PIN_BTN_CAL    = 43;  // D6 — Calibrate button
const int PIN_BTN_SNOOZE = 44;  // D7 — Snooze/Break button
const int PIN_LED_BLUE   = 7;   // D8 — break due (blinks)
const int PIN_LED_WHITE  = 8;   // D9 — break over (blinks)

// ================================================================
//  State
// ================================================================
enum PostureLevel { GOOD, WARNING, BAD, NO_PERSON };
enum BreakLedState { BRK_NONE, BRK_DUE, BRK_OVER };
volatile PostureLevel g_postureLevel = NO_PERSON;
volatile BreakLedState g_breakLedState = BRK_NONE;
volatile uint32_t g_lastStateMs = 0;
bool g_staConnected = false;

char g_breakText[64] = "";
bool g_breakDue = false;

volatile bool g_btnCalPressed = false;
volatile bool g_btnSnoozePressed = false;
uint32_t g_lastBtnCal = 0;
uint32_t g_lastBtnSnooze = 0;

uint32_t g_lastBuzzMs = 0;
uint32_t g_badSinceMs = 0;
bool g_wasBad = false;

httpd_handle_t g_webSrv    = nullptr;
httpd_handle_t g_streamSrv = nullptr;

// OLED — SSD1306 128x64 I2C
U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);

// ================================================================
//  LED + Buzzer control
// ================================================================

void updateFeedback() {
  switch (g_postureLevel) {
    case GOOD:
      digitalWrite(PIN_LED_GREEN, HIGH);
      digitalWrite(PIN_LED_YELLOW, LOW);
      digitalWrite(PIN_LED_RED, LOW);
      g_wasBad = false;
      break;
    case WARNING:
      digitalWrite(PIN_LED_GREEN, LOW);
      digitalWrite(PIN_LED_YELLOW, HIGH);
      digitalWrite(PIN_LED_RED, LOW);
      g_wasBad = false;
      break;
    case BAD:
      digitalWrite(PIN_LED_GREEN, LOW);
      digitalWrite(PIN_LED_YELLOW, LOW);
      digitalWrite(PIN_LED_RED, HIGH);
      if (!g_wasBad) {
        g_badSinceMs = millis();
        g_wasBad = true;
      }
      break;
    case NO_PERSON:
    default:
      digitalWrite(PIN_LED_GREEN, LOW);
      digitalWrite(PIN_LED_YELLOW, LOW);
      digitalWrite(PIN_LED_RED, LOW);
      g_wasBad = false;
      break;
  }
}

void handleBuzzer() {
  uint32_t now = millis();

  if (g_wasBad && (now - g_badSinceMs > 10000)) {
    if (now - g_lastBuzzMs > 5000) {
      for (int i = 0; i < 3; i++) {
        digitalWrite(PIN_BUZZER, HIGH);
        delay(100);
        digitalWrite(PIN_BUZZER, LOW);
        delay(100);
      }
      g_lastBuzzMs = now;
    }
  }

  if (g_breakDue && (now - g_lastBuzzMs > 10000)) {
    for (int i = 0; i < 2; i++) {
      digitalWrite(PIN_BUZZER, HIGH);
      delay(200);
      digitalWrite(PIN_BUZZER, LOW);
      delay(200);
    }
    g_lastBuzzMs = now;
  }
}

void updateBreakLeds() {
  bool blinkOn = (millis() / 500) % 2 == 0;

  switch (g_breakLedState) {
    case BRK_DUE:
      digitalWrite(PIN_LED_BLUE, blinkOn ? HIGH : LOW);
      digitalWrite(PIN_LED_WHITE, LOW);
      break;
    case BRK_OVER:
      digitalWrite(PIN_LED_BLUE, LOW);
      digitalWrite(PIN_LED_WHITE, blinkOn ? HIGH : LOW);
      break;
    case BRK_NONE:
    default:
      digitalWrite(PIN_LED_BLUE, LOW);
      digitalWrite(PIN_LED_WHITE, LOW);
      break;
  }
}

// ================================================================
//  OLED display
// ================================================================

void updateOLED() {
  u8g2.clearBuffer();

  u8g2.setFont(u8g2_font_6x10_tr);
  u8g2.drawStr(1, 9, "SARS Posture Monitor");
  u8g2.drawHLine(0, 12, 128);

  u8g2.setFont(u8g2_font_7x14B_tr);
  switch (g_postureLevel) {
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

  u8g2.setFont(u8g2_font_6x10_tr);
  if (g_breakText[0] != '\0') {
    u8g2.drawStr(4, 46, g_breakText);
  }

  u8g2.setFont(u8g2_font_5x7_tr);
  if (g_staConnected) {
    char ipLine[32];
    snprintf(ipLine, sizeof(ipLine), "IP: %s", WiFi.localIP().toString().c_str());
    u8g2.drawStr(4, 62, ipLine);
  } else {
    u8g2.drawStr(4, 62, "AP: SARS-Kamera");
  }

  u8g2.sendBuffer();
}

// ================================================================
//  Button handling
// ================================================================

void checkButtons() {
  uint32_t now = millis();

  if (digitalRead(PIN_BTN_CAL) == LOW && (now - g_lastBtnCal > 500)) {
    g_btnCalPressed = true;
    g_lastBtnCal = now;
    Serial.println("[BTN] Calibrate pressed");
  }

  if (digitalRead(PIN_BTN_SNOOZE) == LOW && (now - g_lastBtnSnooze > 500)) {
    g_btnSnoozePressed = true;
    g_lastBtnSnooze = now;
    Serial.println("[BTN] Snooze pressed");
  }
}

// ================================================================
//  MJPEG stream handler
// ================================================================
#define STREAM_BOUNDARY "sars_stream"
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

  while (true) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) { delay(10); continue; }

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
  char buf[256];
  int len = httpd_req_recv(req, buf, sizeof(buf) - 1);
  if (len <= 0) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty body");
    return ESP_FAIL;
  }
  buf[len] = '\0';

  if (strstr(buf, "\"good\""))          g_postureLevel = GOOD;
  else if (strstr(buf, "\"warning\""))  g_postureLevel = WARNING;
  else if (strstr(buf, "\"bad\""))      g_postureLevel = BAD;
  else                                  g_postureLevel = NO_PERSON;

  if (strstr(buf, "\"break_state\":\"due\""))       g_breakLedState = BRK_DUE;
  else if (strstr(buf, "\"break_state\":\"over\""))  g_breakLedState = BRK_OVER;
  else if (strstr(buf, "\"break_state\":\"none\""))  g_breakLedState = BRK_NONE;

  g_breakDue = (strstr(buf, "\"break_due\":true") != nullptr);
  if (g_breakLedState == BRK_NONE && g_breakDue) g_breakLedState = BRK_DUE;

  char* bt = strstr(buf, "\"break_text\":\"");
  if (bt) {
    bt += 14;
    char* end = strchr(bt, '"');
    if (end && (end - bt) < (int)sizeof(g_breakText)) {
      memcpy(g_breakText, bt, end - bt);
      g_breakText[end - bt] = '\0';
    }
  } else {
    g_breakText[0] = '\0';
  }

  g_lastStateMs = millis();
  updateFeedback();

  char resp[64];
  snprintf(resp, sizeof(resp),
    "{\"ok\":true,\"cal\":%s,\"snooze\":%s}",
    g_btnCalPressed ? "true" : "false",
    g_btnSnoozePressed ? "true" : "false"
  );
  g_btnCalPressed = false;
  g_btnSnoozePressed = false;

  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_send(req, resp, strlen(resp));
  return ESP_OK;
}

// ================================================================
//  GET /status
// ================================================================

esp_err_t statusHandler(httpd_req_t* req) {
  char json[192];
  snprintf(json, sizeof(json),
    "{\"posture\":\"%s\",\"uptime\":%lu,\"freeHeap\":%lu,\"wifi\":\"%s\",\"ip\":\"%s\"}",
    g_postureLevel == GOOD ? "good" : g_postureLevel == WARNING ? "warning" :
    g_postureLevel == BAD ? "bad" : "no_person",
    millis() / 1000UL,
    (unsigned long)ESP.getFreeHeap(),
    g_staConnected ? "sta" : "ap",
    g_staConnected ? WiFi.localIP().toString().c_str() : WiFi.softAPIP().toString().c_str()
  );
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, json, strlen(json));
}

// ================================================================
//  Servers
// ================================================================

void startServers() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 80;

  if (httpd_start(&g_webSrv, &cfg) == ESP_OK) {
    httpd_uri_t uStatus = { "/status", HTTP_GET, statusHandler, nullptr };
    httpd_uri_t uState  = { "/state",  HTTP_POST, stateHandler, nullptr };
    httpd_register_uri_handler(g_webSrv, &uStatus);
    httpd_register_uri_handler(g_webSrv, &uState);
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
//  Camera init
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
//  WiFi
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
    digitalWrite(PIN_LED_RED, !digitalRead(PIN_LED_RED));
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    g_staConnected = true;
    Serial.printf("[WiFi] Connected! IP: %s\n", WiFi.localIP().toString().c_str());
    digitalWrite(PIN_LED_RED, LOW);
    digitalWrite(PIN_LED_GREEN, HIGH);
    delay(500);
    digitalWrite(PIN_LED_GREEN, LOW);
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
//  Setup + Loop
// ================================================================

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=============================");
  Serial.println("  SARS - Posture Assistant");
  Serial.println("=============================");

  pinMode(PIN_LED_RED, OUTPUT);
  pinMode(PIN_LED_GREEN, OUTPUT);
  pinMode(PIN_LED_YELLOW, OUTPUT);
  pinMode(PIN_LED_BLUE, OUTPUT);
  pinMode(PIN_LED_WHITE, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_BTN_CAL, INPUT_PULLUP);
  pinMode(PIN_BTN_SNOOZE, INPUT_PULLUP);

  digitalWrite(PIN_LED_RED, LOW);
  digitalWrite(PIN_LED_GREEN, LOW);
  digitalWrite(PIN_LED_YELLOW, LOW);
  digitalWrite(PIN_LED_BLUE, LOW);
  digitalWrite(PIN_LED_WHITE, LOW);
  digitalWrite(PIN_BUZZER, LOW);

  Wire.begin(PIN_SDA, PIN_SCL);
  u8g2.begin();
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_7x14B_tr);
  u8g2.drawStr(20, 35, "SARS v1.0");
  u8g2.setFont(u8g2_font_6x10_tr);
  u8g2.drawStr(25, 52, "Starting...");
  u8g2.sendBuffer();

  if (!initCamera()) {
    Serial.println("[FATAL] Camera init failed");
    u8g2.clearBuffer();
    u8g2.drawStr(4, 35, "CAMERA ERROR!");
    u8g2.sendBuffer();
    while (true) {
      digitalWrite(PIN_LED_RED, !digitalRead(PIN_LED_RED));
      delay(100);
    }
  }

  connectWiFi();
  startServers();

  updateOLED();
  Serial.println("[SARS] Ready.");
}

void loop() {
  checkButtons();
  handleBuzzer();
  updateBreakLeds();
  updateOLED();

  if (g_lastStateMs > 0 && millis() - g_lastStateMs > 30000) {
    g_postureLevel = NO_PERSON;
    g_breakText[0] = '\0';
    g_breakDue = false;
    g_breakLedState = BRK_NONE;
    updateFeedback();
    g_lastStateMs = 0;
  }

  delay(200);
}
