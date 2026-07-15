# Arcane OS // Project Context & Architecture Documentation

> **Version:** 3.0.0 · **Repository:** `AmaanSayed24/Clap-OS`

---

## 1. Executive Summary

**Arcane OS** is a real-time, dual-stage transient acoustic event listener coupled with a multi-monitor Win32 window orchestrator and WebSocket-driven HUD console. It continuously monitors microphone input for double-clap patterns and upon verification, deploys a synchronized multi-application workspace across specific Windows displays, complete with AI vocal greetings and media routing.

### v3.0 Upgrades
- **Voice Verification Security Gate** — local, offline passphrase validation ("Hey Arcane") before workspace deployment
- **Per-Monitor v2 DPI Awareness** — accurate physical pixel coordinates on mixed-DPI multi-monitor setups
- **WASAPI Low-Latency Audio** — bypasses the Windows audio mixer layer for ~10-20ms less input latency
- **Structured Logging** — all `print()` calls replaced with Python `logging` module
- **Chromium Process Optimization** — reduces background CPU/memory in Brave browser instances
- **Process Priority Elevation** — `HIGH_PRIORITY_CLASS` protects audio thread during burst app deployment
- **OS Tuning Script** — `arcane_os_tune.ps1` for DPC hardening, power plan, and DWM animation disabling

---

## 2. System Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                        MICROPHONE INPUT STREAM                         │
│           (44.1 kHz float32 via WASAPI low-latency / PortAudio)        │
└───────────────────────────────────┬────────────────────────────────────┘
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   ACOUSTIC PROCESSING CORE (Backend)                   │
│  • Adaptive Noise Floor Tracking (2.0s Calibration Gate)               │
│  • Dual-Stage Transient State Machine (Stage 0 → Stage 1 → Stage 2)   │
│  • Local Whisper Voice Verification Gate (Stage 2 ➔ Stage 3)           │
│  • DPI-Aware Win32 Monitor Enumeration (Per-Monitor v2, cached)        │
└───────────────────┬───────────────────────────────────┬────────────────┘
                    │ WebSocket Broadcast               │ Win32 Spawn / API
                    ▼ (ws://127.0.0.1:8765)             ▼
┌───────────────────────────────────────┐  ┌─────────────────────────────┐
│       WEB HUD DASHBOARD (Frontend)    │  │  WORKSPACE ORCHESTRATOR     │
│  • Futuristic Sci-Fi Console (Sora)   │  │  • Spotify (Media Route)    │
│  • Real-time RMS Visualizer & Gauges  │  │  • Claude Desktop App       │
│  • Live Pipeline State Tracking       │  │  • Antigravity IDE          │
│  • Automatic Offline Demo Mode        │  │  • WhatsApp Web (Brave)     │
└───────────────────────────────────────┘  │  • ElevenLabs Voice Speech  │
                                           └─────────────────────────────┘
```

---

## 3. Core Components

### A. `arcane_server.py` — Production Backend v3.0

**Key systems:**
1. **DPI Awareness Injection** — `SetProcessDpiAwarenessContext(-4)` called at module load before any `user32.dll` calls. Ensures `EnumDisplayMonitors` returns exact physical pixel boundaries, not virtualized coordinates.
2. **WASAPI Device Selection** — `_find_wasapi_device()` scans for the Windows WASAPI host API and selects the first available input device, bypassing the MME/DirectSound kernel mixer layer. Respects `ARCANE_INPUT_DEVICE` env var.
3. **Voice Verification Security Gate** — Double-clap triggers Stage 2 (Awaiting Auth), stops clap detection, and starts Whisper tiny.en local speech-to-text listener. Verifies passphrase "Hey Arcane" or similar phonetic strings.
   - **Success:** Sets Stage 3, runs automated workspace orchestration.
   - **Failure/Timeout:** Re-arms Stage 0 (Standby), plays access denied vocalizer, and restarts clap detection.
4. **Cached Monitor Enumeration** — `Win32WindowManager` caches monitor rectangles on first call, with `invalidate_cache()` for display shifts.
5. **Chromium Optimization** — Brave instances launched with `--disable-background-timer-throttling` and `--disable-renderer-backgrounding`.
6. **Structured Logging** — Colored `[Arcane]` format via Python `logging` module.

### B. `index.html` — System Console HUD

- Zero-dependency frontend (Vanilla HTML5/CSS3/ES6)
- **Fonts:** Sora (display) + JetBrains Mono (telemetry)
- **Palette:** Deep-space dark (`#07090f`), cyan (`#31d7f7`), violet (`#8b6cf5`), mint (`#3dffb0`), amber (`#ffcf7a`)
- **Demo Mode:** Automatic fallback with simulated telemetry when backend is offline
- **WebSocket:** Exponential backoff retry (1.5s base, max 5 retries)

### C. `arcane.py` — CLI Orchestrator & Calibration

- **`--calibrate`:** Interactive 5-second calibration recording, measures ambient RMS and clap peaks, suggests `SPIKE_RATIO` and `MIN_RMS` values
- **WASAPI-aware device selection** — `find_optimal_input_device()` prefers WASAPI variant of any matched device
- **Extended targets:** VS Code, Claude (web), Binance (web), configurable per-monitor placement

### D. `start.ps1` — Lifecycle Bootstrapper v3.0

1. Git sync (`git pull origin main --quiet`)
2. Virtual environment activation
3. Launch `arcane_server.py --headless` with `HIGH_PRIORITY_CLASS` process elevation
4. Stays alive to monitor the background process

### E. `arcane_os_tune.ps1` — OS System Tuner (Administrator)

One-time setup script with `-Undo` revert flag:
- **Tier 1:** Disable audio device idle power management, disable audio APO enhancements
- **Tier 2:** Activate Ultimate Performance power plan, disable CPU core parking
- **Tier 3:** Disable DWM window minimize/maximize animations

### F. `arcane_launcher.bat` — Silent Background Launcher

- Portable background startup script (`%~dp0` directory resolution)
- Activates virtual environment and executes `arcane_server.py --headless` with `start /B /HIGH`
- Ideal for Windows Task Scheduler deployment at user login

---

## 4. File Structure

```
arcane/
├── .cache/
│   └── arcane_vocal/        # SHA-256 cached ElevenLabs greeting .wav files
├── .env                     # ElevenLabs API keys, voice ID, and target URLs (ignored by git)
├── .gitignore               # .cache/, .venv/, __pycache__/, *.pyc, .env
├── README.md                # Project quickstart
├── requirements.txt         # sounddevice, numpy, python-dotenv, websockets, elevenlabs
├── start.ps1                # Lifecycle bootstrapper (HIGH_PRIORITY_CLASS)
├── arcane_launcher.bat      # [NEW] Silent background launcher for Windows Task Scheduler
├── arcane_os_tune.ps1       # Admin OS tuning script (-Undo supported)
├── arcane.py                # CLI orchestrator & --calibrate utility
├── arcane_server.py         # Production backend (DPI, WASAPI, structured logging)
├── index.html               # Futuristic System Console HUD
└── context.md               # This document
```

---

## 5. Environment Variables (`.env`)

> **Security Note:** The `.env` file is explicitly excluded via `.gitignore` (`Line 5: .env`) and is never tracked, committed, or pushed to remote repositories. All sensitive credentials (`ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`) remain strictly local to the host system.

| Variable | Description | Example |
|:---|:---|:---|
| `ELEVENLABS_API_KEY` | ElevenLabs TTS API key | `sk_xxxxxxxxxxxxxxxxxxxxxxxx` |
| `ELEVENLABS_VOICE_ID` | Voice UUID for greeting | `xxxxxxxxxxxxxxxxxxxx` |
| `ELEVENLABS_MODEL_ID` | Model architecture | `eleven_multilingual_v2` |
| `ELEVENLABS_OUTPUT_FORMAT` | Audio format for playback | `pcm_24000` |
| `ARCANE_INPUT_DEVICE` | Preferred microphone name/index | `Realtek` or `22` |
| `CLAUDE_CODE_URL` | Web target URL for Claude instance | `https://claude.ai/new` |
| `BINANCE_BTC_URL` | Web target URL for Binance monitor | `https://www.binance.com/en/trade/BTC_USDT` |

---

## 6. Tuning Knobs

| Knob | Server v3 | CLI v3 | Description |
|:---|:---|:---|:---|
| `SAMPLE_RATE` | `44100` | `44100` | Mic capture frequency (Hz) |
| `BLOCK_MS` | `40` | `30` | Audio buffer window (ms) |
| `SPIKE_RATIO` | `7.0` | `6.0` | Multiplier above noise floor |
| `MIN_DOUBLE_GAP_S` | `0.08` | `0.08` | Min gap between claps (s) |
| `MAX_DOUBLE_GAP_S` | `0.45` | `0.65` | Max gap between claps (s) |
| `COOLDOWN_S` | `0.50` | `0.18` | Debounce post-spike (s) |
| `MIN_RMS` | `0.012` | `0.008` | Absolute minimum RMS threshold |

---

## 7. Quick Commands

```powershell
# Production server (with UI)
.\.venv\Scripts\python.exe arcane_server.py

# Headless with HIGH priority (PowerShell)
.\start.ps1

# Silent background launcher (Batch / Task Scheduler)
.\arcane_launcher.bat

# Calibrate microphone
.\.venv\Scripts\python.exe arcane.py --calibrate

# Apply OS-level tuning (run as Administrator)
.\arcane_os_tune.ps1

# Revert OS tuning
.\arcane_os_tune.ps1 -Undo
```
