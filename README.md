# SARS — Coding Posture and Break Assistant

A desktop device that monitors your sitting posture via camera and reminds you to take breaks. Built for the Interactive Systems university course.

## How It Works

The system has two parts that communicate over WiFi:

```
ESP32-S3 Sense                    PC (Python)
 ┌──────────────┐                 ┌───────────────────────┐
 │ OV2640 Camera │                 │  stream_reader.py     │
 │   ↓ MJPEG    │ ──GET /stream──>│    ↓ frames           │
 │              │                 │  posture_engine.py    │
 │ POST /state  │ <──HTTP POST──  │    ↓ analysis         │
 │   ↓          │                 │  state_sender.py      │
 │ LEDs / OLED  │                 │  break_timer.py       │
 │ Buzzer       │                 │  main.py (UI + loop)  │
 └──────────────┘                 └───────────────────────┘
```

1. **ESP32-S3** streams MJPEG video from the OV2640 camera over WiFi
2. **PC** consumes the stream, runs MediaPipe Pose Landmarker to detect body landmarks
3. **Posture engine** computes a composite score from three metrics (nose-shoulder ratio, head-forward offset, shoulder tilt) relative to a calibrated baseline
4. **PC** sends the posture level + break timer state back to the ESP32
5. **ESP32** drives LEDs, buzzer, and OLED display for real-time feedback

### Posture Scoring

Three metrics, each relative to your calibrated "good posture" baseline:

| Metric | Weight | What it detects |
|--------|--------|----------------|
| Nose-to-shoulder vertical ratio | 60% | Slouching (head drops relative to shoulders) |
| Ear-to-shoulder horizontal offset | 25% | Forward head posture (head drifts ahead of shoulders) |
| Shoulder tilt | 15% | Lateral lean (one shoulder higher than the other) |

Combined score: 0.0 (worst) to 1.0 (best). Thresholds: ≥0.7 = Good, 0.4–0.7 = Warning, <0.4 = Bad.

### Break Timer

Evidence-based break intervals with escalating reminders:

| Phase | Trigger | Feedback |
|-------|---------|----------|
| Working | 0–20 min | Countdown to next break |
| Micro break due | 20 min | Yellow alert — look away from screen |
| Active break due | 30 min | Orange alert — stand up and stretch |
| Hard ceiling | 55 min | Red alert — you must take a break |
| On break | Person leaves for >60s | Auto-detected, timer resets |
| Break over | Person returns | White LED blinks for 15s welcome-back |

Snooze: press S (keyboard) or the snooze button (hardware) for a 5-minute delay. One snooze per cycle.

## Hardware

### Required Components

- [Seeed XIAO ESP32-S3 Sense](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/) (with OV2640 camera module)
- SSD1306 OLED display (0.96" 128x64, I2C)
- Active buzzer module
- 2x tactile push buttons
- 5x LEDs: red, green, yellow, blue, white
- 5x 220Ω resistors (one per LED)
- Breadboard + jumper wires (male-to-male)
- USB-C cable

### Pin Map

```
D0  (GPIO1)  → [220Ω] → RED LED → GND        (bad posture)
D1  (GPIO2)  → [220Ω] → GREEN LED → GND      (good posture)
D2  (GPIO3)  → [220Ω] → YELLOW LED → GND     (warning)
D3  (GPIO4)  → BUZZER (+) → GND (-)           (alerts)
D4  (GPIO5)  → OLED SDA                       (I2C data)
D5  (GPIO6)  → OLED SCL                       (I2C clock)
3V3          → OLED VCC
GND          → OLED GND
D6  (GPIO43) → BUTTON 1 → GND                 (calibrate)
D7  (GPIO44) → BUTTON 2 → GND                 (snooze/break)
D8  (GPIO7)  → [220Ω] → BLUE LED → GND       (break due — blinks)
D9  (GPIO8)  → [220Ω] → WHITE LED → GND      (break over — blinks)
```

Buttons use internal pull-up resistors (no external resistor needed).

## Software Requirements

### PC Side

- **Python 3.10+**
- Dependencies: mediapipe, opencv-python, numpy, requests

### Firmware Side

- **arduino-cli** (or Arduino IDE)
- ESP32 board package: `esp32:esp32` **version 3.0.7** (newer versions may break the camera driver)
- Library: **U8g2** (for OLED display)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/sohampod/sars.git
cd sars
```

### 2. Set up the PC software

#### Windows

```powershell
cd pc
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

#### macOS

```bash
cd pc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> **Note (macOS):** If using webcam mode (`--webcam`), grant camera access to your Terminal app in System Settings → Privacy & Security → Camera.

#### Linux

```bash
# OpenCV display requires these system packages
sudo apt install -y libgl1-mesa-glx libglib2.0-0

cd pc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Download the MediaPipe model

Download the Pose Landmarker Lite model and place it in `pc/models/`:

```bash
mkdir -p pc/models
curl -L -o pc/models/pose_landmarker_lite.task \
  "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
```

On Windows (PowerShell):

```powershell
New-Item -ItemType Directory -Force -Path pc\models
Invoke-WebRequest -Uri "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task" -OutFile pc\models\pose_landmarker_lite.task
```

### 4. Flash the firmware

#### Install arduino-cli

- **Windows**: `winget install Arduino.ArduinoCLI`
- **macOS**: `brew install arduino-cli`
- **Linux**: `curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh`

#### Install the ESP32 board package

```bash
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.0.7 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
```

#### Install the U8g2 library

```bash
arduino-cli lib install U8g2
```

#### Configure WiFi credentials

```bash
cp firmware/sars_firmware/credentials.h.example firmware/sars_firmware/credentials.h
```

Edit `firmware/sars_firmware/credentials.h` and set your WiFi SSID and password.

#### Compile and flash

Connect the ESP32-S3 via USB-C, then find your serial port:

- **Windows**: Check Device Manager → Ports. Typically `COM3`, `COM4`, etc.
- **macOS**: `ls /dev/cu.usbmodem*`
- **Linux**: `ls /dev/ttyACM*` or `ls /dev/ttyUSB*`

```bash
arduino-cli compile --fqbn "esp32:esp32:XIAO_ESP32S3:PSRAM=opi" firmware/sars_firmware
arduino-cli upload --fqbn "esp32:esp32:XIAO_ESP32S3:PSRAM=opi" --port <YOUR_PORT> firmware/sars_firmware
```

Replace `<YOUR_PORT>` with your serial port (e.g., `COM5`, `/dev/cu.usbmodem14101`, `/dev/ttyACM0`).

**Linux note**: You may need to add your user to the `dialout` group for serial port access:

```bash
sudo usermod -a -G dialout $USER
# Log out and back in for the change to take effect
```

## Usage

### With ESP32 stream (normal mode)

Make sure both the ESP32 and your PC are on the same WiFi network. After flashing, the ESP32 prints its IP address to the serial monitor.

```bash
cd pc
python main.py --url http://<ESP32_IP>:81/stream
```

### With PC webcam (no ESP32 needed)

For testing or development without the ESP32 hardware:

```bash
cd pc
python main.py --webcam
```

Use `--camera N` to select a specific webcam if you have multiple (default: 0).

### Keyboard Controls

| Key | Action |
|-----|--------|
| `C` | Start calibration (hold good posture for 2 seconds) |
| `S` | Snooze break reminder (5 minutes, once per cycle) |
| `B` | Acknowledge break (resets the timer) |
| `Q` | Quit |

### Calibration

Press `C` and hold your best sitting posture for 2 seconds. The system captures ~30 frames and uses the median of each metric as your personal baseline. Calibration is saved to `calibration.json` and auto-loaded on next startup.

## Configuration

All tunable parameters are in `pc/config.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `good_threshold` | 0.7 | Score above this = good posture |
| `warning_threshold` | 0.4 | Score below this = bad posture |
| `micro_break_interval_sec` | 1200 | Micro break reminder (20 min) |
| `active_break_interval_sec` | 1800 | Active break reminder (30 min) |
| `hard_ceiling_sec` | 3300 | Hard ceiling (55 min) |
| `snooze_duration_sec` | 300 | Snooze duration (5 min) |

## Project Structure

```
sars/
├── firmware/
│   └── sars_firmware/
│       ├── sars_firmware.ino        # ESP32 firmware
│       └── credentials.h.example    # WiFi credentials template
├── pc/
│   ├── main.py                      # Main application loop + UI
│   ├── posture_engine.py            # MediaPipe pose analysis + calibration
│   ├── stream_reader.py             # MJPEG stream consumer
│   ├── state_sender.py              # PC → ESP32 state communication
│   ├── break_timer.py               # Break reminder state machine
│   ├── config.py                    # All tunable parameters
│   └── requirements.txt             # Python dependencies
└── README.md
```

## Troubleshooting

**"Model not found" error**: Download the MediaPipe model file (see Installation step 3).

**Cannot connect to ESP32 stream**: Verify both devices are on the same WiFi network. Check the ESP32's IP in the serial monitor (115200 baud).

**Camera init failed on ESP32**: Make sure you're using board package version 3.0.7. Newer versions have known issues with the OV2640 camera on the XIAO ESP32-S3 Sense.

**Serial port permission denied (Linux)**: Add your user to the `dialout` group (see Installation step 4).

**Low FPS**: The camera streams at QVGA (320x240) with JPEG quality 12 — this is intentional for performance. MediaPipe internally resizes to 256x256 anyway.

## License

This project was created for the Interactive Systems course at Saarland University.
