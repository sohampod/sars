# SARS -- Sit, Assess, Remind, Straighten

I built this for the Interactive Systems course at Saarland University. It's a posture monitor that uses an ESP32-S3 camera and MediaPipe on the PC side to track how you're sitting and nudge you to fix it.

The ESP32 handles the camera stream and physical feedback (LEDs, buzzer, OLED screen), and the PC does the actual pose estimation and serves a web dashboard at localhost:8080.

## What it does

- Tracks three posture metrics (slouch, head forward, shoulder tilt) relative to a personal calibration
- Escalating feedback: green LEDs when you're good, amber for a warning, red + buzzer when it's bad
- Pomodoro break timer (25 min work / 5 min break) with auto-detection when you leave your desk
- Web dashboard with live score gauge, camera feed with skeleton overlay, break timer, stats
- XP and leveling system, achievements, daily streaks -- stored in SQLite
- Clap detection via the built-in mic (3 claps = calibrate, 4 = snooze)
- Light/dark theme, reduced motion toggle

## Hardware

| Part | Notes |
|------|-------|
| [XIAO ESP32-S3 Sense](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/) | Has the camera + mic built in |
| SSD1306 OLED (128x64, I2C) | Shows posture level and break status |
| WS2812B NeoPixel strip (5 LEDs) | Color-coded posture feedback |
| Active buzzer | 3.3V, active-HIGH |
| Breadboard + wires, USB-C cable | |

Wiring:
```
D3 (GPIO4)  -> Buzzer
D4 (GPIO5)  -> OLED SDA
D5 (GPIO6)  -> OLED SCL
D7 (GPIO44) -> NeoPixel data
```

## Setup

You need Python 3.10+ and arduino-cli (or Arduino IDE).

```bash
git clone https://github.com/sohampod/sars.git
cd sars/pc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The MediaPipe model isn't in the repo, download it:
```bash
mkdir -p pc/models
curl -L -o pc/models/pose_landmarker_lite.task \
  "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
```

For the firmware, install the ESP32 board package (version 3.0.7 specifically -- newer ones have camera issues) and the U8g2 + Adafruit NeoPixel libraries. Copy `credentials.h.example` to `credentials.h` and put in your WiFi details, then compile and flash:
```bash
arduino-cli compile --fqbn "esp32:esp32:XIAO_ESP32S3:PSRAM=opi" firmware/sars_firmware
arduino-cli upload --fqbn "esp32:esp32:XIAO_ESP32S3:PSRAM=opi" --port /dev/cu.usbmodem101 firmware/sars_firmware
```

## Running

Check the serial monitor for the ESP32's IP, then:
```bash
cd pc
python3 main.py --url http://<ESP32_IP>:81/stream
```

Dashboard is at http://localhost:8080. Use `--webcam` to test without the ESP32.

Other flags: `--camera N` (webcam index), `--no-dashboard`, `--port PORT`.

## Calibration

Sit up straight and hit the Calibrate button on the dashboard (or clap 3 times). It takes about 2.5 seconds to capture your baseline. The calibration saves to `calibration.json` so it persists between runs.

## License

MIT
