"""Murmur: hold a hotkey, speak, get clean text pasted into any app."""
import io, json, os, platform, queue, re, subprocess, sys, threading, time, wave
from collections import deque
from pathlib import Path
import tkinter as tk

import numpy as np
import pyperclip
import sounddevice as sd
from pynput import keyboard

from prompts import build_cleanup_system, whisper_hint

SR = 16000
SYSTEM = platform.system()
CONFIG_DIR = Path.home() / ".murmur"
CONFIG_PATH = CONFIG_DIR / "config.json"
DEFAULTS = {
    "hotkey": "ctrl+alt",                  # hold to talk
    "hands_free_hotkey": "ctrl+alt+space", # press once to start, again to stop
    "language": "en",                      # null = auto-detect
    "stt": {"provider": "local", "local_model": "small", "groq_model": "whisper-large-v3-turbo"},
    "cleanup": {"enabled": True, "model": "claude-haiku-4-5-20251001", "style": "natural"},
    "dictionary": [],
    "api_keys": {"anthropic": "", "groq": ""},
}


def load_config():
    CONFIG_DIR.mkdir(exist_ok=True)
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text())
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2))
    return cfg


def api_key(cfg, name):
    return cfg["api_keys"].get(name) or os.environ.get(name.upper() + "_API_KEY", "")


# ---------------------------------------------------------------- hotkeys
def canon(k):
    if isinstance(k, keyboard.Key):
        n = k.name
        if n.endswith("_l") or n.endswith("_r"):
            n = n[:-2]
        return {"alt_gr": "alt"}.get(n, n)
    ch = getattr(k, "char", None)
    return ch.lower() if ch else None


class Combo:
    def __init__(self, spec, on_down, on_up=None):
        alias = {"win": "cmd", "super": "cmd", "option": "alt", "control": "ctrl"}
        self.keys = {alias.get(p.strip(), p.strip()) for p in spec.lower().split("+")}
        self.on_down, self.on_up, self.active = on_down, on_up, False

    def update(self, down):
        held = self.keys <= down
        if held and not self.active:
            self.active = True
            self.on_down()
        elif not held and self.active:
            self.active = False
            if self.on_up:
                self.on_up()


# ---------------------------------------------------------------- audio
class Recorder:
    def __init__(self, on_level):
        self.on_level, self.frames, self.stream = on_level, [], None

    def start(self):
        self.frames = []

        def cb(indata, n, t, status):
            self.frames.append(indata.copy())
            self.on_level(float(np.sqrt(np.mean(indata ** 2))))

        self.stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", callback=cb)
        self.stream.start()

    def stop(self):
        self.stream.stop()
        self.stream.close()
        return np.concatenate(self.frames)[:, 0] if self.frames else np.zeros(0, "float32")


def to_wav(audio):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------- speech + cleanup
class Transcriber:
    def __init__(self, cfg):
        self.cfg, self.model = cfg, None

    def run(self, audio):
        hint = whisper_hint(self.cfg["dictionary"])
        lang = self.cfg["language"]
        if self.cfg["stt"]["provider"] == "groq":
            import requests
            r = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": "Bearer " + api_key(self.cfg, "groq")},
                files={"file": ("a.wav", to_wav(audio), "audio/wav")},
                data={"model": self.cfg["stt"]["groq_model"], "response_format": "text",
                      "temperature": 0, **({"language": lang} if lang else {}),
                      **({"prompt": hint} if hint else {})},
                timeout=30,
            )
            r.raise_for_status()
            return r.text.strip()
        if self.model is None:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.cfg["stt"]["local_model"], compute_type="int8")
        segs, _ = self.model.transcribe(audio, language=lang, initial_prompt=hint,
                                        vad_filter=True, beam_size=3)
        return " ".join(s.text.strip() for s in segs).strip()


class Cleaner:
    def __init__(self, cfg):
        self.cfg, self.client = cfg, None

    def run(self, raw):
        c = self.cfg["cleanup"]
        if not c["enabled"] or len(raw.split()) <= 2 or not api_key(self.cfg, "anthropic"):
            return raw
        try:
            if self.client is None:
                import anthropic
                self.client = anthropic.Anthropic(api_key=api_key(self.cfg, "anthropic"))
            msg = self.client.messages.create(
                model=c["model"], max_tokens=1500, temperature=0,
                system=build_cleanup_system(self.cfg["dictionary"], c["style"]),
                messages=[{"role": "user", "content": f"<transcript>\n{raw}\n</transcript>"}],
            )
            out = re.sub(r"</?transcript>", "", msg.content[0].text).strip()
            # Guard: if the model started "answering" instead of cleaning, keep the raw text.
            if not out or len(out) > 2 * len(raw) + 60:
                return raw
            return out
        except Exception as e:
            print("cleanup failed, using raw text:", e)
            return raw


def paste(text):
    kb = keyboard.Controller()
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    pyperclip.copy(text)
    time.sleep(0.08)
    mod = keyboard.Key.cmd if SYSTEM == "Darwin" else keyboard.Key.ctrl
    with kb.pressed(mod):
        kb.press("v"); kb.release("v")
    time.sleep(0.3)
    if old is not None:
        pyperclip.copy(old)


# ---------------------------------------------------------------- overlay
PILL, DOT, BAR, DIM, OK = "#17191e", "#ff5b4f", "#f2f0ea", "#8b909a", "#7bd88f"


class Overlay:
    W, H, N = 200, 46, 22

    def __init__(self, root, q):
        self.root, self.q = root, q
        self.state, self.until, self.msg = "idle", 0, ""
        self.levels = deque([0.0] * self.N, maxlen=self.N)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        key = "#010101"
        bg = key
        if SYSTEM == "Windows":
            root.attributes("-transparentcolor", key)
        elif SYSTEM == "Darwin":
            root.attributes("-transparent", True)
            bg = "systemTransparent"
            root.config(bg=bg)
        self.c = tk.Canvas(root, width=self.W, height=self.H, bg=bg, highlightthickness=0)
        self.c.pack()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{self.W}x{self.H}+{(sw - self.W) // 2}+{sh - self.H - 80}")
        root.update_idletasks()
        if SYSTEM == "Windows":  # never steal focus from the app being dictated into
            try:
                import ctypes
                u = ctypes.windll.user32
                hwnd = u.GetParent(root.winfo_id())
                u.SetWindowLongW(hwnd, -20, u.GetWindowLongW(hwnd, -20) | 0x08000000 | 0x80)
            except Exception:
                pass
        root.withdraw()
        self.tick()

    def set(self, state, msg="", hold=0):
        self.state, self.msg = state, msg
        self.until = time.time() + hold if hold else 0
        if state == "idle":
            self.root.withdraw()
        else:
            self.root.deiconify()

    def tick(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "level":
                    self.levels.append(min(1.0, val * 12))
                elif kind == "state":
                    self.set(*val) if isinstance(val, tuple) else self.set(val)
                elif kind == "quit":
                    self.root.destroy(); return
        except queue.Empty:
            pass
        if self.until and time.time() > self.until:
            self.set("idle")
        if self.state != "idle":
            self.draw()
        self.root.after(33, self.tick)

    def draw(self):
        c, W, H = self.c, self.W, self.H
        c.delete("all")
        c.create_oval(0, 0, H, H, fill=PILL, outline="")
        c.create_oval(W - H, 0, W, H, fill=PILL, outline="")
        c.create_rectangle(H // 2, 0, W - H // 2, H, fill=PILL, outline="")
        mid = H / 2
        if self.state == "listening":
            c.create_oval(16, mid - 5, 26, mid + 5, fill=DOT, outline="")
            step = (W - 62) / self.N
            for i, lv in enumerate(self.levels):
                h = 3 + lv * (H - 20)
                x = 42 + i * step
                c.create_line(x, mid - h / 2, x, mid + h / 2, width=3, fill=BAR, capstyle="round")
            self.levels.append(self.levels[-1] * 0.7)  # decay when silent
        elif self.state == "processing":
            t = time.time() * 4
            for i in range(3):
                r = 3 + 2 * (0.5 + 0.5 * np.sin(t - i * 0.9))
                x = W / 2 + (i - 1) * 16
                c.create_oval(x - r, mid - r, x + r, mid + r, fill=DIM, outline="")
        elif self.state == "done":
            c.create_line(W / 2 - 8, mid, W / 2 - 2, mid + 6, W / 2 + 9, mid - 6,
                          width=3, fill=OK, capstyle="round", joinstyle="round")
        elif self.state == "error":
            c.create_text(W / 2, mid, text=self.msg, fill=DOT, font=("Helvetica", 10))


# ---------------------------------------------------------------- app
class App:
    def __init__(self):
        self.cfg = load_config()
        self.q = queue.Queue()
        self.root = tk.Tk()
        self.overlay = Overlay(self.root, self.q)
        self.rec = Recorder(lambda l: self.q.put(("level", l)))
        self.stt, self.cleaner = Transcriber(self.cfg), Cleaner(self.cfg)
        self.recording = self.busy = False
        self.down = set()
        self.combos = [
            Combo(self.cfg["hotkey"], self.start, self.stop),
            Combo(self.cfg["hands_free_hotkey"], self.toggle),
        ]
        keyboard.Listener(on_press=self.on_press, on_release=self.on_release, daemon=True).start()
        threading.Thread(target=self.warm_up, daemon=True).start()
        self.start_tray()

    def warm_up(self):  # load the local model now so the first dictation is fast
        if self.cfg["stt"]["provider"] == "local":
            self.stt.run(np.zeros(SR, "float32"))

    def on_press(self, k):
        n = canon(k)
        if n:
            self.down.add(n)
            for c in self.combos: c.update(self.down)

    def on_release(self, k):
        self.down.discard(canon(k))
        for c in self.combos: c.update(self.down)

    def toggle(self):
        self.stop() if self.recording else self.start()

    def start(self):
        if self.recording or self.busy:
            return
        try:
            self.rec.start()
        except Exception as e:
            self.q.put(("state", ("error", "No microphone", 1.5))); print(e); return
        self.recording = True
        self.q.put(("state", "listening"))

    def stop(self):
        if not self.recording:
            return
        self.recording = False
        audio = self.rec.stop()
        threading.Thread(target=self.process, args=(audio,), daemon=True).start()

    def process(self, audio):
        self.busy = True
        try:
            if len(audio) < SR * 0.3 or np.sqrt(np.mean(audio ** 2)) < 0.003:
                self.q.put(("state", "idle")); return
            self.q.put(("state", "processing"))
            raw = self.stt.run(audio)
            if not raw:
                self.q.put(("state", "idle")); return
            text = self.cleaner.run(raw)
            self.q.put(("state", ("done", "", 0.7)))
            paste(text)
        except Exception as e:
            print("error:", e)
            self.q.put(("state", ("error", "Something failed", 1.8)))
        finally:
            self.busy = False

    def start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((2, 2, 62, 62), 16, fill=PILL)
            for i, h in enumerate((10, 24, 34, 20, 8)):
                x = 12 + i * 10
                d.rounded_rectangle((x, 32 - h // 2, x + 5, 32 + h // 2), 2, fill=BAR)

            def toggle_cleanup(icon, item):
                self.cfg["cleanup"]["enabled"] = not self.cfg["cleanup"]["enabled"]

            def open_cfg(icon, item):
                if SYSTEM == "Windows": os.startfile(CONFIG_PATH)
                else: subprocess.Popen(["open" if SYSTEM == "Darwin" else "xdg-open", str(CONFIG_PATH)])

            menu = pystray.Menu(
                pystray.MenuItem("AI cleanup", toggle_cleanup, checked=lambda i: self.cfg["cleanup"]["enabled"]),
                pystray.MenuItem("Open settings file", open_cfg),
                pystray.MenuItem("Quit", lambda i, it: (i.stop(), self.q.put(("quit", None)))),
            )
            self.tray = pystray.Icon("murmur", img, "Murmur", menu)
            self.tray.run_detached()
        except Exception as e:
            print("tray unavailable:", e)

    def run(self):
        print(f"Murmur running. Hold [{self.cfg['hotkey']}] to dictate, "
              f"or press [{self.cfg['hands_free_hotkey']}] for hands-free.")
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
