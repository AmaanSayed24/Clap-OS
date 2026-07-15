#!/usr/bin/env python3
"""
Arcane Backend Server v3.0
--------------------------
Handles non-looping dual-stage audio transient routing and native Win32 window positioning.

v3.0 Upgrades:
  - Per-Monitor v2 DPI Awareness for accurate multi-monitor coordinates
  - WASAPI low-latency audio device selection (Windows)
  - Structured logging replacing raw print() calls
  - Chromium process optimization flags for Brave browser instances
  - Cached monitor enumeration with DPI-safe physical pixel boundaries
"""

import os
import sys
import time
import asyncio
import threading
import subprocess
import webbrowser
import json
import wave
import hashlib
import logging
import tempfile
import shutil
from pathlib import Path

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

# =============================================================================
#  Win32 DPI Awareness — MUST run before any ctypes.windll.user32 calls
# =============================================================================
if sys.platform == "win32":
    import ctypes
    try:
        # Per-Monitor v2 DPI Awareness Context (-4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        try:
            # Fallback for older Win10 builds: PROCESS_PER_MONITOR_DPI_AWARE (2)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

# =============================================================================
#  Logging
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [\033[1;35mArcane\033[0m] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("arcane_server")

# =============================================================================
#  Initialization
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- Performance Architecture Knobs ---
SAMPLE_RATE = 44100
BLOCK_MS = 40
SPIKE_RATIO = 7.0  # Higher baseline helps filter keyboard clicks/breathing
COOLDOWN_S = 0.50  # Debounce padding window
MIN_DOUBLE_GAP_S = 0.08
MAX_DOUBLE_GAP_S = 0.45
MIN_RMS = 0.012

# --- Custom Target Allocations ---
WORKSPACE_TRACK_URI = "https://open.spotify.com/track/4iLqG9SeJSnt0cSPICSjxv"
BRAVE_FORCE_FULLSCREEN = True

WHATSAPP_DISPLAY_MONITOR = 1  # Monitor for WhatsApp Web

ARCANE_VOCAL_GREETING_ENABLED = True
ARCANE_VOCAL_PHRASE = (
    "Welcome back sir. Your workspace is online and all systems are ready. "
    "Claude is standing by, your IDE is loaded, and I'm here whenever you need me. "
    "Just say the word and let's get to work."
)

# Global State Vectors (State Machine Protection)
CONNECTED_CLIENTS = set()
CURRENT_STAGE = 0  # 0 = Headless, 1 = UI Visible, 2 = Active Environment deployed
STATE_LOCK = threading.Lock()


# =============================================================================
#  WASAPI Low-Latency Audio Device Selection
# =============================================================================

def _find_wasapi_device() -> int | None:
    """
    Find the default WASAPI input device for lowest-latency capture on Windows.
    Returns the device index if found, None otherwise.
    WASAPI bypasses the Windows audio mixer layer, eliminating ~10-20ms of
    kernel buffering that MME/DirectSound host APIs add.
    """
    if sys.platform != "win32":
        return None
    try:
        hostapis = sd.query_hostapis()
        wasapi_idx = next(
            (i for i, api in enumerate(hostapis) if "WASAPI" in api["name"]),
            None,
        )
        if wasapi_idx is None:
            return None
        # Prefer ARCANE_INPUT_DEVICE env var if set
        override = (os.environ.get("ARCANE_INPUT_DEVICE") or "").strip()
        for dev_idx, dev in enumerate(sd.query_devices()):
            if dev["hostapi"] != wasapi_idx or dev["max_input_channels"] < 1:
                continue
            if override and override.lower() in dev["name"].lower():
                return dev_idx
        # Fallback: first WASAPI input device
        for dev_idx, dev in enumerate(sd.query_devices()):
            if dev["hostapi"] == wasapi_idx and dev["max_input_channels"] >= 1:
                return dev_idx
    except Exception as e:
        log.warning("WASAPI device discovery failed: %s", e)
    return None


# =============================================================================
#  Win32 Window Manager (DPI-Aware, Cached)
# =============================================================================

class Win32WindowManager:
    """
    Enumerates physical monitor boundaries using native Win32 APIs.
    With Per-Monitor v2 DPI awareness set at process startup,
    EnumDisplayMonitors returns exact physical pixel coordinates —
    no coordinate virtualization even on mixed-DPI multi-monitor setups.
    """
    _cached_monitors: list[tuple[int, int, int, int]] | None = None

    @classmethod
    def _enumerate_monitors(cls) -> list[tuple[int, int, int, int]]:
        """Enumerate all monitors and cache the result."""
        if sys.platform != "win32":
            return [(0, 0, 1920, 1080)]
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        rects = []

        @ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(RECT),
            wintypes.LPARAM,
        )
        def callback(_hm, _hdc, lprc, _lp):
            r = lprc.contents
            rects.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
            return True

        ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
        rects.sort(key=lambda item: (item[0], item[1]))
        cls._cached_monitors = rects
        log.info(
            "Enumerated %d physical monitor(s) with DPI-aware coordinates",
            len(rects),
        )
        return rects

    @classmethod
    def get_monitor_bounds(cls, index: int) -> tuple[int, int, int, int]:
        """
        Get the (left, top, right, bottom) pixel bounds for monitor at `index` (1-based).
        Falls back to 1920x1080 if no monitors are detected.
        """
        monitors = cls._cached_monitors or cls._enumerate_monitors()
        if not monitors:
            return (0, 0, 1920, 1080)
        adjusted_idx = max(0, min(index - 1, len(monitors) - 1))
        return monitors[adjusted_idx]

    @classmethod
    def invalidate_cache(cls):
        """Force re-enumeration on next call (e.g., after display config change)."""
        cls._cached_monitors = None


class ArcaneVocalizer:
    @classmethod
    def synthesize_and_play(cls, loop) -> None:
        if not ARCANE_VOCAL_GREETING_ENABLED or not ARCANE_VOCAL_PHRASE.strip():
            return
        phrase = ARCANE_VOCAL_PHRASE.strip()
        voice_id = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
        model_id = (
            os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2"
        ).strip()
        out_fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()

        if not voice_id:
            log.warning("ELEVENLABS_VOICE_ID not set — vocal greeting skipped.")
            send_stage(loop, "vocal", "error", "missing voice ID")
            return

        cache_dir = BASE_DIR / ".cache" / "arcane_vocal"
        hash_key = f"{phrase}|{voice_id}|{model_id}|{out_fmt}".encode()
        cache_file = cache_dir / f"{hashlib.sha256(hash_key).hexdigest()[:24]}.wav"

        if cache_file.is_file():
            try:
                with wave.open(str(cache_file), "rb") as wf:
                    raw = wf.readframes(wf.getnframes())
                    rate = wf.getframerate()
                arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                sd.play(arr, rate)
                sd.wait()
                log.info("Vocal greeting played from cache.")
                send_stage(loop, "vocal", "done", "played from cache")
                return
            except Exception as e:
                log.warning("Cache read fail: %s", e)

        api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
        if not api_key:
            log.warning("ELEVENLABS_API_KEY not set — vocal greeting skipped.")
            send_stage(loop, "vocal", "error", "missing API key")
            return

        try:
            from elevenlabs.client import ElevenLabs

            client = ElevenLabs(api_key=api_key)
            audio_stream = client.text_to_speech.convert(
                voice_id=voice_id, text=phrase, model_id=model_id, output_format=out_fmt
            )
            raw_data = b"".join(audio_stream)

            cache_dir.mkdir(parents=True, exist_ok=True)
            with wave.open(str(cache_file), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(raw_data)

            arr = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
            sd.play(arr, 24000)
            sd.wait()
            log.info("Vocal greeting synthesized and delivered.")
            send_stage(loop, "vocal", "done", "greeting delivered")
        except Exception as e:
            log.warning("ElevenLabs live fetch failed: %s", e)
            send_stage(loop, "vocal", "error", str(e)[:72])


def spawn_brave_instance(url: str, monitor: int, label: str) -> None:
    if not url.strip():
        return
    brave_path = None
    if sys.platform == "win32":
        for path_env in ["ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"]:
            base = os.environ.get(path_env, "")
            if base:
                target = os.path.join(
                    base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"
                )
                if os.path.isfile(target):
                    brave_path = target
                    break
    else:
        brave_path = shutil.which("brave-browser") or shutil.which("brave")

    if not brave_path:
        log.warning(
            "Brave execution path trace failed. Reverting to browser fallback for %s",
            label,
        )
        webbrowser.open(url)
        return

    cmd = [brave_path, "--new-window"]

    # Chromium process optimization flags — reduce background CPU/memory usage
    # These are safe: they don't break extensions, only reduce idle resource consumption
    cmd += [
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
    ]

    if sys.platform == "win32":
        left, top, right, bottom = Win32WindowManager.get_monitor_bounds(monitor)
        cmd.append(f"--window-position={left},{top}")
        if BRAVE_FORCE_FULLSCREEN:
            cmd.append(f"--window-size={right - left},{bottom - top}")
            cmd.append("--start-fullscreen")
    elif BRAVE_FORCE_FULLSCREEN:
        cmd.append("--start-fullscreen")

    cmd.append(url)
    kw = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        subprocess.Popen(cmd, **kw)
        log.info(
            "Launched %s in Brave window on display node #%d", label, monitor
        )
    except Exception as e:
        log.error("Failed to spin up Brave instance: %s", e)


def focus_or_launch_antigravity() -> None:
    """Launch or focus Antigravity IDE."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    exe_target = None
    if sys.platform == "win32":
        paths = [
            os.path.join(local_app, "Programs", "Antigravity IDE", "Antigravity IDE.exe"),
        ]
        for p in paths:
            if os.path.isfile(p):
                exe_target = p
                break
    else:
        exe_target = shutil.which("antigravity-ide") or shutil.which("code")

    if not exe_target:
        log.warning("Antigravity IDE not found on disk.")
        return
    kw = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen([exe_target], **kw)
        log.info("Antigravity IDE environment initialized.")
    except Exception as e:
        log.error("Antigravity IDE launch failed: %s", e)


def launch_claude_app() -> None:
    """Launch the Claude desktop app."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    exe_target = None
    if sys.platform == "win32":
        candidate = os.path.join(local_app, "AnthropicClaude", "claude.exe")
        if os.path.isfile(candidate):
            exe_target = candidate

    if not exe_target:
        log.warning("Claude desktop app not found.")
        return
    kw = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen([exe_target], **kw)
        log.info("Claude desktop app launched.")
    except Exception as e:
        log.error("Claude app launch failed: %s", e)


def execute_workspace_deployment(loop):
    """Unrolls the deployment sequence exactly once with isolated handlers."""
    log.info("\033[1;32m[⚡] Transients verified. Unrolling environment…\033[0m")

    # 1. Spotify Protocol
    if WORKSPACE_TRACK_URI.strip():
        send_stage(loop, "spotify", "active")
        try:
            if sys.platform == "win32":
                os.startfile(WORKSPACE_TRACK_URI.strip())
            else:
                webbrowser.open(WORKSPACE_TRACK_URI.strip())
            log.info("Media route injected -> Playing Attention by Charlie Puth")
            send_stage(loop, "spotify", "done", "Attention — Charlie Puth")
        except Exception as e:
            log.error("Spotify launch error: %s", e)
            send_stage(loop, "spotify", "error", str(e))

    # 2. Claude Desktop App
    send_stage(loop, "claude", "active")
    try:
        launch_claude_app()
        send_stage(loop, "claude", "done", "Desktop app")
    except Exception as e:
        log.error("Claude launch error: %s", e)
        send_stage(loop, "claude", "error", str(e))

    # 3. Antigravity IDE (reopens last session automatically)
    send_stage(loop, "antigravity", "active")
    try:
        focus_or_launch_antigravity()
        send_stage(loop, "antigravity", "done", "Last project restored")
    except Exception as e:
        log.error("Antigravity IDE launch error: %s", e)
        send_stage(loop, "antigravity", "error", str(e))

    # 4. WhatsApp Web
    send_stage(loop, "whatsapp", "active")
    try:
        spawn_brave_instance("https://web.whatsapp.com", WHATSAPP_DISPLAY_MONITOR, "WhatsApp Web")
        send_stage(loop, "whatsapp", "done", f"Monitor {WHATSAPP_DISPLAY_MONITOR}")
    except Exception as e:
        log.error("WhatsApp Web launch error: %s", e)
        send_stage(loop, "whatsapp", "error", str(e))

    # 5. Background Vocal Synthesis Thread
    if ARCANE_VOCAL_GREETING_ENABLED:
        send_stage(loop, "vocal", "active")
        threading.Thread(target=ArcaneVocalizer.synthesize_and_play, args=(loop,), daemon=True).start()

    # Finish sequence
    try:
        asyncio.run_coroutine_threadsafe(broadcast({"event": "sequence_done"}), loop)
    except RuntimeError:
        pass

    # Keep CURRENT_STAGE at 3 — sequence runs once per server session.
    # Start background active listener loop for lock trigger
    log.info("Workspace deployed. Starting background active listener loop for lock trigger...")
    threading.Thread(target=audio_stream_loop, args=(loop,), daemon=True).start()


VOICE_MODEL = None

def preload_voice_model():
    global VOICE_MODEL
    if VOICE_MODEL is not None:
        return
    try:
        from faster_whisper import WhisperModel
        log.info("Pre-loading Whisper tiny.en local voice engine...")
        VOICE_MODEL = WhisperModel("tiny.en", device="cpu", compute_type="int8")
        log.info("Whisper voice engine pre-loaded successfully.")
    except Exception as e:
        log.error("Failed to pre-load Whisper model: %s", e)


def voice_verification_loop(loop):
    global CURRENT_STAGE, VOICE_MODEL
    log.info("\033[1;33m[Voice Auth]\033[0m Voice verification sequence started.")

    # Check if voice engine is ready
    if VOICE_MODEL is None:
        log.info("\033[1;33m[Voice Auth]\033[0m Voice engine not pre-loaded. Initializing now...")
        asyncio.run_coroutine_threadsafe(
            broadcast({"event": "voice_auth_status", "status": "loading", "detail": "Initializing voice engine..."}),
            loop
        )
        preload_voice_model()
        if VOICE_MODEL is None:
            handle_auth_failure(loop, "Voice engine failed to load")
            return

    model = VOICE_MODEL

    # Broadcast that we are listening
    asyncio.run_coroutine_threadsafe(
        broadcast({"event": "voice_auth_status", "status": "listening", "detail": "Say 'Hey Arcane' to unlock..."}),
        loop
    )

    # Voice recording configuration (3.5 seconds is plenty for "Hey Arcane")
    duration_s = 3.5
    dev_idx = _find_wasapi_device()
    
    # Determine the native sample rate of the device
    native_rate = 16000
    if dev_idx is not None:
        try:
            dev_info = sd.query_devices(dev_idx)
            native_rate = int(dev_info["default_samplerate"])
        except Exception:
            pass
    else:
        try:
            native_rate = int(sd.query_devices(kind='input')["default_samplerate"])
        except Exception:
            pass

    block_samples = int(native_rate * duration_s)
    log.info("\033[1;33m[Voice Auth]\033[0m Recording mic array for %s seconds at native %d Hz...", duration_s, native_rate)

    try:
        rec_kwargs = {
            "samplerate": native_rate,
            "channels": 1,
            "dtype": "float32"
        }
        if dev_idx is not None:
            rec_kwargs["device"] = dev_idx

        audio = sd.rec(block_samples, **rec_kwargs)
        sd.wait() # block until recording is complete
        audio = audio.flatten()
        log.info("\033[1;33m[Voice Auth]\033[0m Recording complete. Processing transcription...")
        
        # Resample to 16000 Hz if native rate is different
        if native_rate != 16000:
            log.info("\033[1;33m[Voice Auth]\033[0m Downsampling from %d Hz to 16000 Hz...", native_rate)
            duration = len(audio) / native_rate
            target_samples = int(duration * 16000)
            orig_indices = np.linspace(0, duration, len(audio))
            target_indices = np.linspace(0, duration, target_samples)
            audio = np.interp(target_indices, orig_indices, audio).astype(np.float32)
    except Exception as e:
        log.error("Failed to record audio for verification: %s", e)
        handle_auth_failure(loop, f"Mic recording error: {e}")
        return

    # Broadcast that we are transcribing
    asyncio.run_coroutine_threadsafe(
        broadcast({"event": "voice_auth_status", "status": "processing", "detail": "Verifying passphrase..."}),
        loop
    )

    try:
        segments, info = model.transcribe(audio, beam_size=5, initial_prompt="arcane, Hey Arcane")
        text = " ".join([seg.text for seg in segments]).strip().lower()
        log.info("\033[1;37m[Voice Auth]\033[0m Transcribed: '%s'", text)

        # Check if voice passphrase matches 'hey arcane' or 'arcane' (allowing phonetic matches)
        passphrase_matches = [
            "arcane", "our cane", "our-cane", "hey arcane",
            "hay arcane", "hey arca", "hey arc", "arcade"
        ]

        authenticated = any(phrase in text for phrase in passphrase_matches)
    except Exception as e:
        log.error("Whisper transcription failed: %s", e)
        authenticated = False
        text = "error"

    if authenticated:
        log.info("\033[1;32m[Voice Auth]\033[0m Authentication Successful!")
        with STATE_LOCK:
            CURRENT_STAGE = 3

        asyncio.run_coroutine_threadsafe(
            broadcast({"event": "voice_auth_status", "status": "success", "detail": "Access Granted! Welcoming sir..."}),
            loop
        )

        # Deploy workspace sequence
        threading.Thread(
            target=execute_workspace_deployment,
            args=(loop,),
            daemon=True,
        ).start()
    else:
        log.info("\033[1;31m[Voice Auth]\033[0m Access Denied: Phrase did not match.")
        handle_auth_failure(loop, f"Incorrect phrase: '{text}'" if text else "Silence / No match")


def handle_auth_failure(loop, reason):
    global CURRENT_STAGE
    with STATE_LOCK:
        CURRENT_STAGE = 0

    asyncio.run_coroutine_threadsafe(
        broadcast({"event": "voice_auth_status", "status": "failed", "detail": reason}),
        loop
    )

    # Vocal/Audio feedback alert
    if ARCANE_VOCAL_GREETING_ENABLED:
        play_access_denied()
    else:
        time.sleep(2.0)

    # Restart clap detection stream loop
    threading.Thread(target=audio_stream_loop, args=(loop,), daemon=True).start()


def voice_lock_verification_loop(loop):
    global CURRENT_STAGE, VOICE_MODEL
    log.info("\033[1;33m[Voice Lock]\033[0m Voice lock verification started.")

    # Ensure model is ready
    if VOICE_MODEL is None:
        asyncio.run_coroutine_threadsafe(
            broadcast({"event": "voice_lock_status", "status": "loading", "detail": "Initializing voice engine..."}),
            loop
        )
        preload_voice_model()
        if VOICE_MODEL is None:
            handle_lock_failure(loop, "Voice engine failed to load")
            return

    # Broadcast listening state
    asyncio.run_coroutine_threadsafe(
        broadcast({"event": "voice_lock_status", "status": "listening", "detail": "Say 'Lock' or 'Close' to confirm..."}),
        loop
    )

    duration_s = 3.5
    dev_idx = _find_wasapi_device()
    
    native_rate = 16000
    if dev_idx is not None:
        try:
            dev_info = sd.query_devices(dev_idx)
            native_rate = int(dev_info["default_samplerate"])
        except Exception:
            pass
    else:
        try:
            native_rate = int(sd.query_devices(kind='input')["default_samplerate"])
        except Exception:
            pass

    block_samples = int(native_rate * duration_s)
    log.info("\033[1;33m[Voice Lock]\033[0m Recording mic array for %s seconds at native %d Hz...", duration_s, native_rate)

    try:
        rec_kwargs = {
            "samplerate": native_rate,
            "channels": 1,
            "dtype": "float32"
        }
        if dev_idx is not None:
            rec_kwargs["device"] = dev_idx

        audio = sd.rec(block_samples, **rec_kwargs)
        sd.wait()
        audio = audio.flatten()
        
        if native_rate != 16000:
            duration = len(audio) / native_rate
            target_samples = int(duration * 16000)
            orig_indices = np.linspace(0, duration, len(audio))
            target_indices = np.linspace(0, duration, target_samples)
            audio = np.interp(target_indices, orig_indices, audio).astype(np.float32)
    except Exception as e:
        log.error("Failed to record audio for lock verification: %s", e)
        handle_lock_failure(loop, f"Recording error: {e}")
        return

    asyncio.run_coroutine_threadsafe(
        broadcast({"event": "voice_lock_status", "status": "processing", "detail": "Verifying command..."}),
        loop
    )

    try:
        segments, info = VOICE_MODEL.transcribe(audio, beam_size=5, initial_prompt="lock, close, reset, shutdown")
        text = " ".join([seg.text for seg in segments]).strip().lower()
        log.info("\033[1;37m[Voice Lock]\033[0m Transcribed: '%s'", text)

        lock_matches = ["lock", "close", "reset", "shut down", "shutdown", "last"]
        authenticated = any(phrase in text for phrase in lock_matches)
    except Exception as e:
        log.error("Whisper failed: %s", e)
        authenticated = False
        text = "error"

    if authenticated:
        log.info("\033[1;32m[Voice Lock]\033[0m Lock Confirmation Successful!")
        with STATE_LOCK:
            CURRENT_STAGE = 0

        asyncio.run_coroutine_threadsafe(
            broadcast({"event": "voice_lock_status", "status": "success", "detail": "Locking workspace..."}),
            loop
        )

        teardown_workspace()

        # Restart clap detector in standby
        threading.Thread(target=audio_stream_loop, args=(loop,), daemon=True).start()
    else:
        log.info("\033[1;31m[Voice Lock]\033[0m Lock Aborted: Phrase did not match.")
        handle_lock_failure(loop, f"Lock aborted: '{text}'" if text else "Aborted")


def handle_lock_failure(loop, reason):
    global CURRENT_STAGE
    with STATE_LOCK:
        CURRENT_STAGE = 3

    asyncio.run_coroutine_threadsafe(
        broadcast({"event": "voice_lock_status", "status": "failed", "detail": reason}),
        loop
    )
    
    time.sleep(2.0)
    # Restart clap detector in active workspace mode
    threading.Thread(target=audio_stream_loop, args=(loop,), daemon=True).start()


def teardown_workspace():
    log.info("\033[1;31m[⚡] Locking workspace. Terminating target processes...\033[0m")
    
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/f", "/im", "brave.exe"], capture_output=True, check=False)
            log.info("Closed Brave browser.")
        except Exception as e:
            log.warning("Brave close warning: %s", e)

        try:
            subprocess.run(["taskkill", "/f", "/im", "claude.exe"], capture_output=True, check=False)
            log.info("Closed Claude Desktop.")
        except Exception as e:
            log.warning("Claude close warning: %s", e)

        try:
            subprocess.run(["taskkill", "/f", "/im", "Antigravity IDE.exe"], capture_output=True, check=False)
            subprocess.run(["taskkill", "/f", "/im", "code.exe"], capture_output=True, check=False)
            log.info("Closed IDE instances.")
        except Exception as e:
            log.warning("IDE close warning: %s", e)

    # Confirmation announcement
    if ARCANE_VOCAL_GREETING_ENABLED:
        phrase = "Workspace locked. Standby mode armed."
        voice_id = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
        model_id = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
        out_fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
        
        if voice_id:
            cache_dir = BASE_DIR / ".cache" / "arcane_vocal"
            hash_key = f"{phrase}|{voice_id}|{model_id}|{out_fmt}".encode()
            cache_file = cache_dir / f"lock_{hashlib.sha256(hash_key).hexdigest()[:16]}.wav"
            
            if cache_file.is_file():
                try:
                    with wave.open(str(cache_file), "rb") as wf:
                        raw = wf.readframes(wf.getnframes())
                        rate = wf.getframerate()
                    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    sd.play(arr, rate)
                    sd.wait()
                    return
                except Exception:
                    pass
            
            api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
            if api_key:
                try:
                    from elevenlabs.client import ElevenLabs
                    client = ElevenLabs(api_key=api_key)
                    audio_stream = client.text_to_speech.convert(
                        voice_id=voice_id, text=phrase, model_id=model_id, output_format=out_fmt
                    )
                    raw_data = b"".join(audio_stream)
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    with wave.open(str(cache_file), "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(24000)
                        wf.writeframes(raw_data)
                    arr = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
                    sd.play(arr, 24000)
                    sd.wait()
                except Exception as e:
                    log.warning("Vocal lock play failed: %s", e)


def play_access_denied():
    phrase = "Access denied. Re-arming system."
    voice_id = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model_id = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    out_fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()

    if not voice_id:
        return

    cache_dir = BASE_DIR / ".cache" / "arcane_vocal"
    hash_key = f"{phrase}|{voice_id}|{model_id}|{out_fmt}".encode()
    cache_file = cache_dir / f"denied_{hashlib.sha256(hash_key).hexdigest()[:16]}.wav"

    if cache_file.is_file():
        try:
            with wave.open(str(cache_file), "rb") as wf:
                raw = wf.readframes(wf.getnframes())
                rate = wf.getframerate()
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            sd.play(arr, rate)
            sd.wait()
            return
        except Exception:
            pass

    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        return

    try:
        from elevenlabs.client import ElevenLabs
        client = ElevenLabs(api_key=api_key)
        audio_stream = client.text_to_speech.convert(
            voice_id=voice_id, text=phrase, model_id=model_id, output_format=out_fmt
        )
        raw_data = b"".join(audio_stream)
        cache_dir.mkdir(parents=True, exist_ok=True)
        with wave.open(str(cache_file), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(raw_data)
        arr = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
        sd.play(arr, 24000)
        sd.wait()
    except Exception as e:
        log.warning("ElevenLabs warning play failed: %s", e)


async def register(websocket):
    CONNECTED_CLIENTS.add(websocket)
    log.info(
        "UI client established websocket link. Active pools: %d",
        len(CONNECTED_CLIENTS),
    )
    try:
        await websocket.wait_closed()
    finally:
        CONNECTED_CLIENTS.discard(websocket)


async def broadcast(message_dict):
    if CONNECTED_CLIENTS:
        payload = json.dumps(message_dict)
        await asyncio.gather(
            *[client.send(payload) for client in list(CONNECTED_CLIENTS)],
            return_exceptions=True,
        )


def send_stage(loop, name, status, detail=""):
    try:
        asyncio.run_coroutine_threadsafe(
            broadcast({"event": "stage", "name": name, "status": status, "detail": detail}),
            loop
        )
    except RuntimeError:
        pass  # Event loop closed during shutdown


def audio_stream_loop(loop):
    global CURRENT_STAGE
    noise_floor = 1e-4
    last_logged_double = 0.0
    last_level_tx = 0.0
    first_clap_time = None
    spike_armed = True

    block_samples = max(int(SAMPLE_RATE * BLOCK_MS / 1000), 1)

    # WASAPI device selection for lowest-latency audio capture
    wasapi_dev = _find_wasapi_device()
    effective_rate = SAMPLE_RATE

    if wasapi_dev is not None:
        try:
            dev_info = sd.query_devices(wasapi_dev)
            # WASAPI devices often only support their native sample rate (usually 48000)
            # Using a mismatched rate causes PaErrorCode -9997 (Invalid sample rate)
            native_rate = int(dev_info["default_samplerate"])
            if native_rate != SAMPLE_RATE:
                log.info(
                    "\033[1;36m[WASAPI]\033[0m Device native rate is %d Hz (not %d), adapting",
                    native_rate, SAMPLE_RATE,
                )
                effective_rate = native_rate
                block_samples = max(int(effective_rate * BLOCK_MS / 1000), 1)
            log.info(
                "\033[1;36m[WASAPI]\033[0m Using low-latency device: %s @ %d Hz",
                dev_info["name"], effective_rate,
            )
        except Exception:
            log.info("\033[1;36m[WASAPI]\033[0m Using device index %d", wasapi_dev)
    else:
        log.info("WASAPI not available -- using default audio host API")

    stream_kwargs = {
        "samplerate": effective_rate,
        "channels": 1,
        "dtype": "float32",
        "blocksize": block_samples,
        "latency": "low",  # Request minimum buffer depth from PortAudio
    }
    if wasapi_dev is not None:
        stream_kwargs["device"] = wasapi_dev

    # Calibration gate: 2 seconds of silence to establish noise floor baseline
    calibration_deadline = time.monotonic() + 2.0
    log.info("Calibrating microphone noise floor baseline... Please remain quiet.")

    with sd.InputStream(**stream_kwargs) as stream:
        while True:
            data, _ = stream.read(block_samples)
            level = float(np.sqrt(np.mean(data**2))) if data.size > 0 else 0.0

            # Constantly adapt noise floor profile
            if level < (noise_floor * 2.2):
                noise_floor = 0.992 * noise_floor + 0.008 * level
                noise_floor = max(noise_floor, 1e-7)

            threshold = max(noise_floor * SPIKE_RATIO, MIN_RMS)
            now = time.monotonic()

            # Real-time Level Broadcast (throttled to 0.10s / 100ms)
            if (now - last_level_tx) >= 0.10:
                last_level_tx = now
                coro = broadcast({
                    "event": "level",
                    "rms": round(level, 5),
                    "noise_floor": round(noise_floor, 5),
                    "threshold": round(threshold, 5)
                })
                try:
                    asyncio.run_coroutine_threadsafe(coro, loop)
                except RuntimeError:
                    coro.close()  # prevent 'was never awaited' warning
                    return        # event loop closed, exit thread

            # Prevent triggers until the calibration gate closes
            if now < calibration_deadline:
                continue  # Skip processing triggers during the first 2 seconds

            # Once sequence has deployed (stage 2 or 4), skip all clap detection
            with STATE_LOCK:
                if CURRENT_STAGE == 2 or CURRENT_STAGE == 4:
                    continue

            if level < (threshold * 0.55):
                spike_armed = True

            if (
                spike_armed
                and level >= threshold
                and (now - last_logged_double) >= COOLDOWN_S
            ):
                spike_armed = False

                with STATE_LOCK:
                    if first_clap_time is None:
                        first_clap_time = now
                        if CURRENT_STAGE == 0:
                            CURRENT_STAGE = 1
                            log.info(
                                "\033[1;33m[1/2]\033[0m Signal 01 -> Elevating Dashboard Overlay Frame"
                            )
                            asyncio.run_coroutine_threadsafe(
                                broadcast({"event": "first_clap"}), loop
                            )
                        elif CURRENT_STAGE == 3:
                            log.info(
                                "\033[1;33m[1/2]\033[0m Signal 01 -> Locking Sequence Pre-Armed"
                            )
                            asyncio.run_coroutine_threadsafe(
                                broadcast({"event": "first_clap_lock"}), loop
                            )
                    else:
                        gap = now - first_clap_time
                        if MIN_DOUBLE_GAP_S <= gap <= MAX_DOUBLE_GAP_S:
                            if CURRENT_STAGE == 1:
                                CURRENT_STAGE = 2
                                log.info(
                                    "\033[1;32m[2/2]\033[0m Signal 02 -> Verified Double-Transient (gap=%.3fs). Starting Voice Verification.",
                                    gap,
                                )
                                asyncio.run_coroutine_threadsafe(
                                    broadcast({"event": "double_clap"}), loop
                                )
                                # Explicitly close the stream to release the mic handle before starting the next thread
                                stream.close()
                                threading.Thread(
                                    target=voice_verification_loop,
                                    args=(loop,),
                                    daemon=True,
                                ).start()
                                return # Exit audio stream loop to release the mic for Whisper
                            elif CURRENT_STAGE == 3:
                                CURRENT_STAGE = 4
                                log.info(
                                    "\033[1;32m[2/2]\033[0m Signal 02 -> Verified Double-Transient while open (gap=%.3fs). Starting Voice Lock Verification.",
                                    gap,
                                )
                                asyncio.run_coroutine_threadsafe(
                                    broadcast({"event": "double_clap_lock"}), loop
                                )
                                stream.close()
                                threading.Thread(
                                    target=voice_lock_verification_loop,
                                    args=(loop,),
                                    daemon=True,
                                ).start()
                                return
                            first_clap_time = None
                            last_logged_double = now
                        else:
                            first_clap_time = now
                            if CURRENT_STAGE == 0:
                                CURRENT_STAGE = 1
                                log.info(
                                    "\033[1;33m[1/2]\033[0m Signal 01 -> Elevating Dashboard Overlay Frame"
                                )
                                asyncio.run_coroutine_threadsafe(
                                    broadcast({"event": "first_clap"}), loop
                                )
                            elif CURRENT_STAGE == 3:
                                log.info(
                                    "\033[1;33m[1/2]\033[0m Signal 01 -> Locking Sequence Pre-Armed"
                                )
                                asyncio.run_coroutine_threadsafe(
                                    broadcast({"event": "first_clap_lock"}), loop
                                )


async def main():
    import websockets

    loop = asyncio.get_running_loop()

    # Pre-enumerate monitors at startup (cached for deployment)
    Win32WindowManager.get_monitor_bounds(1)

    # Start pre-loading Whisper model in background at startup
    threading.Thread(target=preload_voice_model, daemon=True).start()

    threading.Thread(target=audio_stream_loop, args=(loop,), daemon=True).start()

    log.info("Arcane Web Server running on ws://127.0.0.1:8765")

    # Automatically spawn layout template sheet directly into local space mapping
    if "--headless" not in sys.argv:
        ui_file = BASE_DIR / "index.html"
        if ui_file.is_file():
            webbrowser.open(f"file:///{ui_file}")

    async with websockets.serve(register, "127.0.0.1", 8765):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Core shutdown complete.")
