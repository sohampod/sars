# SARS -- Sit, Assess, Remind, Straighten

A desktop posture monitor that uses an ESP32-S3 camera and MediaPipe pose estimation to track your sitting posture in real time. Built for the Interactive Systems course at Saarland University.

## Features

### Posture Detection
- Three-metric analysis: slouch detection (nose-to-shoulder ratio), forward head (ear-to-shoulder offset), shoulder tilt
- Composite score from 0.0 (worst) to 1.0 (best), relative to a personal calibration baseline
- Escalating feedback: green (good) -> amber (warning after 5s) -> red (bad after 15s) -> buzzer (after 30s)
- Smoothing window and hysteresis to avoid flickering between states

### Break Timer
- Pomodoro-based: 25-minute work sessions, 5-minute breaks
- Auto-detects when you leave your desk (no person visible for 10s)
- Snooze support (5 minutes, once per cycle)
- Focus mode to temporarily suppress break reminders

### Hardware Feedback
- NeoPixel LED strip (5 LEDs): green/amber/red based on posture state
- SSD1306 OLED display: shows posture level, break status, and WiFi info
- Active buzzer: escalating alerts for sustained bad posture
- Clap detection via built-in PDM microphone (3 claps = calibrate, 4 claps = snooze)
- Adjustable mic sensitivity with auto-calibration mode

### Web Dashboard
- Real-time posture score gauge, per-metric breakdown (slouch/head/tilt)
- Live camera feed with skeleton overlay
- Break timer ring with countdown
- Session statistics: time tracked, good posture %, average score
- 24-hour posture history chart
- Light and dark theme
- Responsive layout (desktop 4-column grid, tablet 2-column, phone stacked)

### Gamification
- XP system: earn XP for maintaining good posture, lose XP for bad posture
- 20 levels (Slouch Recruit through Spine Grandmaster)
- 10 unlockable achievements (First Steps, Hour of Power, Week Warrior, etc.)
- Daily streak tracking with multiplier bonus
- Persistent stats stored in SQLite

### Accessibility
- Text size options (small, default, large)
- Reduced motion toggle (disables animations)
- High contrast mode
- Color-blind mode (swaps green/red for blue/purple)

## Architecture

```
ESP32-S3 Sense                         PC (Python)
 +-----------------+                    +------------------------+
 | OV2640 Camera   |                    | stream_reader.py       |
 |   | MJPEG       | ---GET /stream---> |   | frames             |
 |                 |                    | posture_engine.py      |
 |                 |                    |   | MediaPipe Pose      |
 | POST /state     | <--HTTP POST----  | state_sender.py        |
 |   |             |                    | break_timer.py         |
 | NeoPixel LEDs   |                    | gamification.py        |
 | OLED Display    |                    | dashboard.py (Flask)   |
 | Buzzer          |                    |   | localhost:8080      |
 | PDM Microphone  |                    | main.py (loop)         |
 +-----------------+                    +------------------------+
```

The ESP32 handles video capture and physical feedback. The PC does all the heavy computation (pose estimation, scoring, break logic, gamification) and serves the web dashboard. Communication is bidirectional over HTTP on the local network.

## Hardware Requirements

| Component | Notes |
|-----------|-------|
| [Seeed XIAO ESP32-S3 Sense](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/) | Includes OV2640 camera and PDM microphone |
| SSD1306 OLED display | 0.96", 128x64, I2C |
| NeoPixel LED strip | 5 LEDs, WS2812B compatible |
| Active buzzer module | 3.3V, active-HIGH |
| Breadboard + jumper wires | For prototyping connections |
| USB-C cable | For flashing and power |

### Pin Wiring

```
D3  (GPIO4)   -> Buzzer (+), GND (-)
D4  (GPIO5)   -> OLED SDA (I2C data)
D5  (GPIO6)   -> OLED SCL (I2C clock)
D7  (GPIO44)  -> NeoPixel data line
3V3           -> OLED VCC
GND           -> OLED GND, Buzzer GND
```

The built-in camera and PDM microphone on the Sense expansion board require no extra wiring.

## Software Requirements

### PC

- **Python 3.10+**
  - Windows: download from [python.org](https://www.python.org/downloads/) (check "Add Python to PATH")
  - macOS: `brew install python`
  - Linux: `sudo apt install python3 python3-pip python3-venv`
- Python packages: `mediapipe`, `opencv-python`, `numpy`, `requests`, `flask`

### Firmware

- **arduino-cli** (or Arduino IDE)
- ESP32 board package: `esp32:esp32` version **3.0.7** (newer versions have camera driver issues)
- Libraries: **U8g2** (OLED), **Adafruit NeoPixel** (LEDs)

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/sohampod/sars.git
cd sars
```

### 2. Install Python dependencies

**Windows:**
```powershell
cd pc
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**macOS / Linux:**
```bash
cd pc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Linux, you may also need: `sudo apt install -y python3-venv libgl1-mesa-glx libglib2.0-0`

On macOS with `--webcam` mode, grant camera access to Terminal in System Settings -> Privacy & Security -> Camera.

### 3. Download the MediaPipe model

The Pose Landmarker Lite model (~5.5 MB) is not included in the repo. Download it into `pc/models/`:

```bash
mkdir -p pc/models
curl -L -o pc/models/pose_landmarker_lite.task \
  "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
```

Windows (PowerShell):
```powershell
New-Item -ItemType Directory -Force -Path pc\models
Invoke-WebRequest -Uri "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task" -OutFile pc\models\pose_landmarker_lite.task
```

### 4. Flash the firmware

Install arduino-cli:
- Windows: `winget install Arduino.ArduinoCLI`
- macOS: `brew install arduino-cli`
- Linux: `curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh`

Install the board package and libraries:
```bash
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.0.7 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install U8g2
arduino-cli lib install "Adafruit NeoPixel"
```

Set up WiFi credentials:
```bash
cp firmware/sars_firmware/credentials.h.example firmware/sars_firmware/credentials.h
```

Edit `credentials.h` and fill in your WiFi SSID and password. This file is gitignored.

Compile and flash (connect the ESP32 via USB-C first):
```bash
arduino-cli compile --fqbn "esp32:esp32:XIAO_ESP32S3:PSRAM=opi" firmware/sars_firmware
arduino-cli upload --fqbn "esp32:esp32:XIAO_ESP32S3:PSRAM=opi" --port <YOUR_PORT> firmware/sars_firmware
```

Find your port:
- Windows: Device Manager -> Ports (e.g. `COM5`)
- macOS: `ls /dev/cu.usbmodem*`
- Linux: `ls /dev/ttyACM*` (you may need `sudo usermod -a -G dialout $USER` for permission)

### 5. Run the application

Find the ESP32's IP address from the serial monitor:
```bash
arduino-cli monitor -p <YOUR_PORT> -c baudrate=115200
```

Look for `[WiFi] Connected! IP: 192.168.x.x`, then start the PC software:

```bash
cd pc
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
python main.py --url http://<ESP32_IP>:81/stream
```

Open `http://localhost:8080` in your browser for the dashboard.

### Webcam mode (no ESP32)

For testing without hardware:
```bash
python main.py --webcam
```

## Usage

### Command-line options

| Flag | Description |
|------|-------------|
| `--webcam` | Use PC webcam instead of ESP32 stream |
| `--camera N` | Select webcam index (default: 0) |
| `--url URL` | Override ESP32 stream URL |
| `--no-dashboard` | Run without the web dashboard |
| `--port PORT` | Override dashboard port (default: 8080) |

### Dashboard Controls

- **Calibrate** button: hold good posture for ~2.5 seconds while the system captures your baseline
- **Snooze** button: delays the current break reminder by 5 minutes
- **Focus Mode** toggle: suppresses break reminders
- **Theme** toggle: switch between light and dark mode
- **Settings** gear icon: access mic sensitivity, accessibility options, and data management

### Clap Commands (via ESP32 microphone)

| Claps | Action |
|-------|--------|
| 3 | Start calibration |
| 4 | Snooze break reminder |

### Calibration

Sit in your best posture and trigger calibration (from the dashboard button or 3 claps). The system captures ~30 frames over 2.5 seconds and computes the median of each metric as your personal baseline. Calibration is saved to `calibration.json` and auto-loaded on the next startup.

## Configuration

All parameters live in `pc/config.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `esp32_stream_url` | `http://192.168.0.10:81/stream` | ESP32 MJPEG stream endpoint |
| `good_threshold` | `0.7` | Score at or above this = good posture |
| `warning_threshold` | `0.4` | Score below this = bad posture |
| `min_landmark_confidence` | `0.5` | MediaPipe detection confidence floor |
| `warning_delay_sec` | `5.0` | Seconds of poor posture before warning |
| `bad_delay_sec` | `15.0` | Seconds before escalating to bad |
| `buzzer_delay_sec` | `30.0` | Seconds before buzzer activates |
| `work_duration_sec` | `1500` | Work session length (25 min) |
| `break_duration_sec` | `300` | Break length (5 min) |
| `snooze_duration_sec` | `300` | Snooze delay (5 min) |
| `calibration_duration_sec` | `2.5` | How long calibration capture lasts |
| `dashboard_port` | `8080` | Web dashboard port |

## Project Structure

```
sars/
+-- firmware/
|   +-- sars_firmware/
|       +-- sars_firmware.ino          # ESP32 firmware (camera, LEDs, OLED, buzzer, mic)
|       +-- credentials.h.example     # WiFi credentials template
+-- pc/
|   +-- main.py                       # Entry point and main loop
|   +-- posture_engine.py             # MediaPipe pose analysis, scoring, calibration
|   +-- stream_reader.py              # MJPEG stream consumer
|   +-- state_sender.py               # Sends posture state to ESP32 via HTTP
|   +-- break_timer.py                # Pomodoro break state machine
|   +-- gamification.py               # XP, levels, achievements, streaks
|   +-- dashboard.py                  # Flask web dashboard server
|   +-- data_logger.py                # SQLite persistence for stats
|   +-- config.py                     # All tunable parameters
|   +-- requirements.txt              # Python dependencies
|   +-- templates/
|   |   +-- dashboard.html            # Single-page dashboard UI
|   +-- static/
|       +-- fonts/                    # Geist Pixel font files
|       +-- img/                      # Background textures for level cards
+-- README.md
```

## How It Works

### Posture Scoring

The posture engine uses MediaPipe Pose Landmarker (Lite model) to detect body landmarks from each video frame. Three metrics are computed relative to your calibrated baseline:

1. **Slouch score** -- Ratio of nose-to-midpoint-of-shoulders vertical distance compared to calibration. Detects when you're hunching forward and your head drops.
2. **Head forward score** -- Horizontal offset between ears and shoulders compared to calibration. Detects when your head drifts ahead of your torso.
3. **Shoulder tilt score** -- Difference in vertical position between left and right shoulders. Detects lateral leaning.

Each metric produces a score from 0 to 1. The composite score is a weighted combination. An 8-frame smoothing window and 4-frame hysteresis prevent jitter at state boundaries.

### Communication Flow

1. The ESP32 streams MJPEG frames from the OV2640 camera on port 81
2. The PC's `stream_reader.py` consumes the stream and yields decoded frames
3. `posture_engine.py` runs MediaPipe on every 3rd frame (for performance)
4. `state_sender.py` POSTs the current posture level, break state, and gamification data back to the ESP32 as JSON
5. The ESP32 firmware updates LEDs, OLED, and buzzer based on the received state
6. The Flask dashboard polls `/api/live` every 500ms and renders the UI

### Gamification

- You earn ~0.17 XP per second of good posture and lose ~0.08 XP per second of bad posture
- 20 levels with increasing XP thresholds (50 XP for level 2 up to 34,000 XP for level 20)
- Achievements unlock for milestones like first calibration, 1 hour of good posture, 7-day streak, etc.
- Daily stats and streaks are persisted in a SQLite database

## Troubleshooting

**"Model not found" error**
Download the MediaPipe model file -- see Setup step 3.

**Cannot connect to ESP32 stream**
Make sure both devices are on the same WiFi network. Check the ESP32's IP in the serial monitor (`arduino-cli monitor -p <PORT> -c baudrate=115200`). If WiFi fails, the ESP32 creates a fallback access point named `SARS-Kamera` (password: `sars1234`).

**Camera init failed on ESP32**
Use board package version 3.0.7 specifically. Newer versions have known issues with the OV2640 on the XIAO ESP32-S3 Sense.

**Serial port permission denied (Linux)**
Add your user to the `dialout` group: `sudo usermod -a -G dialout $USER`, then log out and back in.

**Dashboard not loading**
Check that port 8080 is not in use. Use `--port 9090` (or another port) as an alternative.

**Low detection accuracy**
Make sure the camera can see your upper body (head and both shoulders). Avoid strong backlighting. Run calibration while sitting in your normal good posture.

**Clap detection not working**
Open the dashboard settings and adjust the mic sensitivity slider. The auto-sensitivity mode tries to adapt to your environment, but manual tuning may work better in noisy rooms.

## Security

SARS is designed for **local network use only**. All communication between the ESP32 and PC happens over your local WiFi — no data is sent to the cloud.

### Privacy

- Camera frames are processed locally on your PC by MediaPipe and never stored or transmitted beyond the local network
- Posture scores are saved to a local SQLite database on your PC
- The MJPEG stream is accessible to anyone on the same WiFi network — use a private network

### API Key Authentication

The ESP32 endpoints that modify state (`/state`, `/config`, `/buzzer`) support API key authentication:

1. Set an API key in `credentials.h`:
   ```c
   const char* API_KEY = "your-secret-key-here";
   ```

2. Pass the same key when running the PC software:
   ```bash
   python3 main.py --url http://<IP>:81/stream --key your-secret-key-here
   ```

3. Leave `API_KEY` empty to disable authentication (default for development)

### Network Recommendations

- Use a private WiFi network (not public/shared networks)
- The dashboard binds to `localhost` — only accessible from the PC running the software
- The ESP32 camera stream (port 81) is unencrypted — this is a hardware limitation of the ESP32-S3

## License

MIT
