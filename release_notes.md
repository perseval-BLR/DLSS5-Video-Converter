## DLSS 5 Video Converter v0.1.2

Local web tool that runs video through **NVIDIA DLSS 5 Neural Rendering** (NGX feature 18, `nvngx_dlssnr.dll`) with optical-flow motion vectors and encodes the result with NVENC.

**Run:** unzip → `DLSS5VideoConverter.exe` → browser opens at `http://127.0.0.1:7860` → add a video → Render.

### v0.1.2 — fps fix, lossless, auto-exit

- **Fixed 60 fps videos being slowed to 30 fps** (regression from v0.1.1): the intermediate stream now uses the input's frame rate as its time base, so the original fps is preserved.
- **Lossless encoding** (Quality → "Lossless (NVENC)"): `-tune lossless` for H.264/HEVC NVENC (yuv444p), software fallback `-crf 0`. AV1 NVENC has no lossless mode — a clear error is shown instead.
- **Auto-exit when the browser tab is closed**: the page sends a heartbeat; if no tabs are left (30 s of silence) and no render is running, the app shuts itself down — no more killing it via Task Manager.
- **Exit button (⏻) in the header** with confirmation.

### v0.1.1 — crash fixes

- **Fixed a hard crash on videos whose time base is not 1/30** (most MP4s): the intermediate NUT stream used the input's time base, and the NUT muxer crashed while writing the trailer — the process died silently, leaving a truncated `processed-video.mkv` in `jobs/`. The stream now always uses 1/30 and PTS values are converted from the input scale.
- **Fixed `Invalid argument: '!?' returned 22`**: PyAV raised EINVAL on frames with invalid PTS (None / negative / NOPTS) that the decoder can emit after scene cuts. PTS is now sanitized and forced strictly monotonic.

### Features
- **Profiles:** Faithful / Natural / Strong-Cinematic / **Extreme-Overdrive** (intensity up to 3.0)
- **NR sliders:** Intensity, Local Tone, Local Structure, Skin Structure (0–3, skin from −1) — manual values override the profile
- **Encoding:** H.264 / HEVC / AV1 (NVENC, software fallback), MP4 / MKV, CRF 17/20/24
- **Split compare:** original vs result, two synced players, sound from the result
- **Metrics:** PSNR / SSIM on every 30th frame (in the JSON report)
- **UI:** RU/EN, dark/light theme, results feed with Download/JSON, README modal, drag&drop or Add button
- Original source video is kept in `originals/` for comparison

### Requirements
- Windows 10/11 64-bit
- NVIDIA **RTX 40/50** (RTX 30 — beta, very slow)
- Recent NVIDIA driver

### Honest limitations
- Render path is RGBA8 — HDR input is converted to SDR (not mislabeled as HDR)
- Optical-flow motion ≠ engine motion vectors: fast motion, occlusions, thin objects, cuts may show temporal artifacts
- `nvngx.dll` in `bin/runtime` is a modified shim (caller-check wrapper), not NVIDIA's NGX core; the bundled model is modified for standalone calls
- Every frame must return a successful feature-18 result or the render is rejected
- First run on 4K video may take 1–3 minutes for analysis (ffprobe decodes frames)

### Source
Full source in this repo: `app.py`, `dlss5_converter/`, `build_exe.bat` (PyInstaller), `native/` (worker sources).

Made by **Perseval** — https://youtube.com/@perseval_BLR

---

## RTX 40 Series (Ada) build

`DLSS5-Video-Converter-v0.1.1-RTX40-win64.zip` — same app with a community-patched `nvngx_dlssnr.dll` (Ada CUDA binaries) for GeForce RTX 40 Series. Experimental (Uncle Burrito / dev-camo patch): expect a large performance hit vs RTX 50 and possible visual artifacts. Do NOT use the patched DLL in online / anti-cheat-protected games. RTX 20/30 are NOT supported.
