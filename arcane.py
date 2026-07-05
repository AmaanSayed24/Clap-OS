#!/usr/bin/env python3
"""
Arcane: Advanced Transient Audio Event Listener & Workspace Orchestrator
-----------------------------------------------------------------------
Listens for distinct double-transient spikes to instantly establish
a customized dark-mode development workspace environment.

Run with --calibrate first to auto-tune thresholds to your clap:
    python arcane.py --calibrate
"""

from __future__ import annotations

import os
import sys
import time
import wave
import json
import shutil
import asyncio
import hashlib
import logging
import tempfile
import threading
import subprocess
import webbrowser
from pathlib import Path

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

# --- Core Tuning Parameters --------------------------------------------------
# These are auto-overridden by --calibrate. You can also edit them manually
# after running calibration and seeing your clap's RMS printed to the terminal.

SAMPLE_RATE       = 44100
BLOCK_MS          = 30          # smaller blocks = faster reaction
CHANNELS          = 1

SPIKE_RATIO       = 6.0         # how many × above noise floor counts as a clap
                                 # lower  → more sensitive (catches soft claps)
                                 # higher → less sensitive (ignores ambient noise)

COOLDOWN_S        = 0.18        # minimum silence between any two detected spikes
MIN_DOUBLE_GAP_S  = 0.08        # minimum gap between clap 1 and clap 2
MAX_DOUBLE_GAP_S  = 0.65        # maximum gap — widened so natural claps fit
RETRIGGER_RATIO   = 0.40        # RMS must drop to this × threshold before re-arming
NOISE_FLOOR_ALPHA = 0.990       # how fast noise floor adapts (higher = slower)
MIN_RMS           = 0.008       # absolute floor — ignore anything quieter than this
QUIET_GATE_MULT   = 2.0         # noise floor only updates when level < floor × this

# --- Workspace Target Routing ------------------------------------------------
WORKSPACE_TRACK_URI      = "https://open.spotify.com/track/4iLqG9SeJSnt0cSPICSjxv"
WORKSPACE_TRACK_LABEL    = "Attention — Charlie Puth"
FOCUS_ACTIVE_VSCODE      = True
LAUNCH_NEW_VSCODE_WINDOW = False

LAUNCH_CLAUDE_WORKSPACE  = True
LAUNCH_BINANCE_WORKSPACE = True
BRAVE_FORCE_FULLSCREEN   = True
BRAVE_ISOLATED_SITE_PROFILES = False

CLAUDE_DISPLAY_MONITOR  = 1
BINANCE_DISPLAY_MONITOR = 3

# --- Vocal Synthesizer -------------------------------------------------------
ARCANE_VOCAL_GREETING_ENABLED = True
ARCANE_VOCAL_PHRASE = (
    "Welcome back sir. Workspace initialization complete. "
    "Congratulations on the new client for your SaaS app—make sure to follow up. "
    "If it helps: a short, specific note while the deal is still fresh usually "
    "anchors trust better than a polished deck sent cold a few days later."
)
ARCANE_VOCAL_DELAY_S       = 1.0
ARCANE_VOCAL_CACHE_ENABLED = True

# --- HUD Bridge --------------------------------------------------------------
HUD_ENABLED          = True
HUD_HOST             = "localhost"
HUD_PORT             = 8765
HUD_LEVEL_INTERVAL_S = 0.10

# --- Init --------------------------------------------------------------------
load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [\033[1;35mArcane\033[0m] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("arcane")


# =============================================================================
#  Calibration
# =============================================================================

def run_calibration() -> None:
    """
    Records 5 seconds of audio. Asks you to clap twice in that window.
    Prints the measured RMS values and suggests tuning constants you can
    paste into the top of this file.
    """
    DURATION   = 5
    BLOCK_SAMP = max(int(SAMPLE_RATE * BLOCK_MS / 1000), 1)

    print("\n\033[1;36m[Calibrate]\033[0m Recording for 5 seconds.")
    print("            Clap normally — like you would to trigger the sequence.")
    print("            Clap at least twice with a natural gap.\n")

    device_idx = find_optimal_input_device()
    blocks: list[float] = []
    peaks:  list[float] = []
    noise_floor = 1e-4
    spike_candidates: list[float] = []
    spike_armed = True

    start = time.monotonic()
    with sd.InputStream(
        device=device_idx,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=BLOCK_SAMP,
    ) as stream:
        while (time.monotonic() - start) < DURATION:
            elapsed = DURATION - (time.monotonic() - start)
            print(f"\r  {elapsed:.1f}s remaining…", end="", flush=True)

            data, _ = stream.read(BLOCK_SAMP)
            mono  = np.mean(data.astype(np.float64), axis=1) if data.ndim > 1 else data.astype(np.float64)
            level = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
            blocks.append(level)

            # update noise floor (quiet moments only)
            if level < noise_floor * 2.5:
                noise_floor = 0.990 * noise_floor + 0.010 * level
                noise_floor = max(noise_floor, 1e-7)

            threshold = max(noise_floor * 6.0, 0.008)

            if level < threshold * 0.40:
                spike_armed = True
            if spike_armed and level >= threshold:
                spike_armed = False
                spike_candidates.append(level)
                peaks.append(level)

    print("\r  Done.                    \n")

    ambient_rms = np.percentile(blocks, 30)   # 30th percentile ≈ background noise
    if not peaks:
        print("\033[1;31m[!]\033[0m No claps detected. Try clapping louder or closer to the mic.")
        print(f"    Ambient RMS was: {ambient_rms:.5f}")
        print(f"    Try lowering MIN_RMS below {ambient_rms*8:.4f} and SPIKE_RATIO to 4.0\n")
        return

    avg_clap_rms = float(np.mean(peaks))
    min_clap_rms = float(np.min(peaks))

    # suggested ratio: use 55% of the weakest clap ÷ ambient, capped at 4–9
    ratio = max(4.0, min(9.0, (min_clap_rms / max(ambient_rms, 1e-6)) * 0.55))
    suggested_min_rms = round(ambient_rms * 2.5, 5)

    print("─" * 56)
    print(f"  Ambient noise RMS  : {ambient_rms:.5f}")
    print(f"  Clap RMS (avg)     : {avg_clap_rms:.5f}")
    print(f"  Clap RMS (weakest) : {min_clap_rms:.5f}")
    print(f"  Claps detected     : {len(peaks)}")
    print("─" * 56)
    print("\n\033[1;32m[Suggested constants — paste into arcane.py]\033[0m\n")
    print(f"  SPIKE_RATIO  = {ratio:.1f}")
    print(f"  MIN_RMS      = {suggested_min_rms}")
    print(f"  COOLDOWN_S   = 0.18")
    print(f"  MAX_DOUBLE_GAP_S = 0.65")
    print()

    if len(peaks) >= 2:
        print("\033[1;32m[✓]\033[0m Claps detected — settings above should work well for you.")
    else:
        print("\033[1;33m[!]\033[0m Only one clap detected. Try again or clap louder.")
    print()


# =============================================================================
#  HUD Bridge
# =============================================================================

class ArcaneHUDBridge:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set = set()
        self._ready   = threading.Event()
        self._active  = False

    def start(self) -> None:
        if not HUD_ENABLED:
            return
        try:
            import websockets  # noqa: F401
        except ImportError:
            log.warning("HUD bridge disabled — pip install websockets")
            return
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(timeout=3)

    def send(self, payload: dict) -> None:
        if not self._active or not self._loop:
            return
        msg = json.dumps(payload)

        async def _blast():
            dead = []
            for ws in list(self._clients):
                try:    await ws.send(msg)
                except: dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)

        try:
            asyncio.run_coroutine_threadsafe(_blast(), self._loop)
        except Exception:
            pass

    def stage(self, name: str, status: str, detail: str = "") -> None:
        self.send({"event": "stage", "name": name, "status": status, "detail": detail})

    def _run(self) -> None:
        import websockets

        async def _handler(ws):
            self._clients.add(ws)
            try:
                async for _ in ws:
                    pass
            finally:
                self._clients.discard(ws)

        async def _serve():
            async with websockets.serve(_handler, HUD_HOST, HUD_PORT):
                log.info("\033[1;36m[HUD]\033[0m bridge live → ws://%s:%s", HUD_HOST, HUD_PORT)
                self._active = True
                self._ready.set()
                await asyncio.Future()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(_serve())
        except Exception as err:
            log.warning("HUD bridge error: %s", err)
        finally:
            self._ready.set()


HUD = ArcaneHUDBridge()


# =============================================================================
#  Win32 Window Manager
# =============================================================================

class Win32WindowManager:
    @staticmethod
    def get_sorted_monitors() -> list[tuple[int, int, int, int]]:
        if sys.platform != "win32":
            return []
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        rects: list[tuple[int, int, int, int]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                            ctypes.POINTER(RECT), wintypes.LPARAM)
        def cb(_hm, _hdc, lprc, _lp):
            r = lprc.contents
            rects.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
            return True

        ctypes.windll.user32.EnumDisplayMonitors(None, None, cb, 0)
        rects.sort(key=lambda x: (x[0], x[1]))
        return rects

    @classmethod
    def get_monitor_bounds(cls, index: int) -> tuple[int, int, int, int]:
        monitors = cls.get_sorted_monitors()
        if not monitors:
            return (0, 0, 1920, 1080)
        return monitors[max(0, min(index - 1, len(monitors) - 1))]


# =============================================================================
#  Vocalizer
# =============================================================================

class ArcaneVocalizer:
    @classmethod
    def synthesize_and_play(cls) -> None:
        if not ARCANE_VOCAL_GREETING_ENABLED or not ARCANE_VOCAL_PHRASE.strip():
            return
        phrase   = ARCANE_VOCAL_PHRASE.strip()
        voice_id = (os.environ.get("ELEVENLABS_VOICE_ID")     or "").strip()
        model_id = (os.environ.get("ELEVENLABS_MODEL_ID")     or "eleven_multilingual_v2").strip()
        out_fmt  = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()

        if not voice_id:
            log.warning("ELEVENLABS_VOICE_ID not set — vocal greeting skipped.")
            HUD.stage("vocal", "error", "missing voice id")
            return

        effective_rate: int | None = None
        if out_fmt.startswith("pcm_"):
            try:
                effective_rate = int(out_fmt.split("_", 1)[1])
            except ValueError:
                HUD.stage("vocal", "error", "bad format"); return
        if effective_rate is None:
            # Non-PCM formats (mp3, etc.) can't be played as raw 16-bit via sounddevice
            HUD.stage("vocal", "error", "only pcm formats supported"); return
        if effective_rate not in {16000, 22050, 24000}:
            HUD.stage("vocal", "error", "unsupported sample rate"); return

        cache_dir  = Path(__file__).resolve().parent / ".cache" / "arcane_vocal"
        hash_key   = f"{phrase}|{voice_id}|{model_id}|{out_fmt}".encode()
        cache_file = cache_dir / f"{hashlib.sha256(hash_key).hexdigest()[:24]}.wav"

        if ARCANE_VOCAL_CACHE_ENABLED and cache_file.is_file():
            if cls._play_file(cache_file):
                HUD.stage("vocal", "done", "played from cache"); return

        api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
        if not api_key:
            HUD.stage("vocal", "error", "missing api key"); return

        try:
            from elevenlabs.client import ElevenLabs
            client = ElevenLabs(api_key=api_key)
            raw_data = b"".join(client.text_to_speech.convert(
                voice_id=voice_id, text=phrase,
                model_id=model_id, output_format=out_fmt,
            ))
        except Exception as err:
            log.warning("ElevenLabs error: %s", err)
            HUD.stage("vocal", "error", str(err)[:72]); return

        if ARCANE_VOCAL_CACHE_ENABLED and raw_data:
            cls._save_file(cache_file, raw_data, effective_rate)
        cls._play_raw(raw_data, effective_rate)
        HUD.stage("vocal", "done", "greeting delivered")

    @staticmethod
    def _play_file(path: Path) -> bool:
        try:
            with wave.open(str(path), "rb") as wf:
                if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                    return False
                raw, rate = wf.readframes(wf.getnframes()), wf.getframerate()
            sd.play(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, rate)
            sd.wait(); return True
        except Exception as err:
            log.warning("Cache playback failed: %s", err); return False

    @staticmethod
    def _save_file(path: Path, data: bytes, rate: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            with wave.open(str(tmp), "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2)
                wf.setframerate(rate); wf.writeframes(data)
            tmp.replace(path)
        except OSError:
            if tmp.is_file(): tmp.unlink()

    @staticmethod
    def _play_raw(data: bytes, rate: int) -> None:
        try:
            sd.play(np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0, rate)
            sd.wait()
        except Exception as err:
            log.warning("Audio playback error: %s", err)


# =============================================================================
#  Workspace launchers
# =============================================================================

def spawn_brave_instance(url: str, monitor: int, label: str) -> None:
    if not url.strip():
        return
    brave_path = None
    if sys.platform == "win32":
        for env_var in ["ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"]:
            base = os.environ.get(env_var, "")
            if base:
                c = os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe")
                if os.path.isfile(c):
                    brave_path = c; break
    else:
        brave_path = shutil.which("brave-browser") or shutil.which("brave")

    if not brave_path:
        log.warning("Brave not found — opening in default browser.")
        webbrowser.open(url); return

    cmd = [brave_path]
    if BRAVE_ISOLATED_SITE_PROFILES:
        p = Path(tempfile.gettempdir()) / "arcane-brave-profiles" / label.lower()
        p.mkdir(parents=True, exist_ok=True)
        cmd += [f"--user-data-dir={p}", "--no-first-run"]
    cmd.append("--new-window")
    if sys.platform == "win32":
        l, t, r, b = Win32WindowManager.get_monitor_bounds(monitor)
        cmd.append(f"--window-position={l},{t}")
        if BRAVE_FORCE_FULLSCREEN:
            cmd += [f"--window-size={r-l},{b-t}", "--start-fullscreen"]
    elif BRAVE_FORCE_FULLSCREEN:
        cmd.append("--start-fullscreen")
    cmd.append(url)
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(cmd, **kw)


def focus_or_launch_vscode() -> None:
    if not FOCUS_ACTIVE_VSCODE and not LAUNCH_NEW_VSCODE_WINDOW:
        return
    exe = None
    if sys.platform == "win32":
        for p in [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "Code.exe"),
            os.path.join(os.environ.get("ProgramFiles",  ""), "Microsoft VS Code", "Code.exe"),
        ]:
            if os.path.isfile(p): exe = p; break
    else:
        exe = shutil.which("code")
    if not exe:
        log.warning("VS Code binary not found.")
        HUD.stage("vscode", "error", "binary not found"); return
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    if FOCUS_ACTIVE_VSCODE:      subprocess.Popen([exe],       **kw)
    if LAUNCH_NEW_VSCODE_WINDOW: subprocess.Popen([exe, "-n"], **kw)


# =============================================================================
#  Sequence
# =============================================================================

def execute_arcane_sequence() -> None:
    log.info("\033[1;32m[⚡] Transients verified. Awakening environment…\033[0m")
    HUD.send({"event": "sequence_start"})

    if WORKSPACE_TRACK_URI.strip():
        HUD.stage("spotify", "active")
        try:
            os.startfile(WORKSPACE_TRACK_URI.strip()) if sys.platform == "win32" else webbrowser.open(WORKSPACE_TRACK_URI.strip())
            HUD.stage("spotify", "done", WORKSPACE_TRACK_LABEL)
        except Exception:
            log.warning("Spotify routing failed."); HUD.stage("spotify", "error")

    if LAUNCH_CLAUDE_WORKSPACE:
        HUD.stage("claude", "active")
        try:
            spawn_brave_instance(os.environ.get("CLAUDE_CODE_URL", "https://claude.ai/new"), CLAUDE_DISPLAY_MONITOR, "Claude")
            HUD.stage("claude", "done", f"Monitor {CLAUDE_DISPLAY_MONITOR}")
        except Exception:
            log.warning("Claude workspace failed."); HUD.stage("claude", "error")

    if LAUNCH_BINANCE_WORKSPACE:
        HUD.stage("binance", "active")
        try:
            spawn_brave_instance(os.environ.get("BINANCE_BTC_URL", "https://www.binance.com/en/trade/BTC_USDT"), BINANCE_DISPLAY_MONITOR, "Binance")
            HUD.stage("binance", "done", f"Monitor {BINANCE_DISPLAY_MONITOR}")
        except Exception:
            log.warning("Binance workspace failed."); HUD.stage("binance", "error")

    if ARCANE_VOCAL_GREETING_ENABLED:
        HUD.stage("vocal", "active")
        delay = max(0.0, ARCANE_VOCAL_DELAY_S)
        if delay: time.sleep(delay)
        try:
            threading.Thread(target=ArcaneVocalizer.synthesize_and_play, daemon=True).start()
        except Exception:
            log.warning("Vocal thread failed."); HUD.stage("vocal", "error")

    HUD.stage("vscode", "active")
    try:
        focus_or_launch_vscode()
        HUD.stage("vscode", "done")
    except Exception:
        log.warning("VS Code failed."); HUD.stage("vscode", "error")

    HUD.send({"event": "sequence_done"})


# =============================================================================
#  Input device
# =============================================================================

def find_optimal_input_device() -> int:
    override = (os.environ.get("ARCANE_INPUT_DEVICE") or "").strip()
    if override:
        if override.isdigit(): return int(override)
        for idx, dev in enumerate(sd.query_devices()):
            if override.lower() in dev["name"].lower() and dev["max_input_channels"] >= 1:
                return idx
    default = sd.default.device[0]
    if default is not None and default >= 0: return default
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] >= 1: return idx
    return 0


# =============================================================================
#  Main detection loop
# =============================================================================

def main(on_detection: callable | None = None) -> int:
    block_samples      = max(int(SAMPLE_RATE * BLOCK_MS / 1000), 1)
    noise_floor        = 1e-4
    last_logged_double = 0.0
    last_level_tx      = 0.0
    first_clap_time: float | None = None
    spike_armed        = True
    sequence_executed  = False

    HUD.start()
    input_device_idx = find_optimal_input_device()

    # Print device info so user can verify the right mic is active
    try:
        dev_info = sd.query_devices(input_device_idx)
        log.info("Mic: \033[1;37m%s\033[0m", dev_info["name"])
    except Exception:
        pass

    log.info(
        "Listening — SPIKE_RATIO=%.1f  MIN_RMS=%.4f  gap=%.2f–%.2fs",
        SPIKE_RATIO, MIN_RMS, MIN_DOUBLE_GAP_S, MAX_DOUBLE_GAP_S
    )
    log.info("Clap twice to trigger. Ctrl+C to quit.")

    try:
        with sd.InputStream(
            device=input_device_idx,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=block_samples,
        ) as stream:
            while True:
                data, overflow = stream.read(block_samples)
                if overflow:
                    log.warning("Stream overflow — block dropped.")
                    continue

                mono  = np.mean(data.astype(np.float64), axis=1) if data.ndim > 1 else data.astype(np.float64)
                level = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0

                # adaptive noise floor — only update during quiet moments
                if level < (noise_floor * QUIET_GATE_MULT):
                    noise_floor = NOISE_FLOOR_ALPHA * noise_floor + (1.0 - NOISE_FLOOR_ALPHA) * level
                    noise_floor = max(noise_floor, 1e-7)

                threshold = max(noise_floor * SPIKE_RATIO, MIN_RMS)
                now = time.monotonic()

                # HUD level broadcast (throttled)
                if (now - last_level_tx) >= HUD_LEVEL_INTERVAL_S:
                    last_level_tx = now
                    HUD.send({"event": "level", "rms": round(level, 5),
                              "noise_floor": round(noise_floor, 5),
                              "threshold":   round(threshold,   5)})

                # re-arm after signal drops back below hysteresis threshold
                if level < (threshold * RETRIGGER_RATIO):
                    spike_armed = True

                if spike_armed and level >= threshold and (now - last_logged_double) >= COOLDOWN_S:
                    spike_armed = False
                    log.debug("Spike RMS=%.4f threshold=%.4f", level, threshold)

                    if first_clap_time is None:
                        first_clap_time = now
                        log.info("\033[1;33m[1/2]\033[0m First clap — clap again within %.2fs", MAX_DOUBLE_GAP_S)
                        HUD.send({"event": "first_clap"})
                        if on_detection: on_detection("first_clap")

                    else:
                        gap = now - first_clap_time
                        if MIN_DOUBLE_GAP_S <= gap <= MAX_DOUBLE_GAP_S:
                            first_clap_time    = None
                            last_logged_double = now
                            log.info("\033[1;32m[2/2]\033[0m Double-clap confirmed (gap=%.3fs) 🚀", gap)
                            HUD.send({"event": "double_clap"})
                            if on_detection: on_detection("double_clap")
                            if not sequence_executed:
                                sequence_executed = True
                                threading.Thread(target=execute_arcane_sequence, daemon=True).start()
                        else:
                            # gap out of window — treat this spike as a new first clap
                            log.info("\033[1;33m[!]\033[0m Gap %.3fs out of window (%.2f–%.2f) — resetting", gap, MIN_DOUBLE_GAP_S, MAX_DOUBLE_GAP_S)
                            first_clap_time = now
                            HUD.send({"event": "first_clap"})

    except KeyboardInterrupt:
        log.info("Arcane safely shut down.")
        return 0
    except Exception as err:
        log.error("Fatal error: %s", err)
        return 1


# =============================================================================
#  Entry point
# =============================================================================

if __name__ == "__main__":
    if "--calibrate" in sys.argv:
        run_calibration()
    else:
        sys.exit(main())