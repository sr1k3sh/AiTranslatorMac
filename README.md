# TranslateAI

A simple macOS desktop app that listens to spoken **Japanese** through your
microphone and shows the **English** translation as live on-screen captions,
powered by the Gemini Live API.

## What it does

- Captures your mic audio and streams it to the Gemini live-translate model.
- Displays the English translation as scrolling text in a window.
- Optionally also plays the English translation as audio.

## Setup

1. **Install PortAudio** (needed by PyAudio):

   ```sh
   brew install portaudio
   ```

2. **Create a virtual environment and install dependencies:**

   ```sh
   python3.13 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

   > Requires Python 3.11+ (uses `asyncio.TaskGroup` / `ExceptionGroup`).

3. **Set your Gemini API key** (or paste it into the app window):

   ```sh
   export GEMINI_API_KEY="your-key-here"
   ```

   Get a key at https://aistudio.google.com/apikey

## Run

```sh
source .venv/bin/activate
python app.py
```

Pick your microphone, click **Start**, and speak (or play) Japanese audio.
English captions appear in the window. Click **Stop** to end the session.

### Translating Google Meet (or any app audio)

macOS can't let an app capture another app's audio directly, so route it
through a virtual device:

1. `brew install --cask blackhole-2ch` then **reboot**.
2. In **Audio MIDI Setup**, create a **Multi-Output Device** containing both
   **BlackHole 2ch** and your speakers (so you still hear the call).
3. In **System Settings → Sound → Output**, select that Multi-Output Device.
4. In TranslateAI, click **🎧 Meet** (auto-selects BlackHole) → **Start**.

The first run will trigger a macOS **microphone permission** prompt — allow it
(System Settings → Privacy & Security → Microphone).

## Notes

- The app requests audio output from the model and transcribes it to text for
  the captions. Tick **Play English audio** to also hear the translation.
- Model: `models/gemini-3.5-live-translate-preview` (configurable in `app.py`).
