#!/usr/bin/env python3
"""
Arcane Backend Server v2.0
--------------------------
Handles non-looping dual-stage audio transient routing and native Win32 window positioning.
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
import tempfile
import shutil
from pathlib import Path

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

# Initialize path vectors
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
LAUNCH_CLAUDE_WORKSPACE = True
LAUNCH_BINANCE_WORKSPACE = True
BRAVE_FORCE_FULLSCREEN = True

CLAUDE_DISPLAY_MONITOR = 1
BINANCE_DISPLAY_MONITOR = 3

ARCANE_VOCAL_GREETING_ENABLED = True
ARCANE_VOCAL_PHRASE = (
    "Welcome back sir. Workspace initialization complete. "
    "Congratulations on the new client for your SaaS app—make sure to follow up. "
    "If it helps: a short, specific note while the deal is still fresh usually "
    "anchors trust better than a polished deck sent cold a few days later."
)

# Global State Vectors (State Machine Protection)
CONNECTED_CLIENTS = set()
CURRENT_STAGE = 0  # 0 = Headless, 1 = UI Visible, 2 = Active Environment deployed
STATE_LOCK = threading.Lock()


class Win32WindowManager:
    @staticmethod
    def get_monitor_bounds(index: int) -> tuple[int, int, int, int]:
        if sys.platform != "win32":
            return (0, 0, 1920, 1080)
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
        adjusted_idx = max(0, min(index - 1, len(rects) - 1))
        return rects[adjusted_idx] if rects else (0, 0, 1920, 1080)


class ArcaneVocalizer:
    @classmethod
    def synthesize_and_play(cls) -> None:
        if not ARCANE_VOCAL_GREETING_ENABLED or not ARCANE_VOCAL_PHRASE.strip():
            return
        phrase = ARCANE_VOCAL_PHRASE.strip()
        voice_id = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
        model_id = (
            os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2"
        ).strip()
        out_fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()

        if not voice_id:
            print("[Vocalizer] Warning: ELEVENLABS_VOICE_ID missing.", flush=True)
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
                return
            except Exception as e:
                print(f"[Vocalizer] Cache read fail: {e}", flush=True)

        api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
        if not api_key:
            print("[Vocalizer] Warning: ELEVENLABS_API_KEY missing.", flush=True)
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
            print(f"[Vocalizer] ElevenLabs live fetch failed: {e}", flush=True)


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
        print(
            f"[Automation] Brave execution path trace failed. Reverting to browser fallback for {label}",
            flush=True,
        )
        webbrowser.open(url)
        return

    cmd = [brave_path, "--new-window"]
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
        print(
            f"[Automation] Launched {label} in Brave window on display node #{monitor}",
            flush=True,
        )
    except Exception as e:
        print(f"[Automation] Failed to spin up Brave instance: {e}", flush=True)


def focus_or_launch_vscode() -> None:
    program_files = os.environ.get("ProgramFiles", "")
    local_app = os.environ.get("LOCALAPPDATA", "")
    exe_target = None
    if sys.platform == "win32":
        paths = [
            os.path.join(local_app, "Programs", "Microsoft VS Code", "Code.exe"),
            os.path.join(program_files, "Microsoft VS Code", "Code.exe"),
        ]
        for p in paths:
            if os.path.isfile(p):
                exe_target = p
                break
    else:
        exe_target = shutil.which("code")

    if not exe_target:
        print("[Automation] Warning: Code.exe not found on localized disk.", flush=True)
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
        print("[Automation] VS Code core environment initialized.", flush=True)
    except Exception as e:
        print(f"[Automation] Code environment launch failed: {e}", flush=True)


def execute_workspace_deployment():
    """Unrolls the deployment sequence exactly once with isolated handlers."""
    print("[Orchestrator] Executing target pipeline sequence...", flush=True)

    # 1. Spotify Protocol
    if WORKSPACE_TRACK_URI.strip():
        try:
            if sys.platform == "win32":
                os.startfile(WORKSPACE_TRACK_URI.strip())
            else:
                webbrowser.open(WORKSPACE_TRACK_URI.strip())
            print(
                "[Automation] Media route injected -> Playing Attention by Charlie Puth",
                flush=True,
            )
        except Exception as e:
            print(f"[Automation] Spotify launch error: {e}", flush=True)

    # 2. Brave Mapping Matrices
    if LAUNCH_CLAUDE_WORKSPACE:
        url = os.environ.get("CLAUDE_CODE_URL", "https://claude.ai/new")
        spawn_brave_instance(url, CLAUDE_DISPLAY_MONITOR, "Claude Workspace")

    if LAUNCH_BINANCE_WORKSPACE:
        url = os.environ.get(
            "BINANCE_BTC_URL", "https://www.binance.com/en/trade/BTC_USDT"
        )
        spawn_brave_instance(url, BINANCE_DISPLAY_MONITOR, "Binance Analytics")

    # 3. Core Workspace Sync
    focus_or_launch_vscode()

    # 4. Background Vocal Synthesis Thread
    threading.Thread(target=ArcaneVocalizer.synthesize_and_play, daemon=True).start()


async def register(websocket):
    CONNECTED_CLIENTS.add(websocket)
    print(
        f"[Server] UI client established websocket socket link. Active pools: {len(CONNECTED_CLIENTS)}",
        flush=True,
    )
    try:
        await websocket.wait_closed()
    finally:
        CONNECTED_CLIENTS.remove(websocket)


async def broadcast(message_dict):
    if CONNECTED_CLIENTS:
        payload = json.dumps(message_dict)
        await asyncio.gather(
            *[client.send(payload) for client in CONNECTED_CLIENTS],
            return_exceptions=True,
        )


def audio_stream_loop(loop):
    global CURRENT_STAGE
    noise_floor = 1e-4
    last_logged_double = 0.0
    first_clap_time = None
    spike_armed = True

    block_samples = max(int(SAMPLE_RATE * BLOCK_MS / 1000), 1)

    # SYSTEM UPGRADE: Set a timestamp for when the calibration ends (2 seconds from boot)
    calibration_deadline = time.monotonic() + 2.0
    print(
        "[Audio Core] Calibrating microphone noise floor baseline... Please remain quiet.",
        flush=True,
    )

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=block_samples
    ) as stream:
        while True:
            data, _ = stream.read(block_samples)
            level = float(np.sqrt(np.mean(data**2))) if data.size > 0 else 0.0

            # Constantly adapt noise floor profile
            if level < (noise_floor * 2.2):
                noise_floor = 0.992 * noise_floor + 0.008 * level
                noise_floor = max(noise_floor, 1e-7)

            threshold = max(noise_floor * SPIKE_RATIO, MIN_RMS)
            now = time.monotonic()

            # SYSTEM UPGRADE: Prevent triggers until the calibration gate closes
            if now < calibration_deadline:
                continue  # Skip processing triggers during the first 2 seconds

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
                            print(
                                "\n[Engine] Signal 01 -> Elevating Dashboard Overlay Frame",
                                flush=True,
                            )
                            asyncio.run_coroutine_threadsafe(
                                broadcast({"event": "CLAP_1"}), loop
                            )
                    else:
                        gap = now - first_clap_time
                        if MIN_DOUBLE_GAP_S <= gap <= MAX_DOUBLE_GAP_S:
                            if CURRENT_STAGE == 1:
                                CURRENT_STAGE = 2
                                print(
                                    "[Engine] Signal 02 -> Verified Double-Transient. Unrolling environment.",
                                    flush=True,
                                )
                                asyncio.run_coroutine_threadsafe(
                                    broadcast({"event": "CLAP_2"}), loop
                                )
                                execute_workspace_deployment()
                            first_clap_time = None
                            last_logged_double = now
                        else:
                            first_clap_time = now
                            if CURRENT_STAGE == 0:
                                CURRENT_STAGE = 1
                                print(
                                    "\n[Engine] Signal 01 -> Elevating Dashboard Overlay Frame",
                                    flush=True,
                                )
                                asyncio.run_coroutine_threadsafe(
                                    broadcast({"event": "CLAP_1"}), loop
                                )


async def main():
    import websockets

    loop = asyncio.get_running_loop()
    threading.Thread(target=audio_stream_loop, args=(loop,), daemon=True).start()

    print("Arcane Web Server running on ws://localhost:8765")

    # Automatically spawn layout template sheet directly into local space mapping
    ui_file = BASE_DIR / "index.html"
    if ui_file.is_file():
        webbrowser.open(f"file:///{ui_file}")

    async with websockets.serve(register, "localhost", 8765):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Server] Core shutdown complete.")
