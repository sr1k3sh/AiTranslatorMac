# TranslateAI

A simple macOS desktop app that listens to spoken audio through your
microphone and shows the translation as live on-screen captions, powered by
the Gemini Live API.

## What it does

Two tabs:

### Live (speech)

- Captures your mic audio and streams it to the Gemini live-translate model.
- **Pick input and output languages** from the menus — English, 日本語
  (Japanese), नेपाली (Nepali), Español (Spanish), and 中文 (Chinese).
  Defaults to 日本語 → English. The **⇄** button flips the two in one click.
- Shows a **side-by-side transcript** — the original speech on the left and
  the translation on the right (they stack vertically on a narrow window).
  **Clear** wipes both panels.
- Optionally also plays the translated audio.

### Quick Translate (text)

- Type or **paste** text on the left and the translation appears on the right
  **automatically** — it translates shortly after you stop typing (no need to
  click anything). Changing the From/To languages re-translates too.
- The **⇄** button between the language menus swaps From/To and the text, so
  you can flip the direction in one click.
- The **Translate** button is still there if you want to force it immediately.
- **Copy** puts the translation on your clipboard; **Clear** empties both sides.

## Quick start

The easiest way — one command sets up everything and runs the app:

```sh
git clone https://github.com/sr1k3sh/AiTranslatorMac.git
cd AiTranslatorMac
./setup.sh
```

`setup.sh` walks you through three prompts:

1. **Install packages?** — `Y` creates a local `.venv` and installs the Python
   dependencies; `N` exits.
   PyAudio needs **PortAudio**; if the install fails, run
   `brew install portaudio` and re-run `./setup.sh`.
2. **Gemini API key** — paste your key (it links to where you get a free one).
   The key is **saved to a local `.env` file**, so you're asked **only once** —
   on later runs it detects the saved key and lets you **skip** this step (or
   replace it). Get a free key at <https://aistudio.google.com/apikey>.
3. **Run the app?** — `Y` launches TranslateAI; `N` exits (run `./setup.sh`
   again anytime).

> **Your API key is stored in `.env` in the project folder** — it's gitignored
> (never committed) and the app reads it automatically however you launch it,
> so you never have to `export` it or edit any settings by hand. To change it,
> just run `./setup.sh` again and choose to replace it.

> Requires **Python 3.11+** (uses `asyncio.TaskGroup` / `ExceptionGroup`).

## Other ways to launch (no terminal)

- **Double-click `launch.command`** in Finder — sets up the virtual environment
  on first run, then opens the app.
- Prefer a real app icon? **Double-click `build_app.command`** once to generate
  **`TranslateAI.app`**, which you can launch from Finder, the Dock, or
  Launchpad (drag it to Applications if you like).

Both pick up the API key saved in `.env` automatically.

> macOS Gatekeeper may warn the first time because these scripts are
> unsigned — right-click → **Open** to confirm.

## Manual setup (advanced)

If you'd rather do it by hand instead of `setup.sh`:

```sh
brew install portaudio                  # PyAudio needs this
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY="your-key-here"   # or let setup.sh save it to .env
python app.py
```

## Using the app

Pick your microphone, choose the **Input** and **Output** languages from the
menus (default 日本語 → English, and the **⇄** button flips them), then click
**Start** and speak (or play) audio. The original speech appears on the left
and its translation on the right. Use **Clear** to wipe both panels and
**Stop** to end the session — languages can only be changed while stopped.

For typing/pasting text instead of speech, use the **Quick Translate** tab.

### Translating Google Meet (or any app audio)

macOS can't let an app capture another app's audio directly, so route it
through a virtual device:

1. `brew install --cask blackhole-2ch` then **reboot**.
2. In **Audio MIDI Setup**, create a **Multi-Output Device** containing both
   **BlackHole 2ch** and your speakers (so you still hear the call).
3. In TranslateAI, pick that device from the **Sound output** menu (it
   auto-selects a Multi-Output Device if you have one), click **🎧 Meet**
   (auto-selects BlackHole as input) → **Start**.

> **No more diving into System Settings each time.** When you **Start**, the
> app switches the macOS sound output to the device you chose, and switches it
> **back** when you **Stop**. Your choice is remembered between launches.
> This needs the small helper `SwitchAudioSource` — `setup.sh` offers to
> install it, or run `brew install switchaudio-osx` yourself. Without it, the
> menu is disabled and you'd set the output manually (the old way).

The first run will trigger a macOS **microphone permission** prompt — allow it
(System Settings → Privacy & Security → Microphone).

## Notes

- The app requests audio output from the model and transcribes it to text for
  the captions. Tick **Play translated audio** to also hear the translation.
- Model: `models/gemini-3.5-live-translate-preview` (configurable in `app.py`).
