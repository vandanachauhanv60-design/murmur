# Murmur

Voice dictation for your whole desktop. Hold a key, speak, release: clean, punctuated text is pasted into whatever app you're in.

- **Hold to talk** (`Ctrl+Alt`) or **hands-free** (`Ctrl+Alt+Space`, press again to stop)
- Floating pill with a live waveform, plus a tray icon
- Speech-to-text: fully local (faster-whisper) or fast cloud (Groq Whisper)
- AI cleanup with Claude: removes fillers, resolves self-corrections ("at 3, no, 4"), formats lists, numbers, emails
- Personal dictionary for names and jargon
- Your clipboard is restored after each paste

## Setup

```bash
git clone https://github.com/YOUR_NAME/murmur && cd murmur
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...                 # optional, enables AI cleanup
python app.py
```

Settings live in `~/.murmur/config.json` (tray menu → Open settings file). Change the hotkeys, language, style (`natural`, `formal`, `casual`) and add words to `dictionary`.

For the fastest results, set `"stt": {"provider": "groq"}` and `GROQ_API_KEY`. For fully offline use, keep `provider` as `local` and turn cleanup off.

## Permissions

- **macOS**: allow your terminal under System Settings → Privacy & Security → Microphone, Accessibility and Input Monitoring.
- **Linux**: works on X11. Wayland blocks global hotkeys and synthetic paste.
- **Windows**: nothing extra.

## Tuning the cleanup

The prompt is in `prompts.py`. It treats the transcript as data (so it can't be hijacked by dictated instructions) and falls back to the raw text if the model misbehaves.

## Build an executable

```bash
pip install pyinstaller && pyinstaller --noconsole --onefile app.py
```
