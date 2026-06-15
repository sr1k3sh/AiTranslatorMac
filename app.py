"""
TranslateAI — Live speech translation captions for macOS.

Listens to your microphone, streams the audio to the Gemini Live API
(live-translate model), and shows the translation as live captions in a
simple desktop window. Pick the input and output languages from the menus
(defaults: 日本語 → English).

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
import tkinter.font as tkfont

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
TEXT_MODEL = "gemini-2.5-flash"   # used by the Quick Translate (text) tab
QUICK_DEBOUNCE_MS = 700           # idle delay before auto-translating typed text

# Languages offered for both input and output, as code -> display label. The
# live-translate model auto-detects the spoken language, so the *output* choice
# drives the translation (target_language_code); the *input* choice sets the
# header label and the user's expectation.
LANGUAGES = {
    "ja": "日本語 (Japanese)",
    "en": "English",
    "ne": "नेपाली (Nepali)",
    "es": "Español (Spanish)",
    "zh": "中文 (Chinese)",
}
DEFAULT_INPUT = "ja"
DEFAULT_OUTPUT = "en"

pya = pyaudio.PyAudio()


def lang_name(code: str) -> str:
    """Short display name for a language code, e.g. 'ja' -> '日本語'."""
    return LANGUAGES.get(code, code).split(" (")[0]


def code_for_label(label: str) -> str:
    """Map a combobox label back to its language code."""
    for code, name in LANGUAGES.items():
        if name == label:
            return code
    return label


def build_config(play_audio: bool, target_language: str) -> types.LiveConnectConfig:
    """Live API config: speak audio in, get the target language back.

    We always request AUDIO (what the live-translate model is built for) plus
    both transcriptions: the input transcription is the original speech, and
    the output transcription is the translated text.
    """
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=target_language,
        ),
        # Transcribe the user's speech (original) and the model's translated
        # audio so we can render both side by side.
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


def translate_text(api_key: str, text: str, source: str, target: str) -> str:
    """One-shot text translation via the Gemini text model (Quick Translate)."""
    client = genai.Client(api_key=api_key)
    prompt = (
        f"Translate the following text from {lang_name(source)} to "
        f"{lang_name(target)}. Return only the translation, with no quotes, "
        f"notes, or extra commentary.\n\n{text}"
    )
    response = client.models.generate_content(model=TEXT_MODEL, contents=prompt)
    return (response.text or "").strip()


# --- Backend: streams mic -> Gemini -> caption events -----------------------

class Translator:
    """Runs the Gemini Live session on its own asyncio thread.

    UI updates are pushed as
    ("status"|"original"|"translated"|"turn_end"|"error", payload) tuples onto
    a thread-safe queue that the Tk main loop drains.
    """

    def __init__(self, api_key, events, mic_index=None, play_audio=False,
                 target_language="en"):
        self.api_key = api_key
        self.events = events
        self.mic_index = mic_index
        self.play_audio = play_audio
        self.target_language = target_language

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
        config = build_config(self.play_audio, self.target_language)

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
                    original = sc.input_transcription
                    if original is not None and original.text:
                        self._emit("original", original.text)
                    translated = sc.output_transcription
                    if translated is not None and translated.text:
                        self._emit("translated", translated.text)
                    if sc.turn_complete:
                        self._emit("turn_end")
                # Fallback for TEXT-modality responses.
                if response.text:
                    self._emit("translated", response.text)
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
PANEL_HOVER = "#222632"
ACCENT = "#4f8cff"
ACCENT_HOVER = "#3d76e0"
DANGER = "#e0564f"
DANGER_HOVER = "#c8463f"
TEXT = "#e7e9ee"
MUTED = "#8a91a0"
DISABLED = "#3a3f4b"


class RoundedButton(tk.Canvas):
    """A flat, rounded button drawn on a Canvas.

    macOS Aqua ignores ``bg``/``fg`` on a native ``tk.Button``, which makes
    custom-coloured buttons render with invisible text. Drawing on a Canvas
    sidesteps that entirely and gives us hover/disabled states for free.
    """

    def __init__(self, master, *, text="", textvariable=None, command=None,
                 fill=PANEL, fg=TEXT, hover_fill=PANEL_HOVER, parent_bg=BG,
                 font=("Helvetica Neue", 11), padx=16, pady=7, radius=11):
        super().__init__(master, bg=parent_bg, highlightthickness=0, bd=0)
        self._command = command
        self._fill = fill
        self._hover_fill = hover_fill
        self._fg = fg
        self._radius = radius
        self._padx = padx
        self._pady = pady
        self._font = tkfont.Font(font=font)
        self._enabled = True
        self._hovered = False

        self._textvariable = textvariable
        self._text = textvariable.get() if textvariable is not None else text
        if textvariable is not None:
            textvariable.trace_add("write", self._on_var_change)

        self.configure(cursor="pointinghand")
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._resize_to_text()

    # -- public API (mirrors the bits of tk.Button we use) --

    def set_text(self, text):
        self._text = text
        self._resize_to_text()

    def set_style(self, *, fill=None, hover_fill=None):
        if fill is not None:
            self._fill = fill
        if hover_fill is not None:
            self._hover_fill = hover_fill
        self._redraw()

    def set_enabled(self, enabled):
        self._enabled = enabled
        self.configure(cursor="pointinghand" if enabled else "arrow")
        self._redraw()

    # -- internals --

    def _on_var_change(self, *_):
        self.set_text(self._textvariable.get())

    def _resize_to_text(self):
        w = self._font.measure(self._text) + self._padx * 2
        h = self._font.metrics("linespace") + self._pady * 2
        self.configure(width=w, height=h)
        self._redraw()

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.create_polygon(pts, smooth=True, **kw)

    def _redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        if not self._enabled:
            fill, fg = PANEL, DISABLED
        elif self._hovered:
            fill, fg = self._hover_fill, self._fg
        else:
            fill, fg = self._fill, self._fg
        self._round_rect(1, 1, w - 1, h - 1, self._radius, fill=fill, outline=fill)
        self.create_text(w / 2, h / 2, text=self._text, fill=fg, font=self._font)

    def _on_enter(self, _e):
        self._hovered = True
        self._redraw()

    def _on_leave(self, _e):
        self._hovered = False
        self._redraw()

    def _on_release(self, _e):
        if self._enabled and self._command is not None:
            self._command()


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.translator = None
        self.running = False
        self._orig_needs_para = False
        self._trans_needs_para = False
        self._transcript_wide = None

        # Quick Translate: debounce timer + a sequence guard so a slow earlier
        # request can't overwrite a newer translation.
        self._quick_after_id = None
        self._quick_seq = 0
        self._quick_last = None

        root.title("TranslateAI")
        root.configure(bg=BG)
        root.geometry("760x560")
        root.minsize(560, 420)

        self._build_ui()
        self._apply_languages()
        self._refresh_mics()
        self.root.after(60, self._drain_events)

    def _build_ui(self):
        # Fonts kept as objects so we can rescale them on resize.
        self.header_font = tkfont.Font(family="Helvetica Neue", size=20,
                                       weight="bold")
        self.caption_font = tkfont.Font(family="Helvetica Neue", size=18)

        # Shared API key — env var, or a field shown when it's missing.
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            key_row = tk.Frame(self.root, bg=BG)
            key_row.pack(fill="x", padx=20, pady=(12, 4))
            tk.Label(key_row, text="GEMINI_API_KEY", bg=BG, fg=MUTED,
                     font=("Helvetica Neue", 11)).pack(side="left")
            self.key_var = tk.StringVar()
            tk.Entry(key_row, textvariable=self.key_var, show="•",
                     bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
                     font=("Helvetica Neue", 11)).pack(
                side="left", fill="x", expand=True, padx=(8, 0))
        else:
            self.key_var = None

        # Two tabs: Live (audio) and Quick Translate (text).
        style = ttk.Style()
        try:
            style.configure("TNotebook", background=BG, borderwidth=0)
            style.configure("TNotebook.Tab", padding=(16, 6))
        except tk.TclError:
            pass
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        self.live_tab = tk.Frame(self.notebook, bg=BG)
        self.quick_tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.live_tab, text="Live")
        self.notebook.add(self.quick_tab, text="Quick Translate")

        self._build_live_tab()
        self._build_quick_tab()

        # Drive responsive reflow + font scaling off the window size.
        self.root.bind("<Configure>", self._on_resize)

    def _build_live_tab(self):
        parent = self.live_tab

        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x", padx=20, pady=(16, 8))

        self.header_var = tk.StringVar(value="Live translation")
        tk.Label(
            header, textvariable=self.header_var,
            bg=BG, fg=TEXT, font=self.header_font,
        ).pack(side="left")

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = tk.Label(
            header, textvariable=self.status_var,
            bg=BG, fg=MUTED, font=("Helvetica Neue", 12),
        )
        self.status_label.pack(side="right")

        # Language selection — Input → Output.
        labels = list(LANGUAGES.values())
        lang_frame = tk.Frame(parent, bg=BG)
        lang_frame.pack(fill="x", padx=20, pady=(4, 2))

        tk.Label(lang_frame, text="Input", bg=BG, fg=MUTED,
                 font=("Helvetica Neue", 11)).pack(side="left")
        self.input_var = tk.StringVar(value=LANGUAGES[DEFAULT_INPUT])
        self.input_menu = ttk.Combobox(
            lang_frame, textvariable=self.input_var, values=labels,
            state="readonly", width=16,
        )
        self.input_menu.pack(side="left", padx=(8, 8))
        self.input_menu.bind("<<ComboboxSelected>>", self._on_language_change)

        tk.Label(lang_frame, text="→", bg=BG, fg=TEXT,
                 font=("Helvetica Neue", 14, "bold")).pack(side="left")

        tk.Label(lang_frame, text="Output", bg=BG, fg=MUTED,
                 font=("Helvetica Neue", 11)).pack(side="left", padx=(8, 0))
        self.output_var = tk.StringVar(value=LANGUAGES[DEFAULT_OUTPUT])
        self.output_menu = ttk.Combobox(
            lang_frame, textvariable=self.output_var, values=labels,
            state="readonly", width=16,
        )
        self.output_menu.pack(side="left", padx=(8, 0))
        self.output_menu.bind("<<ComboboxSelected>>", self._on_language_change)

        # Controls — laid out with grid so they can reflow responsively.
        self.controls = tk.Frame(parent, bg=BG)
        self.controls.pack(fill="x", padx=20, pady=(4, 8))

        self.mic_label = tk.Label(self.controls, text="Mic", bg=BG, fg=MUTED,
                                  font=("Helvetica Neue", 11))
        self.mic_var = tk.StringVar()
        self.mic_menu = ttk.Combobox(
            self.controls, textvariable=self.mic_var, state="readonly", width=24
        )

        self.meet_btn = RoundedButton(
            self.controls, text="🎧 Meet", command=self._select_meet_audio,
            fill=PANEL, hover_fill=PANEL_HOVER,
        )

        self.play_var = tk.BooleanVar(value=False)
        self.play_check = tk.Checkbutton(
            self.controls, text="Play translated audio", variable=self.play_var,
            bg=BG, fg=TEXT, selectcolor=PANEL, activebackground=BG,
            activeforeground=TEXT, font=("Helvetica Neue", 11),
            highlightthickness=0, bd=0,
        )

        self.clear_btn = RoundedButton(
            self.controls, text="Clear", command=self._clear_captions,
            fill=PANEL, hover_fill=PANEL_HOVER,
        )

        self.toggle_btn = RoundedButton(
            self.controls, text="Start", command=self._toggle,
            fill=ACCENT, hover_fill=ACCENT_HOVER, fg="white",
            font=("Helvetica Neue", 13, "bold"), padx=24, pady=7,
        )

        self._controls_wide = None
        self._layout_controls(wide=True)

        # Transcript — original (left) and translation (right), side by side.
        self.transcript = tk.Frame(parent, bg=BG)
        self.transcript.pack(fill="both", expand=True, padx=20, pady=(4, 20))
        self.orig_panel, self.orig_text = self._build_panel("Original")
        self.trans_panel, self.trans_text = self._build_panel("Translation")
        self._layout_transcript(wide=True)

    def _build_quick_tab(self):
        """Text translation: type/paste on the left, see the result on the right."""
        parent = self.quick_tab
        labels = list(LANGUAGES.values())

        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=20, pady=(16, 6))

        tk.Label(row, text="From", bg=BG, fg=MUTED,
                 font=("Helvetica Neue", 11)).pack(side="left")
        self.q_input_var = tk.StringVar(value=LANGUAGES[DEFAULT_INPUT])
        self.q_input_menu = ttk.Combobox(
            row, textvariable=self.q_input_var, values=labels,
            state="readonly", width=16)
        self.q_input_menu.pack(side="left", padx=(8, 8))
        self.q_input_menu.bind("<<ComboboxSelected>>", self._quick_schedule)

        # Clickable swap: flip the From/To languages and the text.
        self.q_swap_btn = RoundedButton(
            row, text="⇄", command=self._quick_swap,
            fill=PANEL, hover_fill=PANEL_HOVER,
            font=("Helvetica Neue", 14, "bold"), padx=12, pady=5,
        )
        self.q_swap_btn.pack(side="left", padx=(2, 2))

        tk.Label(row, text="To", bg=BG, fg=MUTED,
                 font=("Helvetica Neue", 11)).pack(side="left", padx=(8, 0))
        self.q_output_var = tk.StringVar(value=LANGUAGES[DEFAULT_OUTPUT])
        self.q_output_menu = ttk.Combobox(
            row, textvariable=self.q_output_var, values=labels,
            state="readonly", width=16)
        self.q_output_menu.pack(side="left", padx=(8, 0))
        self.q_output_menu.bind("<<ComboboxSelected>>", self._quick_schedule)

        self.q_translate_btn = RoundedButton(
            row, text="Translate", command=self._quick_translate,
            fill=ACCENT, hover_fill=ACCENT_HOVER, fg="white",
            font=("Helvetica Neue", 13, "bold"), padx=24, pady=7,
        )
        self.q_translate_btn.pack(side="right")

        # Panels: editable input (left) + read-only output (right).
        self.q_panels = tk.Frame(parent, bg=BG)
        self.q_panels.pack(fill="both", expand=True, padx=20, pady=(4, 6))

        self.q_in_panel, self.q_input_text, in_actions = self._build_quick_panel(
            "Type or paste text", editable=True)
        # Translate automatically a short moment after typing/pasting stops.
        self.q_input_text.bind("<KeyRelease>", self._quick_schedule)
        self.q_input_text.bind("<<Paste>>", self._quick_schedule)
        RoundedButton(in_actions, text="Paste", command=self._quick_paste,
                      fill=PANEL, hover_fill=PANEL_HOVER).pack(side="right")
        RoundedButton(in_actions, text="Clear", command=self._quick_clear,
                      fill=PANEL, hover_fill=PANEL_HOVER).pack(side="right", padx=(0, 8))

        self.q_out_panel, self.q_output_text, out_actions = self._build_quick_panel(
            "Translation", editable=False)
        RoundedButton(out_actions, text="Copy", command=self._quick_copy,
                      fill=PANEL, hover_fill=PANEL_HOVER).pack(side="right")

        self._quick_wide = None
        self._layout_quick(wide=True)

        self.q_status_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self.q_status_var, bg=BG, fg=MUTED,
                 font=("Helvetica Neue", 11), anchor="w").pack(
            fill="x", padx=20, pady=(0, 14))

    def _build_quick_panel(self, title, editable):
        """Panel with a title row (label + action buttons) and a text area.

        Returns (panel_frame, text_widget, actions_frame).
        """
        panel = tk.Frame(self.q_panels, bg=BG)
        head = tk.Frame(panel, bg=BG)
        head.pack(fill="x", pady=(0, 4))
        tk.Label(head, text=title, bg=BG, fg=MUTED,
                 font=("Helvetica Neue", 11, "bold")).pack(side="left")
        actions = tk.Frame(head, bg=BG)
        actions.pack(side="right")
        text = scrolledtext.ScrolledText(
            panel, wrap="word", bg=PANEL, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            font=self.caption_font, padx=14, pady=14, spacing3=8,
        )
        text.pack(fill="both", expand=True)
        if not editable:
            text.configure(state="disabled")
        return panel, text, actions

    def _layout_quick(self, wide):
        """Side by side when wide; stacked (input on top) when narrow."""
        if wide == self._quick_wide:
            return
        self._quick_wide = wide
        t = self.q_panels
        self.q_in_panel.grid_forget()
        self.q_out_panel.grid_forget()
        for i in range(2):
            t.grid_rowconfigure(i, weight=0)
            t.grid_columnconfigure(i, weight=0)

        if wide:
            self.q_in_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
            self.q_out_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
            t.grid_rowconfigure(0, weight=1)
            t.grid_columnconfigure(0, weight=1)
            t.grid_columnconfigure(1, weight=1)
        else:
            self.q_in_panel.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
            self.q_out_panel.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
            t.grid_columnconfigure(0, weight=1)
            t.grid_rowconfigure(0, weight=1)
            t.grid_rowconfigure(1, weight=1)

    def _build_panel(self, title):
        """A titled, read-only scrolling text panel for the transcript view."""
        panel = tk.Frame(self.transcript, bg=BG)
        tk.Label(panel, text=title, bg=BG, fg=MUTED,
                 font=("Helvetica Neue", 11, "bold")).pack(anchor="w", pady=(0, 4))
        text = scrolledtext.ScrolledText(
            panel, wrap="word", bg=PANEL, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            font=self.caption_font, padx=14, pady=14, spacing3=8,
        )
        text.pack(fill="both", expand=True)
        text.configure(state="disabled")
        return panel, text

    def _layout_transcript(self, wide):
        """Side by side when wide; stacked (original on top) when narrow."""
        if wide == self._transcript_wide:
            return
        self._transcript_wide = wide
        t = self.transcript
        self.orig_panel.grid_forget()
        self.trans_panel.grid_forget()
        for i in range(2):
            t.grid_rowconfigure(i, weight=0)
            t.grid_columnconfigure(i, weight=0)

        if wide:
            self.orig_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
            self.trans_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
            t.grid_rowconfigure(0, weight=1)
            t.grid_columnconfigure(0, weight=1)
            t.grid_columnconfigure(1, weight=1)
        else:
            self.orig_panel.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
            self.trans_panel.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
            t.grid_columnconfigure(0, weight=1)
            t.grid_rowconfigure(0, weight=1)
            t.grid_rowconfigure(1, weight=1)

    def _layout_controls(self, wide):
        """Single row when wide; two rows (mic on top) when narrow."""
        if wide == self._controls_wide:
            return
        self._controls_wide = wide
        c = self.controls
        for w in (self.mic_label, self.mic_menu, self.meet_btn,
                  self.play_check, self.clear_btn, self.toggle_btn):
            w.grid_forget()
        for col in range(6):
            c.grid_columnconfigure(col, weight=0)

        if wide:
            self.mic_label.grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
            self.mic_menu.grid(row=0, column=1, padx=(0, 12), pady=4, sticky="ew")
            self.meet_btn.grid(row=0, column=2, padx=(0, 16), pady=4)
            self.play_check.grid(row=0, column=3, padx=(0, 12), pady=4, sticky="w")
            self.clear_btn.grid(row=0, column=4, padx=(0, 8), pady=4, sticky="e")
            self.toggle_btn.grid(row=0, column=5, pady=4, sticky="e")
            c.grid_columnconfigure(1, weight=1)
        else:
            self.mic_label.grid(row=0, column=0, padx=(0, 8), pady=(4, 8), sticky="w")
            self.mic_menu.grid(row=0, column=1, columnspan=3, pady=(4, 8), sticky="ew")
            self.meet_btn.grid(row=1, column=0, padx=(0, 8), pady=(0, 4), sticky="w")
            self.play_check.grid(row=1, column=1, padx=(0, 8), pady=(0, 4), sticky="w")
            self.clear_btn.grid(row=1, column=2, padx=(0, 8), pady=(0, 4), sticky="w")
            self.toggle_btn.grid(row=1, column=3, pady=(0, 4), sticky="e")
            c.grid_columnconfigure(1, weight=1)
            c.grid_columnconfigure(3, weight=1)

    def _on_resize(self, event):
        if event.widget is not self.root:
            return
        width = event.width
        self._layout_controls(wide=width >= 720)
        # Two side-by-side panels need room; stack them below this width.
        self._layout_transcript(wide=width >= 720)
        self._layout_quick(wide=width >= 720)
        # Scale caption + header type to the window width (clamped).
        span = max(0.0, min(1.0, (width - 560) / (1100 - 560)))
        self.caption_font.configure(size=int(round(15 + span * 8)))   # 15–23
        self.header_font.configure(size=int(round(17 + span * 7)))    # 17–24

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

    def _input_code(self):
        return code_for_label(self.input_var.get())

    def _output_code(self):
        return code_for_label(self.output_var.get())

    def _apply_languages(self):
        """Sync the window title and header to the selected languages."""
        label = f"{lang_name(self._input_code())} → {lang_name(self._output_code())}"
        self.header_var.set(f"Live {label}")
        self.root.title(f"TranslateAI — {label}")

    def _on_language_change(self, _event=None):
        """Selecting a language while running would need a reconnect."""
        if self.running:
            self._set_status("Stop before changing languages", error=True)
            return
        self._apply_languages()

    def _clear_captions(self):
        """Wipe both transcript panels."""
        for widget in (self.orig_text, self.trans_text):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        self._orig_needs_para = False
        self._trans_needs_para = False

    # -- Quick Translate (text) tab --

    def _q_status(self, text, error=False):
        self.q_status_var.set(text)

    def _resolve_key(self):
        return self.api_key or (self.key_var.get().strip() if self.key_var else "")

    def _set_quick_output(self, text):
        self.q_output_text.configure(state="normal")
        self.q_output_text.delete("1.0", "end")
        self.q_output_text.insert("end", text)
        self.q_output_text.configure(state="disabled")

    def _quick_schedule(self, _event=None):
        """Debounce: (re)arm an auto-translate a short moment after input stops."""
        if self._quick_after_id is not None:
            self.root.after_cancel(self._quick_after_id)
        self._quick_after_id = self.root.after(QUICK_DEBOUNCE_MS, self._quick_auto)

    def _quick_auto(self):
        self._quick_after_id = None
        self._quick_translate(auto=True)

    def _quick_swap(self):
        """Flip From/To and swap the text between the two panels."""
        in_text = self.q_input_text.get("1.0", "end").strip()
        out_text = self.q_output_text.get("1.0", "end").strip()

        # Flip the language selections.
        from_lang, to_lang = self.q_input_var.get(), self.q_output_var.get()
        self.q_input_var.set(to_lang)
        self.q_output_var.set(from_lang)

        if out_text:
            # The current translation is already the source text in the new
            # "From" language, and the old source is its translation in the new
            # "To" language — so we can swap both in place, no API call needed.
            self.q_input_text.delete("1.0", "end")
            self.q_input_text.insert("end", out_text)
            self._set_quick_output(in_text)
            self._quick_last = out_text
            self._q_status("Swapped")
        else:
            # Nothing translated yet — just re-run with the languages flipped.
            self._quick_last = None
            self._quick_schedule()

    def _quick_translate(self, auto=False):
        text = self.q_input_text.get("1.0", "end").strip()
        if not text:
            self._set_quick_output("")
            self._quick_last = None
            self._q_status("" if auto else "Type or paste some text first",
                           error=not auto)
            return
        api_key = self._resolve_key()
        if not api_key:
            self._q_status("Set GEMINI_API_KEY first", error=True)
            return
        # Skip if this exact text was already translated (avoids redundant calls
        # while typing); a manual click always re-runs.
        if auto and text == self._quick_last:
            return

        source = code_for_label(self.q_input_var.get())
        target = code_for_label(self.q_output_var.get())
        self._quick_seq += 1
        seq = self._quick_seq
        self.q_translate_btn.set_enabled(False)
        self._q_status("Translating…")

        def work():
            try:
                result = translate_text(api_key, text, source, target)
                self.events.put(("quick_result", (seq, text, result)))
            except Exception as exc:  # surface API/network errors in the UI
                self.events.put(("quick_error", (seq, str(exc))))

        threading.Thread(target=work, daemon=True).start()

    def _quick_done(self, seq, result, error, source_text=None):
        # Ignore results from a request that a newer one has superseded.
        if seq != self._quick_seq:
            return
        self.q_translate_btn.set_enabled(True)
        if error is not None:
            self._q_status(f"Error: {error}", error=True)
            return
        self._set_quick_output(result or "")
        self._quick_last = source_text
        self._q_status("Done")

    def _quick_copy(self):
        out = self.q_output_text.get("1.0", "end").strip()
        if not out:
            self._q_status("Nothing to copy", error=True)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(out)
        self._q_status("Translation copied to clipboard")

    def _quick_paste(self):
        try:
            clip = self.root.clipboard_get()
        except tk.TclError:
            clip = ""
        if clip:
            self.q_input_text.insert("insert", clip)
            self._q_status("Pasted from clipboard")
            self._quick_schedule()
        else:
            self._q_status("Clipboard is empty", error=True)

    def _quick_clear(self):
        if self._quick_after_id is not None:
            self.root.after_cancel(self._quick_after_id)
            self._quick_after_id = None
        self.q_input_text.delete("1.0", "end")
        self._set_quick_output("")
        self._quick_last = None
        self._q_status("")

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

        self._orig_needs_para = False
        self._trans_needs_para = False
        self.translator = Translator(
            api_key=api_key,
            events=self.events,
            mic_index=self._selected_mic_index(),
            play_audio=self.play_var.get(),
            target_language=self._output_code(),
        )
        self.translator.start()
        self.running = True
        self.toggle_btn.set_text("Stop")
        self.toggle_btn.set_style(fill=DANGER, hover_fill=DANGER_HOVER)
        self.input_menu.configure(state="disabled")
        self.output_menu.configure(state="disabled")

    def _stop(self):
        if self.translator is not None:
            self.translator.stop()
        self.running = False
        self.toggle_btn.set_text("Start")
        self.toggle_btn.set_style(fill=ACCENT, hover_fill=ACCENT_HOVER)
        self.input_menu.configure(state="readonly")
        self.output_menu.configure(state="readonly")

    def _set_status(self, text, error=False):
        self.status_var.set(text)
        self.status_label.configure(fg="#e0564f" if error else MUTED)

    def _append(self, widget, text, para_attr):
        widget.configure(state="normal")
        if getattr(self, para_attr):
            widget.insert("end", "\n\n")
            setattr(self, para_attr, False)
        widget.insert("end", text)
        widget.see("end")
        widget.configure(state="disabled")

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self._set_status(payload)
                    if payload == "Stopped":
                        self._stop()
                elif kind == "original":
                    self._append(self.orig_text, payload, "_orig_needs_para")
                elif kind == "translated":
                    self._append(self.trans_text, payload, "_trans_needs_para")
                elif kind == "turn_end":
                    self._orig_needs_para = True
                    self._trans_needs_para = True
                elif kind == "error":
                    self._set_status(f"Error: {payload}", error=True)
                    self._stop()
                elif kind == "quick_result":
                    seq, src, result = payload
                    self._quick_done(seq, result, None, src)
                elif kind == "quick_error":
                    seq, msg = payload
                    self._quick_done(seq, None, msg)
        except queue.Empty:
            pass
        self.root.after(60, self._drain_events)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
