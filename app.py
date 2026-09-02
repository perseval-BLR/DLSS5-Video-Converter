#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DLSS 5 Video Converter — веб-интерфейс (стиль NR Media UI).
Бэкенд: dlss5_converter.core (feature 18, optical flow, NVENC).
Запуск: start.bat  →  http://127.0.0.1:7860
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# PyInstaller exe: ассеты (bin/, outputs/, jobs/, originals/) лежат рядом с exe,
# а __file__ указывает на _MEIPASS (временная распаковка) — берём папку exe.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dlss5_converter.core import (
    ConversionOptions,
    FFMPEG,
    ORIGINALS,
    OUTPUTS,
    PROFILES,
    RUNTIME,
    cancel_active_job,
    convert_video,
    detect_gpu,
)

WORK = ROOT / "_work"
WORK.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)

# ── Превью кадра (NR Media CLI) ──
# dlssnr-image.exe + caller/nvngx.dll лежат в preview/ (из NR Media UI),
# nvngx_dlssnr.dll копируется туда при старте из bin/runtime (одна копия в зипе).
PREVIEW_DIR = ROOT / "preview"
PREVIEW_CLI = PREVIEW_DIR / "dlssnr-image.exe"
PREVIEW_DLL = PREVIEW_DIR / "nvngx_dlssnr.dll"
PREVIEW_READY = False


def _init_preview():
    global PREVIEW_READY
    try:
        if not PREVIEW_CLI.exists():
            return
        if not (PREVIEW_DIR / "caller" / "nvngx.dll").exists():
            return
        if not PREVIEW_DLL.exists():
            src = RUNTIME / "nvngx_dlssnr.dll"
            if not src.exists():
                return
            PREVIEW_DIR.mkdir(exist_ok=True)
            shutil.copy2(src, PREVIEW_DLL)
        PREVIEW_READY = True
    except Exception:
        PREVIEW_READY = False

# ── Heartbeat: авто-выход при закрытии вкладки/браузера ──
# Страница шлёт /api/heartbeat каждые 5 c. Если вкладок не осталось (heartbeat
# устарел) и рендер не идёт — сервер сам завершает процесс, чтобы exe не висел
# в памяти после закрытия браузера (жалоба: «приходится убивать через диспетчер»).
HEARTBEAT_LOCK = threading.Lock()
LAST_HEARTBEAT = time.time()
HEARTBEAT_TIMEOUT = 30  # секунд без heartbeat → выход (если рендер не идёт)


def _touch_heartbeat():
    global LAST_HEARTBEAT
    with HEARTBEAT_LOCK:
        LAST_HEARTBEAT = time.time()


def _heartbeat_stale() -> bool:
    with HEARTBEAT_LOCK:
        return time.time() - LAST_HEARTBEAT > HEARTBEAT_TIMEOUT

# ── Состояние рендера ──
STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "progress": 0.0,
    "message": "",
    "done": False,
    "ok": False,
    "error": "",
    "result": None,   # dict: output, report, frames, elapsed, gpu
    "cancel": False,
}


def _set_state(**kw):
    with STATE_LOCK:
        STATE.update(kw)


def _get_state():
    with STATE_LOCK:
        return dict(STATE)


def _run_render(input_path: str, options: ConversionOptions):
    _set_state(running=True, progress=0.0, message="Старт...", done=False, ok=False, error="", result=None, cancel=False)

    def report(value: float, message: str):
        _set_state(progress=value, message=message)

    try:
        result = convert_video(input_path, options, progress=report)
        _set_state(done=True, ok=True, progress=1.0, message="Готово",
                   result={
                       "output": result.output_path,
                       "report": result.report_path,
                       "frames": result.frames,
                       "elapsed": result.elapsed_seconds,
                       "gpu": result.gpu,
                   })
    except Exception as exc:
        _set_state(done=True, ok=False, error=str(exc), message="Ошибка")
    finally:
        _set_state(running=False)


# ── HTML (стиль NR Media UI) ──
HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>DLSS 5 Neural Rendering — видео</title>
<style>
:root {
  --bg: #0a0c10;
  --bg-2: #0e1116;
  --card: rgba(20, 24, 31, .72);
  --card-solid: #14181f;
  --border: rgba(255,255,255,.07);
  --border-strong: rgba(255,255,255,.12);
  --text: #eef1f6;
  --text-2: #aab3c2;
  --muted: #6b7484;
  --accent: #76b900;
  --accent-2: #a3e635;
  --accent-glow: rgba(118,185,0,.35);
  --danger: #f87171;
  --radius: 16px;
  --radius-sm: 10px;
  --shadow: 0 20px 60px rgba(0,0,0,.5);
  --font: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
  --mono: 'JetBrains Mono', 'Cascadia Code', 'Consolas', 'SF Mono', monospace;
  --surface: rgba(255,255,255,.04);
  --surface-strong: rgba(255,255,255,.1);
  --track: rgba(255,255,255,.1);
  --glow-soft: rgba(118,185,0,.1);
}
* { box-sizing:border-box; margin:0; padding:0; }
html { color-scheme: dark; }
body {
  background: var(--bg);
  color: var(--text);
  font: 14px/1.55 var(--font);
  min-height: 100vh;
  padding: 28px 24px 40px;
  background-image:
    radial-gradient(900px 500px at 15% -10%, rgba(118,185,0,.09), transparent 60%),
    radial-gradient(700px 400px at 90% 0%, rgba(163,230,53,.05), transparent 60%),
    radial-gradient(800px 600px at 50% 120%, rgba(30,41,59,.4), transparent 60%);
  background-attachment: fixed;
}
::selection { background: rgba(118,185,0,.3); }
.header { display: flex; align-items: center; justify-content: space-between; max-width: 1440px; margin: 0 auto 24px; gap: 16px; }
.logo { display: flex; align-items: center; gap: 14px; }
.logo-mark {
  width: 44px; height: 44px; border-radius: 12px; flex-shrink: 0;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 8px 24px var(--accent-glow), inset 0 1px 0 rgba(255,255,255,.25);
}
.logo-mark svg { width: 24px; height: 24px; }
.logo h1 { font-size: 17px; font-weight: 700; letter-spacing: -.01em; }
.logo .sub { font-size: 12px; color: var(--muted); margin-top: 1px; font-family: var(--mono); }
.header-right { display: flex; align-items: center; gap: 10px; }
.badge {
  font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  color: var(--accent-2); background: var(--glow-soft);
  border: 1px solid rgba(118,185,0,.25); border-radius: 999px; padding: 4px 10px;
  font-family: var(--mono);
}
.badge::before { content: '['; color: var(--muted); }
.badge::after { content: ']'; color: var(--muted); }
.grid { display: grid; grid-template-columns: 1fr 340px; gap: 20px; max-width: 1440px; margin: 0 auto; align-items: start; }
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 20px; backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  box-shadow: var(--shadow), inset 0 1px 0 var(--surface);
  position: relative;
}
.card::before {
  content: ''; position: absolute; inset: 0; border-radius: var(--radius); pointer-events: none;
  background: linear-gradient(160deg, var(--surface), transparent 40%);
}
.drop-title { font-size: 11px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); font-family: var(--mono); }
.drop-title::before { content: '['; color: var(--accent); }
.drop-title::after { content: ']'; color: var(--accent); }
.drop-sub { font-size: 12px; color: var(--muted); margin: 2px 0 12px; }
#drop {
  border: 1.5px dashed var(--border-strong); border-radius: var(--radius-sm);
  padding: 48px 24px; text-align: center; color: var(--muted); cursor: pointer;
  transition: all .25s ease; position: relative; overflow: hidden;
}
#drop:hover { border-color: var(--accent); color: var(--accent); background: var(--glow-soft); }
#drop.drag { border-color: var(--accent); color: var(--accent); background: var(--glow-soft); transform: scale(1.005); }
body.page-drag #drop { border-color: var(--accent); color: var(--accent); background: var(--glow-soft); transform: scale(1.005); }
body.page-drag::after { content: 'DROP'; position: fixed; inset: 0; z-index: 9999; pointer-events: none;
  border: 3px dashed var(--accent); background: rgba(0,0,0,.25); display: flex; align-items: center; justify-content: center;
  font-family: var(--mono); font-size: 28px; letter-spacing: .3em; color: var(--accent); }
#drop .drop-icon { font-size: 34px; margin-bottom: 10px; opacity: .7; transition: transform .25s; }
#drop .drop-icon svg { width: 40px; height: 40px; }
#drop .drop-label { font-size: 15px; font-weight: 600; color: var(--text); margin-bottom: 6px; }
#drop:hover .drop-icon { transform: translateY(-3px); }
#drop .hint { font-size: 13px; }
#drop .hint b { color: var(--text-2); font-weight: 600; }
#fileinfo { margin-top: 12px; font-size: 12px; color: var(--muted); font-family: var(--mono); display: none; }
#fileinfo b { color: var(--accent-2); }
.controls h2 { font-size: 11px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); margin: 22px 0 8px; font-family: var(--mono); }
.controls h2::before { content: '['; color: var(--accent); }
.controls h2::after { content: ']'; color: var(--accent); }
.controls h2:first-child { margin-top: 0; }
.controls label { display: flex; justify-content: space-between; align-items: center; margin: 12px 0 5px; font-size: 12.5px; color: var(--text-2); }
.controls label span { color: var(--text); font-weight: 600; min-width: 44px; text-align: right; font-variant-numeric: tabular-nums; font-size: 13px; font-family: var(--mono); }
.controls label span[data-i18n] { color: var(--text-2); font-weight: 500; min-width: 0; text-align: left; font-variant-numeric: normal; font-size: 12.5px; font-family: var(--font); }
select {
  width: 100%; background: var(--bg-2); color: var(--text); border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm); padding: 9px 12px; outline: none; font-size: 13px;
  transition: border-color .2s; cursor: pointer; appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%23aab3c2' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 12px center;
}
select:focus { border-color: var(--accent); }
.btn {
  width: 100%; margin-top: 18px; padding: 13px; border: none; border-radius: var(--radius-sm);
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #0a0c10; font-size: 14.5px; font-weight: 700; letter-spacing: .01em;
  cursor: pointer; transition: all .2s; box-shadow: 0 6px 20px var(--accent-glow);
  display: flex; align-items: center; justify-content: center; gap: 8px;
}
.btn:hover { transform: translateY(-1px); box-shadow: 0 10px 28px var(--accent-glow); }
.btn:active { transform: translateY(0) scale(.99); }
.btn:disabled { opacity: .5; cursor: wait; transform: none; box-shadow: none; }
.btn svg { width: 16px; height: 16px; }
.btn2 {
  flex: 1; padding: 9px 10px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm);
  background: var(--surface); color: var(--text-2); cursor: pointer; font-size: 12.5px;
  font-weight: 500; transition: all .2s; display: flex; align-items: center; justify-content: center; gap: 5px;
  font-family: var(--mono);
}
.btn2:hover { border-color: var(--accent); color: var(--accent); background: var(--glow-soft); }
.btn2:disabled { opacity: .4; cursor: wait; }
.btn2 svg { width: 13px; height: 13px; }
.btn-danger {
  flex: 1; padding: 9px 10px; border: 1px solid rgba(248,113,113,.25); border-radius: var(--radius-sm);
  background: rgba(248,113,113,.06); color: var(--danger); cursor: pointer; font-size: 12.5px;
  font-weight: 500; transition: all .2s; display: flex; align-items: center; justify-content: center; gap: 5px;
  font-family: var(--mono);
}
.btn-danger:hover { background: rgba(248,113,113,.14); border-color: var(--danger); }
.btn-danger svg { width: 13px; height: 13px; }
.row { display: flex; gap: 8px; margin-top: 10px; }
.divider-row { height: 1px; background: var(--border); margin: 12px 0; }
input[type=file] { display: none; }
.status {
  margin-top: 12px; font-size: 12px; color: var(--muted); min-height: 18px; white-space: pre-wrap;
  font-family: var(--mono); border-top: 1px solid var(--border); padding-top: 10px;
}
.status::before { content: '> '; color: var(--accent); font-weight: 700; }
.progress { margin-top: 12px; height: 6px; border-radius: 3px; background: var(--track); overflow: hidden; display: none; }
.progress .bar { height: 100%; width: 0%; background: linear-gradient(90deg, var(--accent), var(--accent-2)); transition: width .3s; }
.metrics { margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
.metric {
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 6px 12px; font-size: 12px; color: var(--muted); font-family: var(--mono);
}
.metric b { color: var(--accent-2); font-weight: 700; }
.results-title { font-size: 11px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); font-family: var(--mono); margin-bottom: 10px; }
.results-title::before { content: '['; color: var(--accent); }
.results-title::after { content: ']'; color: var(--accent); }
#results { display: flex; flex-direction: column; gap: 10px; }
.result-item {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 12px 14px; display: flex; align-items: center; justify-content: space-between; gap: 10px;
}
.result-item .r-name { font-size: 12.5px; font-weight: 600; color: var(--text); word-break: break-all; }
.result-item .r-meta { font-size: 11px; color: var(--muted); font-family: var(--mono); margin-top: 2px; }
.result-item .r-actions { display: flex; gap: 6px; flex-shrink: 0; }
.result-item .r-actions a, .result-item .r-actions button {
  padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer;
  border: 1px solid var(--border-strong); background: var(--surface); color: var(--text-2);
  text-decoration: none; font-family: var(--mono); transition: all .2s;
}
.result-item .r-actions a:hover, .result-item .r-actions button:hover { border-color: var(--accent); color: var(--accent); }
.result-item .r-actions .dl { border-color: rgba(118,185,0,.35); color: var(--accent-2); background: var(--glow-soft); }
.result-item .r-actions .dl:hover { box-shadow: 0 0 16px rgba(118,185,0,.15); }
.result-item .r-actions .cmp { border-color: rgba(163,230,53,.3); color: var(--accent); }
.result-item .r-actions .cmp:hover { background: rgba(163,230,53,.1); box-shadow: 0 0 16px rgba(118,185,0,.15); }
.empty { font-size: 12px; color: var(--muted); font-family: var(--mono); padding: 20px 0; text-align: center; }
.error-box {
  margin-top: 12px; padding: 10px 12px; border-radius: var(--radius-sm);
  background: rgba(248,113,113,.08); border: 1px solid rgba(248,113,113,.25);
  color: var(--danger); font-size: 12px; font-family: var(--mono); white-space: pre-wrap; display: none;
}
video.preview { width: 100%; max-height: 52vh; border-radius: var(--radius-sm); margin-top: 14px; display: none; box-shadow: 0 12px 40px rgba(0,0,0,.55); }
.help-btn {
  display: flex; align-items: center; gap: 6px;
  padding: 8px 14px; border: 1px solid var(--border-strong); border-radius: 999px;
  background: var(--surface); color: var(--text-2);
  font-size: 13px; font-weight: 500; cursor: pointer; transition: all .2s;
}
.help-btn:hover { border-color: var(--accent); color: var(--accent); background: var(--glow-soft); }
.footer {
  max-width: 1440px; margin: 24px auto 0; padding-top: 16px;
  border-top: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  font-size: 12.5px; color: var(--muted);
}
.footer-made b { color: var(--accent-2); font-weight: 700; }
.footer-link { color: var(--text-2); text-decoration: none; transition: color .2s; }
.footer-link:hover { color: var(--accent); text-decoration: underline; }
/* ── Светлая тема ── */
body[data-theme="light"] {
  --bg: #f2f4f7;
  --bg-2: #e9edf1;
  --card: rgba(255,255,255,.8);
  --card-solid: #ffffff;
  --border: rgba(0,0,0,.08);
  --border-strong: rgba(0,0,0,.14);
  --text: #1a1d23;
  --text-2: #4a5260;
  --muted: #7a8394;
  --accent: #5a9400;
  --accent-2: #4d7c0f;
  --accent-glow: rgba(90,148,0,.25);
  --danger: #dc2626;
  --shadow: 0 20px 60px rgba(0,0,0,.12);
  --surface: rgba(0,0,0,.04);
  --surface-strong: rgba(0,0,0,.08);
  --track: rgba(0,0,0,.12);
  --glow-soft: rgba(90,148,0,.08);
  background-image:
    radial-gradient(900px 500px at 15% -10%, rgba(90,148,0,.07), transparent 60%),
    radial-gradient(700px 400px at 90% 0%, rgba(77,124,15,.04), transparent 60%),
    radial-gradient(800px 600px at 50% 120%, rgba(30,41,59,.06), transparent 60%);
}
body[data-theme="light"] .btn { color: #fff; }
body[data-theme="light"] .logo-mark svg { stroke: #fff; }
/* ── README modal ── */
.modal-overlay {
  position: fixed; inset: 0; z-index: 10000;
  background: rgba(0,0,0,.6); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
  display: none; align-items: center; justify-content: center; padding: 24px;
}
.modal {
  max-width: 720px; width: 100%; max-height: 80vh; overflow-y: auto;
  background: var(--card-solid); border: 1px solid var(--border-strong);
  border-radius: 16px; padding: 24px; box-shadow: var(--shadow);
}
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.modal-head span { font-size: 15px; font-weight: 700; letter-spacing: .02em; }
.modal-close { padding: 4px 12px; font-size: 16px; line-height: 1; }
/* ── Compare (ДО/ПОСЛЕ) modal ── */
.compare-overlay {
  position: fixed; inset: 0; z-index: 10000;
  background: rgba(0,0,0,.6); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
  display: none; align-items: center; justify-content: center; padding: 24px;
}
.compare-modal {
  max-width: 1280px; width: 100%; max-height: 92vh; overflow-y: auto;
  background: var(--card-solid); border: 1px solid var(--border-strong);
  border-radius: 16px; padding: 20px 24px; box-shadow: var(--shadow);
}
.compare-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.compare-head .c-title { font-size: 15px; font-weight: 700; letter-spacing: .02em; }
.compare-head .c-time { font-family: var(--mono); font-size: 13px; color: var(--accent-2); font-variant-numeric: tabular-nums; min-width: 110px; text-align: center; }
.compare-btn {
  padding: 8px 18px; border: 1px solid var(--border-strong); border-radius: 999px;
  background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: #0a0c10;
  font-size: 13px; font-weight: 700; cursor: pointer; transition: all .2s;
  display: flex; align-items: center; gap: 6px;
}
.compare-btn:hover { box-shadow: 0 6px 20px var(--accent-glow); }
.compare-btn svg { width: 14px; height: 14px; }
body[data-theme="light"] .compare-btn { color: #fff; }
.compare-grid { display: flex; gap: 14px; flex-wrap: wrap; }
.compare-col { flex: 1 1 320px; min-width: 280px; }
.compare-lbl {
  font-family: var(--mono); font-size: 11px; font-weight: 700; letter-spacing: .08em;
  text-transform: uppercase; color: var(--muted); margin-bottom: 6px;
}
.compare-lbl b { color: var(--accent-2); }
.compare-lbl:first-child b { color: var(--muted); border: 1px solid var(--border-strong); border-radius: 6px; padding: 1px 6px; }
.compare-video { width: 100%; border-radius: var(--radius-sm); background: #000; border: 1px solid var(--border); }
</style>
</head>
<body>
<div class="header">
  <div class="logo">
    <div class="logo-mark">
      <svg viewBox="0 0 24 24" fill="none" stroke="#0a0c10" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/>
        <circle cx="12" cy="12" r="3.2"/>
      </svg>
    </div>
    <div>
      <h1>DLSS 5 Video Converter</h1>
      <div class="sub">feature 18 · optical flow · NVENC</div>
    </div>
  </div>
  <div class="header-right">
    <div class="badge" id="gpu-badge">GPU: ...</div>
    <button class="help-btn" onclick="toggleLang()" id="langBtn">EN</button>
    <button class="help-btn" onclick="toggleTheme()" id="themeBtn">☀</button>
    <button class="help-btn" onclick="toggleReadme()" id="readmeBtn">README</button>
    <button class="help-btn" onclick="exitApp()" id="exitBtn" title="Exit">⏻</button>
  </div>
</div>

<div class="grid">
  <div class="card">
    <div class="drop-title" data-i18n="input">Input</div>
    <div class="drop-sub" data-i18n="inputSub">видео прогоняется через DLSS 5 Neural Rendering (feature 18)</div>
    <div id="drop">
      <div class="drop-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 16V4M12 4l-4 4M12 4l4 4"/>
          <path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>
        </svg>
      </div>
      <div class="drop-label" data-i18n="addVideo">Добавить видео</div>
      <div class="hint" data-i18n="addHint">mp4 / mkv / webm · клик для выбора</div>
      <input type="file" id="file" accept="video/*">
    </div>
    <div id="fileinfo"></div>
    <video class="preview" id="preview" controls></video>
    <div class="row" id="preview-row" style="display:none">
      <button class="btn2" id="preview-btn" data-i18n="preview">Превью кадра</button>
      <span class="hint" id="preview-hint"></span>
    </div>
    <div id="preview-box" style="display:none">
      <div class="compare-lbl"><b data-i18n="previewBefore">ДО</b> <span data-i18n="previewAfter">ПОСЛЕ NR</span></div>
      <div class="compare-grid">
        <div class="compare-col"><img id="preview-src" alt="source frame"></div>
        <div class="compare-col"><img id="preview-out" alt="NR result"></div>
      </div>
    </div>
    <div class="status" id="status" data-i18n="statusWait">Ожидание файла</div>
    <div class="progress" id="progress"><div class="bar" id="bar"></div></div>
    <div class="error-box" id="error"></div>
    <div class="metrics" id="metrics"></div>
  </div>

  <div class="card controls">
    <h2 data-i18n="profile">Profile</h2>
    <select id="profile">
      <option value="Strong / Cinematic" selected>Strong / Cinematic</option>
      <option value="Extreme / Overdrive">Extreme / Overdrive</option>
      <option value="Natural">Natural</option>
      <option value="Faithful">Faithful</option>
    </select>
    <h2 data-i18n="nrParams">NR Parameters</h2>
    <label><span data-i18n="intensity">Intensity</span> <span id="v-intensity">1.65</span></label>
    <input type="range" id="intensity" min="0" max="3" step="0.05" value="1.65">
    <label><span data-i18n="tone">Local Tone</span> <span id="v-tone">1.40</span></label>
    <input type="range" id="tone" min="0" max="3" step="0.05" value="1.40">
    <label><span data-i18n="structure">Local Structure</span> <span id="v-structure">1.50</span></label>
    <input type="range" id="structure" min="0" max="3" step="0.05" value="1.50">
    <label><span data-i18n="skin">Skin Structure</span> <span id="v-skin">1.00</span></label>
    <input type="range" id="skin" min="-1" max="3" step="0.05" value="1.00">
    <div class="divider-row"></div>
    <h2 data-i18n="encoding">Encoding</h2>
    <label><span data-i18n="quality">Качество</span> <span id="q-val">High</span></label>
    <select id="quality">
      <option value="High" selected>High (CRF 17)</option>
      <option value="Balanced">Balanced (CRF 20)</option>
      <option value="Small">Small (CRF 24)</option>
      <option value="Lossless">Lossless (NVENC)</option>
    </select>
    <label><span data-i18n="codec">Кодек</span> <span id="c-val">H.264</span></label>
    <select id="codec">
      <option value="H.264" selected>H.264 (NVENC)</option>
      <option value="HEVC">HEVC (NVENC)</option>
      <option value="AV1">AV1 (NVENC)</option>
    </select>
    <label><span data-i18n="container">Контейнер</span> <span id="k-val">MP4</span></label>
    <select id="container">
      <option value="MP4" selected>MP4</option>
      <option value="MKV">MKV</option>
    </select>
    <div class="divider-row"></div>
    <button class="btn" id="render" disabled>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3l14 9-14 9V3z"/></svg>
      <span data-i18n="render">Render whole video</span>
    </button>
    <div class="row">
      <button class="btn-danger" id="stop" disabled>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6h12v12H6z"/></svg>
        <span data-i18n="stop">Stop</span>
      </button>
      <button class="btn2" id="clear">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg>
        <span data-i18n="clear">Очистить</span>
      </button>
    </div>
  </div>
</div>

<div class="card" style="max-width:1440px;margin:20px auto 0;">
  <div class="results-title" data-i18n="results">Results</div>
  <div id="results"><div class="empty" data-i18n="emptyResults">Пока пусто — рендеры появятся здесь</div></div>
</div>

<footer class="footer">
  <span class="footer-made"><span data-i18n="madeBy">Сделано</span> <b>Perseval</b></span>
  <a class="footer-link" href="https://youtube.com/@perseval_BLR" target="_blank" rel="noopener">youtube.com/@perseval_BLR</a>
</footer>


<!-- README modal -->
<div class="modal-overlay" id="readme-modal" onclick="if (event.target === this) closeReadme()">
  <div class="modal">
    <div class="modal-head">
      <span>README</span>
      <button class="help-btn modal-close" onclick="closeReadme()" title="Close">×</button>
    </div>
    <pre id="readme-content" style="white-space:pre-wrap; font-family:var(--mono); font-size:12.5px; color:var(--text-2); line-height:1.6"></pre>
  </div>
</div>

<!-- Compare ДО/ПОСЛЕ modal -->
<div class="compare-overlay" id="compare-overlay" onclick="if (event.target === this) closeCompare()">
  <div class="compare-modal" id="compare-modal">
    <div class="compare-head">
      <span class="c-title" id="c-title"></span>
      <span class="c-time" id="c-time">0:00 / 0:00</span>
      <span style="display:flex;gap:10px;align-items:center;">
        <button class="compare-btn" id="c-btn">
          <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
          <span id="c-btn-label" data-i18n="comparePlay">Пуск</span>
        </button>
        <button class="help-btn modal-close" onclick="closeCompare()" title="Close">×</button>
      </span>
    </div>
    <div class="compare-grid">
      <div class="compare-col">
        <div class="compare-lbl"><b data-i18n="compareBefore">ДО</b> · <span id="c-name-1"></span></div>
        <video class="compare-video" id="cmp-video-1" muted playsinline></video>
      </div>
      <div class="compare-col">
        <div class="compare-lbl"><b data-i18n="compareAfter">ПОСЛЕ NR</b> · <span id="c-name-2"></span></div>
        <video class="compare-video" id="cmp-video-2" playsinline></video>
      </div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let currentFile = null;

// ── Диагностика: любая JS-ошибка видна в статус-баре ──
window.addEventListener('error', e => {
  const st = document.getElementById('status');
  if (st) st.textContent = 'JS ERROR: ' + e.message + ' @ ' + (e.lineno || '?');
});
window.addEventListener('unhandledrejection', e => {
  const st = document.getElementById('status');
  if (st) st.textContent = 'JS PROMISE ERROR: ' + (e.reason && e.reason.message || e.reason);
});

// ── GPU-баннер ──
fetch('/api/gpu').then(r => r.json()).then(d => {
  $('gpu-badge').textContent = d.ok ? (d.name + ' · ' + d.driver) : ('GPU: ' + (d.error || 'нет'));
}).catch(() => {});

// ── Drop-зона ──
const drop = $('drop'), fileInput = $('file');
drop.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => { if (fileInput.files[0]) setFile(fileInput.files[0]); });
['dragenter','dragover'].forEach(ev => document.addEventListener(ev, e => { e.preventDefault(); document.body.classList.add('page-drag'); }));
['dragleave','drop'].forEach(ev => document.addEventListener(ev, e => { e.preventDefault(); document.body.classList.remove('page-drag'); }));
document.addEventListener('drop', e => {
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) setFile(f);
});
function setFile(f) {
  currentFile = f;
  $('fileinfo').style.display = 'block';
  $('fileinfo').innerHTML = '<b>' + f.name + '</b> · ' + (f.size/1048576).toFixed(1) + ' MB';
  // Видео показываем сразу — по нему перематываем и берём кадр для превью
  $('preview').src = URL.createObjectURL(f);
  $('preview').style.display = 'block';
  $('render').disabled = false;
  $('status').textContent = t('statusReady') + ': ' + f.name;
  $('error').style.display = 'none';
  // Превью: показываем кнопку, путь файла узнаём при первом клике (upload)
  previewPath = null;
  $('preview-box').style.display = 'none';
  $('preview-hint').textContent = '';
  showPreviewRow();
}

// ── NR-параметры: слайдеры + профили ──
const PROFILES = {
  'Faithful':          { intensity: 0.70, tone: 0.75, structure: 0.75, skin: -1.0 },
  'Natural':           { intensity: 1.00, tone: 1.00, structure: 1.00, skin: -1.0 },
  'Strong / Cinematic':{ intensity: 1.65, tone: 1.40, structure: 1.50, skin: 1.0 },
  'Extreme / Overdrive':{ intensity: 2.50, tone: 2.00, structure: 2.00, skin: 1.5 },
};
const NR_IDS = { intensity: 'v-intensity', tone: 'v-tone', structure: 'v-structure', skin: 'v-skin' };
function setSlider(id, val) {
  $(id).value = val;
  $(NR_IDS[id]).textContent = (+val).toFixed(2);
}
$('profile').addEventListener('change', () => {
  const p = PROFILES[$('profile').value];
  if (p) { setSlider('intensity', p.intensity); setSlider('tone', p.tone); setSlider('structure', p.structure); setSlider('skin', p.skin); }
});
['intensity','tone','structure','skin'].forEach(id => {
  $(id).addEventListener('input', () => { $(NR_IDS[id]).textContent = (+$(id).value).toFixed(2); });
});

// ── Рендер ──
let uploadAbort = null;
$('render').addEventListener('click', async () => {
  if (!currentFile) return;
  $('render').disabled = true; $('stop').disabled = false;
  $('error').style.display = 'none'; $('metrics').innerHTML = '';
  $('progress').style.display = 'block'; $('bar').style.width = '0%';
  $('status').textContent = t('statusUpload');
  const fd = new FormData();
  fd.append('file', currentFile);
  uploadAbort = new AbortController();
  try {
    const up = await fetch('/api/upload', { method: 'POST', body: fd, signal: uploadAbort.signal });
    const upj = await up.json();
    if (!upj.ok) throw new Error(upj.error || 'upload failed');
    $('status').textContent = t('statusStart');
    const r = await fetch('/api/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: upj.path,
        profile: $('profile').value,
        quality: $('quality').value,
        codec: $('codec').value,
        container: $('container').value,
        intensity: +$('intensity').value,
        local_tone: +$('tone').value,
        local_structure: +$('structure').value,
        skin_structure: +$('skin').value,
      })
    });
    const rj = await r.json();
    if (!rj.ok) throw new Error(rj.error || 'render start failed');
    poll();
  } catch (e) {
    if (e.name === 'AbortError') {
      // Upload отменён кнопкой Stop — не показываем ошибку
      $('status').textContent = t('statusStopped');
      $('render').disabled = !currentFile;
      $('stop').disabled = true;
      $('progress').style.display = 'none';
    } else {
      fail(e.message);
    }
  }
});

function poll() {
  fetch('/api/status').then(r => r.json()).then(s => {
    if (s.running) {
      $('bar').style.width = Math.round(s.progress * 100) + '%';
      $('status').textContent = translateStatus(s.message);
      setTimeout(poll, 500);
    } else if (s.done) {
      $('progress').style.display = 'none';
      $('stop').disabled = true;
      if (s.ok) {
        $('status').textContent = t('statusDone') + ': ' + s.result.frames + ' frames in ' + s.result.elapsed.toFixed(1) + 's on ' + s.result.gpu;
        $('metrics').innerHTML =
          '<div class="metric">frames <b>' + s.result.frames + '</b></div>' +
          '<div class="metric">time <b>' + s.result.elapsed.toFixed(1) + 's</b></div>' +
          '<div class="metric">gpu <b>' + s.result.gpu + '</b></div>';
        $('preview').src = '/outputs/' + encodeURIComponent(s.result.output.split(/[\\/]/).pop());
        $('preview').style.display = 'block';
        loadResults();
      } else if (s.error) {
        fail(s.error);
      } else {
        // Отменено пользователем (cancel) — без ошибки
        $('status').textContent = t('statusStopped');
        $('render').disabled = !currentFile;
      }
    } else {
      // Не running и не done — рендер отменён/сброшен: приводим UI в порядок
      $('progress').style.display = 'none';
      $('stop').disabled = true;
      $('render').disabled = !currentFile;
      $('status').textContent = t('statusStopped');
    }
  }).catch(() => setTimeout(poll, 1000));
}

function fail(msg) {
  $('render').disabled = !currentFile; $('stop').disabled = true;
  $('progress').style.display = 'none';
  $('error').textContent = msg;
  $('error').style.display = 'block';
  $('status').textContent = t('statusError');
}

$('stop').addEventListener('click', () => {
  // Отменяем upload, если он идёт (рендер ещё не стартовал)
  if (uploadAbort) { uploadAbort.abort(); uploadAbort = null; }
  // Отменяем рендер, если он идёт
  fetch('/api/cancel', { method: 'POST' });
  $('stop').disabled = true;
  $('status').textContent = t('statusStop');
});

// ── Превью кадра (NR Media CLI, фото-пайплайн) ──
// «Кое-как»: кадр выдёргивается из видео и прогоняется без temporal-контекста,
// но для подбора ползунков достаточно. Debounce 400 мс.
let previewTimer = null;
let previewBusy = false;
let previewPath = null;

function showPreviewRow() {
  $('preview-row').style.display = 'flex';
}

function runPreview() {
  if (previewBusy || !currentFile) return;
  const t = $('preview').currentTime || 0;
  const doPreview = (path) => {
    previewPath = path;
    previewBusy = true;
    $('preview-btn').disabled = true;
    $('preview-hint').textContent = '...';
    fetch('/api/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: path,
        time: t,
        profile: $('profile').value,
        intensity: +$('intensity').value,
        tone: +$('tone').value,
        structure: +$('structure').value,
        skin: +$('skin').value,
      })
    }).then(r => r.json()).then(d => {
      previewBusy = false;
      $('preview-btn').disabled = false;
      if (d.ok) {
        $('preview-box').style.display = 'block';
        $('preview-out').src = d.image;
        // Фото-CLI принимает параметры только [0,2] — если ползунки выше,
        // превью клампится; честно говорим об этом
        const clamped = [+$('intensity').value, +$('tone').value, +$('structure').value].some(v => v > 2) || +$('skin').value > 2;
        $('preview-hint').textContent = d.elapsed + 's' + (clamped ? ' (clamped to [0,2])' : '');
      } else {
        $('preview-hint').textContent = d.error || 'error';
      }
    }).catch(() => {
      previewBusy = false;
      $('preview-btn').disabled = false;
      $('preview-hint').textContent = 'network error';
    });
  };
  if (previewPath) {
    doPreview(previewPath);
  } else {
    // Первый клик: загружаем файл на сервер, чтобы ffmpeg мог выдернуть кадр
    const fd = new FormData();
    fd.append('file', currentFile);
    $('preview-hint').textContent = 'upload...';
    fetch('/api/upload', { method: 'POST', body: fd }).then(r => r.json()).then(up => {
      if (up.ok) doPreview(up.path);
      else $('preview-hint').textContent = up.error || 'upload failed';
    }).catch(() => { $('preview-hint').textContent = 'upload failed'; });
  }
}

$('preview-btn').addEventListener('click', runPreview);
// Ползунки → пере-превью с debounce
for (const id of ['intensity', 'tone', 'structure', 'skin', 'profile']) {
  $(id).addEventListener('input', () => {
    if (!previewPath) return;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(runPreview, 400);
  });
}
// Перемотка видео → пере-превью (только если превью уже открыто)
$('preview').addEventListener('seeked', () => {
  if (previewPath && $('preview-box').style.display === 'block') {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(runPreview, 400);
  }
});

// ── Выход из приложения (кнопка ⏻ в шапке) ──
function exitApp() {
  if (confirm(t('exitConfirm'))) {
    fetch('/api/exit', { method: 'POST' });
    setTimeout(() => { window.close(); }, 300);
  }
}

// ── Heartbeat: сообщаем серверу, что вкладка жива ──
// Если все вкладки закрыты, сервер сам завершит процесс (не висит в памяти).
setInterval(() => { fetch('/api/heartbeat', { method: 'POST' }).catch(() => {}); }, 5000);

// ── Результаты ──
function loadResults() {
  fetch('/api/results').then(r => r.json()).then(d => {
    const box = $('results');
    if (!d.items.length) { box.innerHTML = '<div class="empty">' + t('emptyResults') + '</div>'; return; }
    box.innerHTML = d.items.map(it => {
      const name = it.name.replace(/_DLSS5_.*[.](mp4|mkv)$/i, '');
      return '<div class="result-item">' +
        '<div><div class="r-name">' + it.name + '</div>' +
        '<div class="r-meta">' + (it.size/1048576).toFixed(1) + ' MB · ' + it.time + '</div></div>' +
        '<div class="r-actions">' +
        '<button class="cmp" onclick="openCompare(' + String.fromCharCode(39) + encodeURIComponent(it.name) + String.fromCharCode(39) + ')" title="' + t('compare') + '">◑</button>' +
        '<a class="dl" href="/outputs/' + encodeURIComponent(it.name) + '" download>' + t('download') + '</a>' +
        (it.report ? '<a href="/outputs/' + encodeURIComponent(it.report) + '" download>' + t('json') + '</a>' : '') +
        '</div></div>';
    }).join('');
  }).catch(() => {});
}
$('clear').addEventListener('click', () => {
  fetch('/api/clear', { method: 'POST' }).then(() => loadResults());
});
loadResults();

// ── i18n (RU/EN) ──
const I18N = {
  ru: {
    addVideo: 'Добавить видео', addHint: 'mp4 / mkv / webm · клик для выбора',
    input: 'Input', inputSub: 'видео прогоняется через DLSS 5 Neural Rendering (feature 18)',
    results: 'Результаты', emptyResults: 'Пока пусто — рендеры появятся здесь',
    madeBy: 'Сделано', render: 'Render whole video', stop: 'Stop', clear: 'Очистить',
    profile: 'Profile', nrParams: 'NR Parameters', encoding: 'Encoding',
    intensity: 'Intensity', tone: 'Local Tone', structure: 'Local Structure', skin: 'Skin Structure',
    quality: 'Качество', codec: 'Кодек', container: 'Контейнер',
    statusWait: 'Ожидание файла', statusReady: 'Файл выбран — жми Render',
    statusUpload: 'Загрузка файла...', statusStart: 'Запуск рендера...',
    statusDone: 'Готово', statusError: 'Ошибка', statusStop: 'Остановка...', statusStopped: 'Остановлено',
    download: 'Скачать', compare: 'Сравнить', json: 'JSON',
    compareDone: 'Сравнить ДО/ПОСЛЕ', compareBefore: 'ДО', compareAfter: 'ПОСЛЕ NR',
    compareNotFound: 'Оригинал не найден для этого рендера', comparePlay: 'Пуск', comparePause: 'Пауза',
    compareSync: 'Синхронизация недоступна — одно из видео не загрузилось',
    exitConfirm: 'Закрыть приложение? Рендер будет остановлен.',
    preview: 'Превью кадра', previewBefore: 'ДО', previewAfter: 'ПОСЛЕ NR',
  },
  en: {
    addVideo: 'Add video', addHint: 'mp4 / mkv / webm · click to select',
    input: 'Input', inputSub: 'video is processed through DLSS 5 Neural Rendering (feature 18)',
    results: 'Results', emptyResults: 'Nothing here yet — renders will appear',
    madeBy: 'Made by', render: 'Render whole video', stop: 'Stop', clear: 'Clear',
    profile: 'Profile', nrParams: 'NR Parameters', encoding: 'Encoding',
    intensity: 'Intensity', tone: 'Local Tone', structure: 'Local Structure', skin: 'Skin Structure',
    quality: 'Quality', codec: 'Codec', container: 'Container',
    statusWait: 'Waiting for a file', statusReady: 'File selected — hit Render',
    statusUpload: 'Uploading...', statusStart: 'Starting render...',
    statusDone: 'Done', statusError: 'Error', statusStop: 'Stopping...', statusStopped: 'Stopped',
    download: 'Download', compare: 'Compare', json: 'JSON',
    compareDone: 'Compare BEFORE/AFTER', compareBefore: 'BEFORE', compareAfter: 'AFTER NR',
    compareNotFound: 'No original found for this render', comparePlay: 'Play', comparePause: 'Pause',
    compareSync: 'Sync unavailable — one of the videos did not load',
    exitConfirm: 'Close the app? The render will be stopped.',
    preview: 'Frame preview', previewBefore: 'BEFORE', previewAfter: 'AFTER NR',
  },
};
let lang = 'ru';
function lsGet(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
lang = lsGet('dlss5v_lang', 'ru');
function t(k) { return (I18N[lang] || I18N.ru)[k] || k; }
// Статусы из core.py приходят на русском — переводим, если UI на EN
function translateStatus(msg) {
  if (lang !== 'en' || !msg) return msg;
  const map = [
    [/Анализ видео: декодирование кадров \(ffprobe\)\.\.\./, 'Analyzing video: decoding frames (ffprobe)...'],
    [/Видео: (\d+)x(\d+), (\d+) кадров — запуск feature 18\.\.\./, 'Video: $1x$2, $3 frames — starting feature 18...'],
    [/кадров за ([\d.]+)s на /, 'frames in $1s on '],
  ];
  for (const [re, to] of map) {
    if (re.test(msg)) return msg.replace(re, to);
  }
  return msg;
}
function applyLang() {
  const t = I18N[lang] || I18N.ru;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const k = el.getAttribute('data-i18n');
    if (t[k]) el.textContent = t[k];
  });
  $('langBtn').textContent = lang === 'ru' ? 'EN' : 'RU';
  document.documentElement.lang = lang;
  if (readmeOpen()) loadReadme();
}
function toggleLang() { lang = lang === 'ru' ? 'en' : 'ru'; lsSet('dlss5v_lang', lang); applyLang(); }

// ── README modal ──
function readmeOpen() { const el = $('readme-modal'); return !!el && el.style.display === 'flex'; }
function loadReadme() {
  fetch('/api/readme?lang=' + lang).then(r => r.json()).then(d => {
    const el = $('readme-content');
    if (el) el.textContent = d.ok ? d.content : (d.error || 'README unavailable');
  }).catch(() => { const el = $('readme-content'); if (el) el.textContent = 'README unavailable'; });
}
function openReadme() { const el = $('readme-modal'); if (el) { el.style.display = 'flex'; loadReadme(); } }
function closeReadme() { const el = $('readme-modal'); if (el) el.style.display = 'none'; }
function toggleReadme() { readmeOpen() ? closeReadme() : openReadme(); }
document.addEventListener('keydown', e => { if (e.key === 'Escape' && readmeOpen()) closeReadme(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && cmpOpen()) closeCompare(); });

// ── Compare (ДО/ПОСЛЕ, два синхронных плеера) ──
let cmpState = null; // {v1, v2, syncing, playing, timer}

function cmpOpen() { return $('compare-modal').style.display !== 'none'; }
function fmtCmpTime(s) { if (!isFinite(s) || s <= 0) return '0:00'; s = Math.floor(s); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
function cmpTimer() {
  if (!cmpState) return;
  const v = cmpState.v1;
  if (v && isFinite(v.duration) && v.duration > 0) {
    $('c-time').textContent = fmtCmpTime(v.currentTime) + ' / ' + fmtCmpTime(v.duration);
  }
}
function syncOthers(src, cur) {
  // Мастер-ведомый: синхронизирует ТОЛЬКО v1 (оригинал, muted).
  // v2 (результат, звук) следует за мастером, но НЕ синхронизирует обратно —
  // иначе timeupdate обоих создаёт бесконечную петлю (дёрганье, хрип, зависание).
  if (!cmpState || src !== cmpState.v1) return;
  const other = cmpState.v2;
  if (!other || !isFinite(other.duration) || other.duration <= 0) return;
  const drift = Math.abs(other.currentTime - cur);
  if (drift > 0.25) { // порог: не дёргаем на каждом тике, только при реальном расхождении
    other.currentTime = Math.min(cur, other.duration);
  }
}
function syncPlay(v) { if (cmpState) [cmpState.v1, cmpState.v2].forEach(x => { if (x && x !== v) x.play().catch(() => {}); }); }
function syncPause(v) { if (cmpState) [cmpState.v1, cmpState.v2].forEach(x => { if (x && x !== v) x.pause(); }); }
function closeCompare() {
  // Скрываем ОБА: и оверлей (blur-подложка), и модалку — иначе после закрытия
  // страница остаётся в блюре и «висит»
  const ov = $('compare-overlay');
  if (ov) ov.style.display = 'none';
  const modal = $('compare-modal');
  if (modal) modal.style.display = 'none';
  if (!cmpState) return;
  clearInterval(cmpState.timer);
  cmpState.v1.removeAttribute('src'); cmpState.v1.load();
  cmpState.v2.removeAttribute('src'); cmpState.v2.load();
  cmpState = null;
}
function openCompare(enc) {
  const name = decodeURIComponent(enc);
  fetch('/api/pair?output=' + encodeURIComponent(name)).then(r => r.json()).then(d => {
    if (!d.ok) { alert(t('compareNotFound') + ': ' + (d.error || '')); return; }
    const ov = $('compare-overlay');
    ov.style.display = 'flex';
    $('compare-modal').style.display = 'block';
    $('c-title').textContent = name;
    $('c-time').textContent = '0:00 / 0:00';
    const v1 = $('cmp-video-1'), v2 = $('cmp-video-2');
    v1.muted = true; v1.controls = true; v1.preload = 'metadata';
    v1.src = '/originals/' + encodeURIComponent(d.original);
    v2.controls = true; v2.preload = 'metadata';
    v2.src = '/outputs/' + encodeURIComponent(d.output);
    cmpState = { v1: v1, v2: v2, syncing: false, playing: false, timer: null };
    v1.currentTime = 0; v2.currentTime = 0;
    const setBtn = () => { $('c-btn-label').textContent = cmpState && cmpState.playing ? t('comparePause') : t('comparePlay'); };
    const onTime = e => {
      const src = e.target;
      if (!cmpState || cmpState.syncing || !src || !isFinite(src.currentTime) || !isFinite(src.duration)) return;
      cmpState.syncing = true;
      syncOthers(src, src.currentTime);
      cmpState.syncing = false;
    };
    v1.addEventListener('timeupdate', onTime);
    v2.addEventListener('timeupdate', onTime);
    const onPlay = e => {
      cmpState.playing = true; setBtn();
      syncPlay(e.target);
      if (!cmpState.timer) cmpState.timer = setInterval(cmpTimer, 250);
    };
    const onPause = e => {
      if (!cmpState) return;
      if (e.target.ended) { // конец одного — останавливаем оба
        cmpState.playing = false; setBtn();
        syncPause(e.target);
        clearInterval(cmpState.timer); cmpState.timer = null;
        return;
      }
      if (!cmpState.v1.paused || !cmpState.v2.paused) return;
      cmpState.playing = false; setBtn();
      clearInterval(cmpState.timer); cmpState.timer = null;
    };
    v1.addEventListener('play', onPlay);
    v2.addEventListener('play', onPlay);
    v1.addEventListener('pause', onPause);
    v2.addEventListener('pause', onPause);
    const onEnded = e => {
      if (!cmpState) return;
      cmpState.playing = false; setBtn();
      syncPause(e.target);
      clearInterval(cmpState.timer); cmpState.timer = null;
    };
    v1.addEventListener('ended', onEnded);
    v2.addEventListener('ended', onEnded);
    const onErr = () => { clearInterval(cmpState.timer); cmpState.timer = null; };
    v1.addEventListener('error', onErr);
    v2.addEventListener('error', onErr);
    $('c-btn').onclick = () => {
      if (!cmpState) return;
      const any = !cmpState.v1.paused || !cmpState.v2.paused;
      if (any) { cmpState.v1.pause(); cmpState.v2.pause(); }
      else {
        Promise.all([cmpState.v1.play().catch(() => {}), cmpState.v2.play().catch(() => {})]);
      }
    };
    cmpState.v1.play().catch(() => {
      // autoplay-политика: стартуем по клику на кнопку
      cmpState.v2.play().catch(() => {});
    });
  }).catch(() => alert(t('compareNotFound')));
}

// ── Тема (dark/light) ──
function toggleTheme() {
  const cur = document.body.dataset.theme || 'dark';
  const next = cur === 'dark' ? 'light' : 'dark';
  document.body.dataset.theme = next;
  $('themeBtn').textContent = next === 'dark' ? '☀' : '☾';
  lsSet('dlss5v_theme', next);
}
(function initTheme() {
  const theme = lsGet('dlss5v_theme', 'dark');
  document.body.dataset.theme = theme;
  $('themeBtn').textContent = theme === 'dark' ? '☀' : '☾';
})();
applyLang();
</script>

</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "DLSS5VideoUI/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _serve_video(self, target, ctype):
        """Отдача видео с поддержкой Range-запросов (обязательно для <video> стриминга:
        без неё браузер качает файл целиком, seek невозможен, compare виснет)."""
        size = target.stat().st_size
        range_header = self.headers.get("Range")
        if range_header:
            try:
                # Формат: bytes=start-end или bytes=start-
                spec = range_header.strip().split("=")[1]
                start_s, _, end_s = spec.partition("-")
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else size - 1
                if start < 0 or end >= size or start > end:
                    raise ValueError
            except Exception:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(target, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                        break  # браузер закрыл соединение (seek/перемотка) — не роняем сервер
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(target, "rb") as fh:
                try:
                    shutil.copyfileobj(fh, self.wfile)
                except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                    pass  # браузер закрыл соединение — не роняем сервер

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send(200, HTML, "text/html")
        elif parsed.path == "/api/gpu":
            try:
                g = detect_gpu()
                self._json(200, {"ok": True, "name": g["name"], "driver": g["driver"]})
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)})
        elif parsed.path == "/api/status":
            self._json(200, _get_state())
        elif parsed.path == "/api/results":
            items = []
            for f in sorted(OUTPUTS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if f.suffix.lower() in (".mp4", ".mkv"):
                    report = f.with_name(f.name + ".report.json")
                    items.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "time": time.strftime("%d.%m %H:%M", time.localtime(f.stat().st_mtime)),
                        "report": report.name if report.exists() else None,
                    })
            self._json(200, {"items": items})
        elif parsed.path == "/api/originals":
            items = []
            if ORIGINALS.is_dir():
                for f in sorted(ORIGINALS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                    if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".webm"):
                        items.append({
                            "name": f.name,
                            "size": f.stat().st_size,
                            "time": time.strftime("%d.%m %H:%M", time.localtime(f.stat().st_mtime)),
                        })
            self._json(200, {"items": items})
        elif parsed.path == "/api/pair":
            qs = urllib.parse.parse_qs(parsed.query)
            out = (qs.get("output") or [""])[0]
            if not out or "/" in out or "\\" in out:
                self._json(200, {"ok": False, "error": "bad output name"})
                return
            if "_DLSS5_" not in out:
                self._json(200, {"ok": False, "error": "not a DLSS5 result: " + out})
                return
            stamp = out.rsplit("_DLSS5_", 1)[1].rsplit(".", 1)[0]
            if not stamp:
                self._json(200, {"ok": False, "error": "bad output name: " + out})
                return
            found = None
            if ORIGINALS.is_dir():
                for f in ORIGINALS.iterdir():
                    if not f.is_file():
                        continue
                    sfx = f.suffix.lower()
                    base = f.name[:-len(sfx)] if sfx else f.name
                    if base.endswith("_ORIGINAL_" + stamp):
                        found = f.name
                        break
            if found is None:
                self._json(200, {"ok": False, "error": "original not found for: " + out})
                return
            self._json(200, {"ok": True, "output": out, "original": found})
        elif parsed.path == "/api/readme":
            qs = urllib.parse.parse_qs(parsed.query)
            lang = (qs.get("lang") or ["ru"])[0]
            fname = "README.ru.md" if lang == "ru" else "README.md"
            fpath = ROOT / fname
            if not fpath.is_file():
                self._json(200, {"ok": False, "error": "file not found: " + fname})
                return
            try:
                content = fpath.read_text(encoding="utf-8")
            except Exception as exc:
                self._json(200, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, "content": content})
        elif parsed.path.startswith("/outputs/"):
            name = urllib.parse.unquote(parsed.path[len("/outputs/"):])
            target = (OUTPUTS / name).resolve()
            if not str(target).startswith(str(OUTPUTS.resolve())) or not target.is_file():
                self._send(404, "not found", "text/plain")
                return
            self._serve_video(target, "video/mp4" if target.suffix == ".mp4" else "video/x-matroska")
        elif parsed.path.startswith("/originals/"):
            name = urllib.parse.unquote(parsed.path[len("/originals/"):])
            target = (ORIGINALS / name).resolve()
            if not ORIGINALS.is_dir() or not str(target).startswith(str(ORIGINALS.resolve())) or not target.is_file():
                self._send(404, "not found", "text/plain")
                return
            if target.suffix.lower() == ".mkv":
                ctype = "video/x-matroska"
            elif target.suffix.lower() == ".webm":
                ctype = "video/webm"
            else:
                ctype = "video/mp4"
            self._serve_video(target, ctype)
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/upload":
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                self._json(400, {"ok": False, "error": "empty upload"})
                return
            # multipart/form-data
            ctype = self.headers.get("Content-Type", "")
            boundary = ctype.split("boundary=")[-1].strip().strip('"').encode()
            data = self.rfile.read(length)
            parts = data.split(b"--" + boundary)
            payload = None
            fname = None
            for part in parts:
                if b"filename=" in part[:2000]:
                    head, _, body = part.partition(b"\r\n\r\n")
                    body = body.rsplit(b"\r\n", 1)[0]
                    fname = head.split(b'filename="')[1].split(b'"')[0].decode("utf-8", "replace")
                    payload = body
                    break
            if payload is None or not fname:
                self._json(400, {"ok": False, "error": "no file part"})
                return
            safe = os.path.basename(fname)
            dest = WORK / (uuid.uuid4().hex[:8] + "_" + safe)
            with open(dest, "wb") as fh:
                fh.write(payload)
            self._json(200, {"ok": True, "path": str(dest), "size": len(payload)})
        elif parsed.path == "/api/preview":
            # Превью кадра: выдернуть кадр из видео → прогнать через NR Media CLI
            # с текущими ползунками → вернуть PNG. «Кое-как»: без temporal-контекста
            # (optical flow), но для подбора параметров достаточно.
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
            except Exception:
                self._json(400, {"ok": False, "error": "bad json"})
                return
            src = Path(body.get("path", ""))
            if not src.is_file():
                self._json(400, {"ok": False, "error": "file not found"})
                return
            if not PREVIEW_READY:
                self._json(200, {"ok": False, "error": "preview not available (dlssnr-image.exe missing)"})
                return
            st = _get_state()
            if st["running"]:
                self._json(200, {"ok": False, "error": "render is running — preview disabled"})
                return
            try:
                t_sec = float(body.get("time", 0))
                profile = body.get("profile", "Strong / Cinematic")
                native = dict(PROFILES.get(profile) or PROFILES["Strong / Cinematic"])
                # Фото-CLI (dlssnr-image.exe) принимает intensity/tone/structure
                # только [0, 2], skin [-1, 2] — рендер же допускает до 3.
                # Клампим для превью (иначе «Fatal: --intensity must be finite in [0, 2]»).
                for key, opt, lo, hi in (("intensity", "intensity", 0.0, 2.0),
                                         ("tone", "local_tone", 0.0, 2.0),
                                         ("structure", "local_structure", 0.0, 2.0),
                                         ("skin", "skin_structure", -1.0, 2.0)):
                    v = body.get(key)
                    if v is not None:
                        native[opt] = min(max(float(v), lo), hi)
                    else:
                        native[opt] = min(max(float(native[opt]), lo), hi)
                work = tempfile.mkdtemp(prefix="prev_", dir=WORK)
                in_png = os.path.join(work, "frame.png")
                out_png = os.path.join(work, "out.png")
                # Кадр из видео (точный seek по времени)
                r = subprocess.run(
                    [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                     "-ss", f"{t_sec:.3f}", "-i", str(src), "-frames:v", "1",
                     "-vf", "scale=1280:trunc(ow/a/2)*2", in_png],
                    capture_output=True, text=True, errors="replace", timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if r.returncode != 0 or not os.path.exists(in_png):
                    self._json(200, {"ok": False, "error": "frame extract failed: " + (r.stderr or "")[-300:]})
                    return
                cmd = [
                    str(PREVIEW_CLI), in_png, out_png,
                    "--preset", str(native["preset"]),
                    "--style", str(native["style"]),
                    "--intensity", str(native["intensity"]),
                    "--tone", str(native["local_tone"]),
                    "--structure", str(native["local_structure"]),
                    "--skin", str(native["skin_structure"]),
                    "--auto-mask", str(native["auto_mask"]),
                    "--diagnostics", "0",
                ]
                t0 = time.time()
                proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=180,
                                      cwd=str(PREVIEW_DIR),
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                dt = round(time.time() - t0, 2)
                if proc.returncode != 0 or not os.path.exists(out_png):
                    self._json(200, {"ok": False, "error": "NR failed: " + (proc.stderr or proc.stdout or "")[-300:]})
                    return
                with open(out_png, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                self._json(200, {"ok": True, "image": "data:image/png;base64," + b64, "elapsed": dt})
            except Exception as exc:
                self._json(200, {"ok": False, "error": str(exc)})
        elif parsed.path == "/api/render":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
            except Exception:
                self._json(400, {"ok": False, "error": "bad json"})
                return
            src = Path(body.get("path", ""))
            if not src.is_file():
                self._json(400, {"ok": False, "error": "file not found"})
                return
            st = _get_state()
            if st["running"]:
                self._json(409, {"ok": False, "error": "render already running"})
                return
            opts = ConversionOptions(
                profile=body.get("profile", "Strong / Cinematic"),
                quality=body.get("quality", "High"),
                codec=body.get("codec", "H.264"),
                container=body.get("container", "MP4"),
                intensity=body.get("intensity"),
                local_tone=body.get("local_tone"),
                local_structure=body.get("local_structure"),
                skin_structure=body.get("skin_structure"),
            )
            t = threading.Thread(target=_run_render, args=(str(src), opts), daemon=True)
            t.start()
            self._json(200, {"ok": True})
        elif parsed.path == "/api/cancel":
            cancel_active_job()
            self._json(200, {"ok": True})
        elif parsed.path == "/api/heartbeat":
            _touch_heartbeat()
            self._json(200, {"ok": True})
        elif parsed.path == "/api/exit":
            # Явный выход: завершаем процесс (кнопка «Выход» в шапке).
            threading.Timer(0.2, _shutdown_server).start()
            self._json(200, {"ok": True})
        elif parsed.path == "/api/clear":
            for f in OUTPUTS.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass
            self._json(200, {"ok": True})
        else:
            self._json(404, {"ok": False, "error": "not found"})


def _shutdown_server():
    """Останавливает сервер и завершает процесс (вызывается из /api/exit или watcher)."""
    try:
        server.shutdown()
    except Exception:
        pass
    os._exit(0)


def _heartbeat_watcher():
    """Фон: если вкладок нет (heartbeat устарел) и рендер не идёт — выходим."""
    while True:
        time.sleep(5)
        try:
            st = _get_state()
            if not st["running"] and _heartbeat_stale():
                _shutdown_server()
        except Exception:
            pass


def main():
    global server
    port = 7860
    _init_preview()
    try:
        print(f"* DLSS 5 Video Converter - http://127.0.0.1:{port}")
    except Exception:
        pass  # windowed exe: stdout может быть None или cp1251 без юникода
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    # Авто-открытие браузера (как в NR Media UI) — с задержкой, чтобы сервер успел подняться
    threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    threading.Thread(target=_heartbeat_watcher, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
