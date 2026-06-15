#!/usr/bin/env bash
#
# TranslateAI interactive setup.
#
# Run from the repo root:
#     ./setup.sh
#
# Walks you through three steps:
#   1. Install Python dependencies   (Y = install, N = exit)
#   2. Enter your Gemini API key     (shows where to get one)
#   3. Run the app                   (Y = launch, N = exit)

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

API_KEY_URL="https://aistudio.google.com/apikey"

# --- styling (no-op when output isn't a terminal) ---------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; CYAN=$'\033[36m'
  YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; GREEN=""; CYAN=""; YELLOW=""; RED=""; RESET=""
fi

# Ask a yes/no question. Returns 0 for yes, 1 for no. Empty answer re-asks.
ask_yn() {
  local prompt="$1" ans
  while true; do
    read -r -p "$prompt ${DIM}[Y/N]${RESET} " ans
    case "$ans" in
      [Yy] | [Yy][Ee][Ss]) return 0 ;;
      [Nn] | [Nn][Oo])     return 1 ;;
      *) echo "  ${YELLOW}Please answer Y or N.${RESET}" ;;
    esac
  done
}

echo
echo "${BOLD}🌐  TranslateAI setup${RESET}"
echo "${DIM}Live Japanese ⇄ English captions, powered by the Gemini Live API.${RESET}"
echo

# --- Step 1: install packages -----------------------------------------------
echo "${BOLD}Step 1/3 — Install packages${RESET}"
if ask_yn "Install the Python dependencies now?"; then
  PY="$(command -v python3 || true)"
  if [ -z "$PY" ]; then
    echo "${RED}✗ Python 3 not found.${RESET} Install it from https://www.python.org/downloads/"
    echo "  (or ${BOLD}brew install python${RESET}), then re-run this script."
    exit 1
  fi

  if [ ! -x ".venv/bin/python" ]; then
    echo "  Creating virtual environment (.venv)…"
    "$PY" -m venv .venv
  fi
  ./.venv/bin/python -m pip install --upgrade pip >/dev/null

  echo "  Installing from requirements.txt…"
  if ! ./.venv/bin/pip install -r requirements.txt; then
    echo "${RED}✗ Install failed.${RESET} PyAudio needs PortAudio — run:"
    echo "      ${BOLD}brew install portaudio${RESET}"
    echo "  then re-run this script."
    exit 1
  fi
  echo "${GREEN}✓ Packages installed.${RESET}"
else
  echo "${DIM}Skipping install. Exiting.${RESET}"
  exit 0
fi
echo

# --- Step 2: API key --------------------------------------------------------
echo "${BOLD}Step 2/3 — Gemini API key${RESET}"
echo "  Don't have one? Create a free key here:"
echo "  ┌──────────────────────────────────────────────────┐"
echo "  │  ${CYAN}${API_KEY_URL}${RESET}            │"
echo "  └──────────────────────────────────────────────────┘"
# Emit an OSC-8 clickable link in real terminals (skipped when piped/redirected).
if [ -t 1 ]; then
  printf '  \033]8;;%s\033\\%sOpen Google AI Studio →%s\033]8;;\033\\\n' \
    "$API_KEY_URL" "$DIM" "$RESET"
fi
echo

read -r -p "  Paste your GEMINI_API_KEY (or leave blank to enter it in-app): " API_KEY
API_KEY="$(printf '%s' "$API_KEY" | tr -d '[:space:]')"

if [ -z "$API_KEY" ] && [ -n "${GEMINI_API_KEY:-}" ]; then
  echo "${DIM}  Using the GEMINI_API_KEY already set in your environment.${RESET}"
  API_KEY="$GEMINI_API_KEY"
fi

if [ -n "$API_KEY" ]; then
  export GEMINI_API_KEY="$API_KEY"
  echo "${GREEN}✓ API key set for this session.${RESET}"
else
  echo "${YELLOW}! No key provided — you can paste it into the app window later.${RESET}"
fi
echo

# --- Step 3: run ------------------------------------------------------------
echo "${BOLD}Step 3/3 — Run the app${RESET}"
if ask_yn "Launch TranslateAI now?"; then
  if [ ! -x ".venv/bin/python" ]; then
    echo "${RED}✗ No virtual environment found — run step 1 first.${RESET}"
    exit 1
  fi
  echo "${GREEN}Starting TranslateAI…${RESET}"
  exec ./.venv/bin/python app.py
else
  echo "${DIM}Done. Launch later with:  ./setup.sh   (or double-click launch.command)${RESET}"
  exit 0
fi
