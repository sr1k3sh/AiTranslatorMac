# TranslateAI

A simple macOS desktop app that listens to spoken audio through your
microphone and shows the translation as live on-screen captions, powered by
the Gemini Live API.

## What it does

- Captures your mic audio and streams it to the Gemini live-translate model.
- **Pick input and output languages** from the menus — English, 日本語
  (Japanese), नेपाली (Nepali), Español (Spanish), and 中文 (Chinese).
  Defaults to 日本語 → English.
- Shows a **side-by-side transcript** — the original speech on the left and
  the translation on the right (they stack vertically on a narrow window).
  **Clear** wipes both panels.
- Optionally also plays the translated audio.

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

### Guided setup (recommended)

Run the interactive setup script from the repo and follow the three prompts:

```sh
./setup.sh
```

1. **Install packages?** — `Y` installs the Python dependencies into a local
   `.venv`; `N` exits.
2. **Gemini API key** — if `GEMINI_API_KEY` is already set, it offers to skip
   this step; otherwise paste your key (it shows where to get a free one), or
   leave it blank to enter it in the app window later.
3. **Run the app?** — `Y` launches TranslateAI; `N` exits.

### Click to launch (no terminal)

- **Double-click `launch.command`** in Finder. On first run it sets up the
  virtual environment and installs dependencies automatically, then opens the
  app. (You still need PortAudio — see the Setup step above.)
- Prefer a real app icon? **Double-click `build_app.command`** once to generate
  **`TranslateAI.app`**, which you can then launch from Finder, the Dock, or
  Launchpad (drag it to Applications if you like).

> macOS Gatekeeper may warn the first time because these scripts are
> unsigned — right-click → **Open** to confirm.

### From the terminal

```sh
source .venv/bin/activate
python app.py
```

Pick your microphone, choose the **Input** and **Output** languages from the
menus (default 日本語 → English), then click **Start** and speak (or play)
audio. The original speech appears on the left and its translation on the
right. Use **Clear** to wipe both panels and **Stop** to end the session —
languages can only be changed while stopped.

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
  the captions. Tick **Play translated audio** to also hear the translation.
- Model: `models/gemini-3.5-live-translate-preview` (configurable in `app.py`).
