"""
TranslateAI — Live Japanese → English captions for macOS.

Listens to your microphone, streams the audio to the Gemini Live API
(live-translate model), and shows the English translation as live captions
in a simple desktop window.

Run:
    python app.py

Requires GEMINI_API_KEY in your environment (or paste it into the window).
"""

import os
import queue
import asyncio
import threading

import tkinter as tk
from tkinter import ttk, scrolledtext

import pyaudio
import numpy as np

from google import genai
from google.genai import types


# --- Audio / model configuration -------------------------------------------

FORMAT = pyaudio.paInt16
CHANNELS = 1
SEND_SAMPLE_RATE = 16000      # Gemini expects 16 kHz PCM input
RECEIVE_SAMPLE_RATE = 24000   # Gemini sends back 24 kHz PCM audio
CHUNK_SIZE = 1024

MODEL = "models/gemini-3.5-live-translate-preview"
TARGET_LANGUAGE = "en"        # translate everything we hear into English

pya = pyaudio.PyAudio()


def build_config(play_audio: bool) -> types.LiveConnectConfig:
    """Live API config: speak audio in, get English back.

    We always request AUDIO (what the live-translate model is built for) plus
    output transcription, which gives us the English text for the captions.
    """
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=TARGET_LANGUAGE,
        ),
        # Transcribe the model's English audio so we can render it as text.
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


# --- Backend: streams mic -> Gemini -> caption events -----------------------

class Translator:
    """Runs the Gemini Live session on its own asyncio thread.

    UI updates are pushed as ("status"|"caption"|"turn_end"|"error", payload)
    tuples onto a thread-safe queue that the Tk main loop drains.
    """

    def __init__(self, api_key, events, mic_index=None, play_audio=False):
        self.api_key = api_key
        self.events = events
        self.mic_index = mic_index
        self.play_audio = play_audio

        self._thread = None
        self._loop = None
        self._stop_event = None

        self.session = None
        self.out_queue = None
        self.audio_in_queue = None
        self.audio_in_stream = None
        self.audio_out_stream = None

    # -- lifecycle (called from the Tk thread) --

    def start(self):
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self):
        loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None:
            loop.call_soon_threadsafe(stop_event.set)

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception as exc:  # belt-and-suspenders
            self._emit("error", str(exc))

    # -- helpers --

    def _emit(self, kind, payload=None):
        self.events.put((kind, payload))

    # -- async tasks --

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._emit("status", "Connecting…")

        client = genai.Client(
            http_options={"api_version": "v1beta"},
            api_key=self.api_key,
        )
        config = build_config(self.play_audio)

        try:
            async with (
                client.aio.live.connect(model=MODEL, config=config) as session,
                asyncio.TaskGroup() as tg,
            ):
                self.session = session
                self.out_queue = asyncio.Queue(maxsize=20)
                self.audio_in_queue = asyncio.Queue()

                tg.create_task(self._listen_audio())
                tg.create_task(self._send_realtime())
                tg.create_task(self._receive())
                if self.play_audio:
                    tg.create_task(self._play_audio())

                self._emit("status", "Listening…")
                await self._stop_event.wait()
                raise asyncio.CancelledError()  # tear down the TaskGroup
        except asyncio.CancelledError:
            pass
        except BaseExceptionGroup as eg:  # noqa: F821 (builtin on 3.11+)
            self._emit("error", "; ".join(str(e) for e in eg.exceptions))
        except Exception as exc:
            self._emit("error", str(exc))
        finally:
            self._cleanup()
            self._emit("status", "Stopped")

    async def _listen_audio(self):
        if self.mic_index is None:
            self.mic_index = pya.get_default_input_device_info()["index"]

        info = pya.get_device_info_by_index(self.mic_index)
        # Try the format Gemini wants directly; many mics support it. Virtual
        # devices (e.g. BlackHole) usually don't, so fall back to their native
        # rate/channels and resample to 16 kHz mono before sending.
        self._cap_rate = SEND_SAMPLE_RATE
        self._cap_channels = CHANNELS
        try:
            self.audio_in_stream = await asyncio.to_thread(
                pya.open,
                format=FORMAT, channels=CHANNELS, rate=SEND_SAMPLE_RATE,
                input=True, input_device_index=self.mic_index,
                frames_per_buffer=CHUNK_SIZE,
            )
        except Exception:
            self._cap_rate = int(info.get("defaultSampleRate", 48000)) or 48000
            self._cap_channels = min(2, int(info.get("maxInputChannels", 1))) or 1
            self.audio_in_stream = await asyncio.to_thread(
                pya.open,
                format=FORMAT, channels=self._cap_channels, rate=self._cap_rate,
                input=True, input_device_index=self.mic_index,
                frames_per_buffer=CHUNK_SIZE,
            )

        needs_convert = (
            self._cap_rate != SEND_SAMPLE_RATE or self._cap_channels != CHANNELS
        )
        while not self._stop_event.is_set():
            data = await asyncio.to_thread(
                self.audio_in_stream.read, CHUNK_SIZE, exception_on_overflow=False
            )
            if needs_convert:
                data = self._to_16k_mono(data, self._cap_rate, self._cap_channels)
            await self.out_queue.put({"data": data, "mime_type": "audio/pcm"})

    @staticmethod
    def _to_16k_mono(data, src_rate, channels):
        """Downmix to mono and resample to 16 kHz int16 PCM."""
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if src_rate != SEND_SAMPLE_RATE and samples.size:
            n_out = int(round(samples.size * SEND_SAMPLE_RATE / src_rate))
            if n_out > 0:
                x_old = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
                x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
                samples = np.interp(x_new, x_old, samples)
        return np.clip(samples, -32768, 32767).astype("<i2").tobytes()

    async def _send_realtime(self):
        while not self._stop_event.is_set():
            msg = await self.out_queue.get()
            if self.session is not None:
                await self.session.send(input=msg)

    async def _receive(self):
        while not self._stop_event.is_set():
            turn = self.session.receive()
            async for response in turn:
                sc = response.server_content
                if sc is not None:
                    transcript = sc.output_transcription
                    if transcript is not None and transcript.text:
                        self._emit("caption", transcript.text)
                    if sc.turn_complete:
                        self._emit("turn_end")
                # Fallback for TEXT-modality responses.
                if response.text:
                    self._emit("caption", response.text)
                if self.play_audio and response.data:
                    self.audio_in_queue.put_nowait(response.data)

            # Drop any audio buffered past an interruption.
            if self.audio_in_queue is not None:
                while not self.audio_in_queue.empty():
                    self.audio_in_queue.get_nowait()

    async def _play_audio(self):
        self.audio_out_stream = await asyncio.to_thread(
            pya.open,
            format=FORMAT,
            channels=CHANNELS,
            rate=RECEIVE_SAMPLE_RATE,
            output=True,
        )
        while not self._stop_event.is_set():
            chunk = await self.audio_in_queue.get()
            await asyncio.to_thread(self.audio_out_stream.write, chunk)

    def _cleanup(self):
        for stream in (self.audio_in_stream, self.audio_out_stream):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        self.audio_in_stream = None
        self.audio_out_stream = None


# --- UI ---------------------------------------------------------------------

BG = "#0f1115"
PANEL = "#171a21"
ACCENT = "#4f8cff"
TEXT = "#e7e9ee"
MUTED = "#8a91a0"


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.translator = None
        self.running = False
        self._needs_paragraph = False

        root.title("TranslateAI — 日本語 → English")
        root.configure(bg=BG)
        root.geometry("760x560")
        root.minsize(560, 420)

        self._build_ui()
        self._refresh_mics()
        self.root.after(60, self._drain_events)

    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=20, pady=(18, 8))

        tk.Label(
            header, text="Live Japanese → English",
            bg=BG, fg=TEXT, font=("Helvetica Neue", 20, "bold"),
        ).pack(side="left")

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = tk.Label(
            header, textvariable=self.status_var,
            bg=BG, fg=MUTED, font=("Helvetica Neue", 12),
        )
        self.status_label.pack(side="right")

        # Controls
        controls = tk.Frame(self.root, bg=BG)
        controls.pack(fill="x", padx=20, pady=(4, 8))

        tk.Label(controls, text="Mic", bg=BG, fg=MUTED,
                 font=("Helvetica Neue", 11)).pack(side="left")
        self.mic_var = tk.StringVar()
        self.mic_menu = ttk.Combobox(
            controls, textvariable=self.mic_var, state="readonly", width=34
        )
        self.mic_menu.pack(side="left", padx=(8, 8))

        self.meet_btn = tk.Button(
            controls, text="🎧 Meet", command=self._select_meet_audio,
            bg=PANEL, fg=TEXT, activebackground="#222632",
            activeforeground=TEXT, relief="flat",
            font=("Helvetica Neue", 11), padx=12, pady=3,
            highlightthickness=0, bd=0, cursor="pointinghand",
        )
        self.meet_btn.pack(side="left", padx=(0, 16))

        self.play_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            controls, text="Play English audio", variable=self.play_var,
            bg=BG, fg=TEXT, selectcolor=PANEL, activebackground=BG,
            activeforeground=TEXT, font=("Helvetica Neue", 11),
            highlightthickness=0, bd=0,
        ).pack(side="left")

        self.toggle_btn = tk.Button(
            controls, text="Start", command=self._toggle,
            bg=ACCENT, fg="white", activebackground="#3d76e0",
            activeforeground="white", relief="flat",
            font=("Helvetica Neue", 13, "bold"), padx=22, pady=4,
            highlightthickness=0, bd=0, cursor="pointinghand",
        )
        self.toggle_btn.pack(side="right")

        # API key row (shown only if env var is missing)
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            key_row = tk.Frame(self.root, bg=BG)
            key_row.pack(fill="x", padx=20, pady=(0, 8))
            tk.Label(key_row, text="GEMINI_API_KEY", bg=BG, fg=MUTED,
                     font=("Helvetica Neue", 11)).pack(side="left")
            self.key_var = tk.StringVar()
            tk.Entry(key_row, textvariable=self.key_var, show="•",
                     bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
                     font=("Helvetica Neue", 11)).pack(
                side="left", fill="x", expand=True, padx=(8, 0))
        else:
            self.key_var = None

        # Captions
        self.caption = scrolledtext.ScrolledText(
            self.root, wrap="word", bg=PANEL, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            font=("Helvetica Neue", 18), padx=16, pady=16, spacing3=8,
        )
        self.caption.pack(fill="both", expand=True, padx=20, pady=(4, 20))
        self.caption.configure(state="disabled")

    def _refresh_mics(self):
        self.mics = []  # list of (label, index)
        for i in range(pya.get_device_count()):
            info = pya.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                self.mics.append((info.get("name", f"Device {i}"), i))
        labels = [name for name, _ in self.mics]
        self.mic_menu["values"] = labels
        if labels:
            try:
                default_idx = pya.get_default_input_device_info()["index"]
            except Exception:
                default_idx = self.mics[0][1]
            sel = next((n for n, i in self.mics if i == default_idx), labels[0])
            self.mic_var.set(sel)

    def _selected_mic_index(self):
        label = self.mic_var.get()
        for name, idx in self.mics:
            if name == label:
                return idx
        return None

    def _select_meet_audio(self):
        """Auto-select the BlackHole virtual device for capturing Meet audio."""
        self._refresh_mics()  # pick up the driver if it was just installed
        match = next(
            (name for name, _ in self.mics if "blackhole" in name.lower()), None
        )
        if match:
            self.mic_var.set(match)
            self._set_status("Meet audio source selected (BlackHole)")
        else:
            self._set_status(
                "BlackHole not found — install it & reboot (see README)",
                error=True,
            )

    def _toggle(self):
        if self.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        api_key = self.api_key or (self.key_var.get().strip() if self.key_var else "")
        if not api_key:
            self._set_status("Set GEMINI_API_KEY first", error=True)
            return

        self._needs_paragraph = False
        self.translator = Translator(
            api_key=api_key,
            events=self.events,
            mic_index=self._selected_mic_index(),
            play_audio=self.play_var.get(),
        )
        self.translator.start()
        self.running = True
        self.toggle_btn.configure(text="Stop", bg="#e0564f",
                                  activebackground="#c8463f")

    def _stop(self):
        if self.translator is not None:
            self.translator.stop()
        self.running = False
        self.toggle_btn.configure(text="Start", bg=ACCENT,
                                  activebackground="#3d76e0")

    def _set_status(self, text, error=False):
        self.status_var.set(text)
        self.status_label.configure(fg="#e0564f" if error else MUTED)

    def _append_caption(self, text):
        self.caption.configure(state="normal")
        if self._needs_paragraph:
            self.caption.insert("end", "\n\n")
            self._needs_paragraph = False
        self.caption.insert("end", text)
        self.caption.see("end")
        self.caption.configure(state="disabled")

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self._set_status(payload)
                    if payload == "Stopped":
                        self._stop()
                elif kind == "caption":
                    self._append_caption(payload)
                elif kind == "turn_end":
                    self._needs_paragraph = True
                elif kind == "error":
                    self._set_status(f"Error: {payload}", error=True)
                    self._stop()
        except queue.Empty:
            pass
        self.root.after(60, self._drain_events)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
