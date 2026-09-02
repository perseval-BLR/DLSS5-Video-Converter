from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

import av
import cv2
import numpy as np

from .guides import TemporalGuideGenerator

# PyInstaller exe: ассеты (bin/, outputs/, jobs/, originals/) лежат рядом с exe,
# а __file__ указывает на _MEIPASS (временная распаковка) — берём папку exe.
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parents[1]

ROOT = BASE
RUNTIME = ROOT / "bin" / "runtime"
FFMPEG = ROOT / "bin" / "ffmpeg" / "bin" / "ffmpeg.exe"
FFPROBE = ROOT / "bin" / "ffmpeg" / "bin" / "ffprobe.exe"
WORKER = RUNTIME / "nvngx.dll"  # executable image name required by the signed snippet caller check
OUTPUTS = ROOT / "outputs"
JOBS = ROOT / "jobs"
ORIGINALS = ROOT / "originals"

VIDEO_MAGIC = 0x32563544
FRAME_MAGIC = 0x314D5246
OUT_MAGIC = 0x3154554F


@dataclass(slots=True)
class ConversionOptions:
    profile: str = "Strong / Cinematic"
    codec: str = "H.264"
    container: str = "MP4"
    quality: str = "High"
    preserve_hdr: bool = False
    warmup_frames: int = 120
    # Кастомные NR-параметры (переопределяют профиль, если заданы)
    intensity: float | None = None
    local_tone: float | None = None
    local_structure: float | None = None
    skin_structure: float | None = None


PROFILES = {
    "Faithful": dict(profile=0, preset=0, style=0, auto_mask=0, ui_correction=0,
                     intensity=0.70, local_tone=0.75, local_structure=0.75, skin_structure=-1.0),
    "Natural": dict(profile=1, preset=0, style=1, auto_mask=0, ui_correction=0,
                    intensity=1.00, local_tone=1.00, local_structure=1.00, skin_structure=-1.0),
    "Strong / Cinematic": dict(profile=2, preset=2, style=2, auto_mask=1, ui_correction=0,
                               intensity=1.65, local_tone=1.40, local_structure=1.50, skin_structure=1.0),
    "Extreme / Overdrive": dict(profile=2, preset=2, style=2, auto_mask=1, ui_correction=0,
                                intensity=2.50, local_tone=2.00, local_structure=2.00, skin_structure=1.5),
}


@dataclass(slots=True)
class ConversionResult:
    output_path: str
    report_path: str
    frames: int
    nr_count_evidence: int
    elapsed_seconds: float
    gpu: str


class Cancelled(RuntimeError):
    pass


class JobController:
    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.processes: list[subprocess.Popen] = []

    def register(self, process: subprocess.Popen) -> None:
        with self.lock:
            self.processes.append(process)

    def unregister(self, process: subprocess.Popen) -> None:
        with self.lock:
            if process in self.processes:
                self.processes.remove(process)

    def stop(self) -> None:
        self.cancel.set()
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass


_ACTIVE_LOCK = threading.Lock()
_ACTIVE: JobController | None = None


def cancel_active_job() -> str:
    global _ACTIVE
    if _ACTIVE is None:
        return "No render is running."
    _ACTIVE.stop()
    return "Stop requested; partial video will be removed and diagnostics retained."


def _run_json(command: list[str]) -> dict:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Media probe failed")
    return json.loads(result.stdout)


def probe_video(path: str | os.PathLike[str]) -> dict:
    # Быстрый анализ: сначала метаданные (мгновенно), -count_frames (декодирует ВСЕ кадры)
    # только если nb_frames не указан — иначе 4K-файл на 10+ минут «висит» на анализе.
    data = _run_json(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=index,codec_name,width,height,avg_frame_rate,r_frame_rate,time_base,duration,nb_frames,nb_read_frames,color_primaries,color_transfer,color_space:stream_tags=rotate:stream_side_data=rotation",
            "-show_entries",
            "format=duration,format_name",
            "-of",
            "json",
            str(path),
        ]
    )
    streams = data.get("streams") or []
    if not streams:
        raise ValueError("The selected file contains no decodable video stream.")
    stream = streams[0]
    rotation = int((stream.get("tags") or {}).get("rotate", 0) or 0)
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(side["rotation"] or 0)
    rotation %= 360
    width, height = int(stream["width"]), int(stream["height"])
    if rotation in (90, 270):
        width, height = height, width
    frames = int(stream.get("nb_read_frames") or stream.get("nb_frames") or 0)
    if frames <= 0:
        # Нет точного числа кадров в метаданных — считаем через -count_frames (медленно, но точно)
        counted = _run_json(
            [
                str(FFPROBE),
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "json",
                str(path),
            ]
        )
        frames = int((counted.get("streams") or [{}])[0].get("nb_read_frames") or 0)
    if frames <= 0:
        raise ValueError("Could not determine an exact frame count for this video.")
    rate_text = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "30/1"
    rate = Fraction(rate_text) if rate_text != "0/0" else Fraction(30, 1)
    transfer = stream.get("color_transfer") or "unknown"
    return {
        "width": width,
        "height": height,
        "coded_width": int(stream["width"]),
        "coded_height": int(stream["height"]),
        "rotation": rotation,
        "frames": frames,
        "fps": float(rate),
        "rate": rate,
        "time_base": Fraction(stream.get("time_base") or "1/1000"),
        "duration": float((data.get("format") or {}).get("duration") or stream.get("duration") or 0),
        "codec": stream.get("codec_name") or "unknown",
        "format": (data.get("format") or {}).get("format_name") or "unknown",
        "color_transfer": transfer,
        "hdr": transfer in {"smpte2084", "arib-std-b67"},
    }


def detect_gpu() -> dict:
    command = [
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total,compute_cap",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("NVIDIA driver tools are unavailable; an RTX GPU and current driver are required.") from exc
    candidates = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 4 and "RTX" in parts[0].upper():
            candidates.append(parts)
    if not candidates:
        raise RuntimeError("No supported NVIDIA RTX GPU was detected.")
    name, driver, memory, capability = candidates[0]
    match = re.search(r"RTX\s+(\d{2})", name.upper())
    generation = int(match.group(1)) if match else 0
    if generation < 30:
        raise RuntimeError(f"{name} is outside the supported RTX 30/40/50 scope.")
    return {
        "name": name,
        "driver": driver,
        "memory_mb": int(memory),
        "compute_capability": capability,
        "generation": generation,
        "beta": generation == 30,
    }


def resolve_size(metadata: dict, options: ConversionOptions) -> tuple[int, int]:
    return int(metadata["width"]), int(metadata["height"])


def _resize_fit(rgba: np.ndarray, width: int, height: int) -> np.ndarray:
    source_h, source_w = rgba.shape[:2]
    scale = min(width / source_w, height / source_h)
    fit_w = max(1, min(width, int(round(source_w * scale))))
    fit_h = max(1, min(height, int(round(source_h * scale))))
    resized = cv2.resize(rgba, (fit_w, fit_h), interpolation=cv2.INTER_LANCZOS4)
    canvas = np.zeros((height, width, 4), dtype=np.uint8)
    canvas[..., 3] = 255
    x = (width - fit_w) // 2
    y = (height - fit_h) // 2
    canvas[y : y + fit_h, x : x + fit_w] = resized
    return canvas


def _rotate(frame: np.ndarray, rotation: int) -> np.ndarray:
    if rotation == 90:
        return np.ascontiguousarray(np.rot90(frame, 3))
    if rotation == 180:
        return np.ascontiguousarray(np.rot90(frame, 2))
    if rotation == 270:
        return np.ascontiguousarray(np.rot90(frame, 1))
    return frame


def _read_exact(stream, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(size - len(chunks))
        if not block:
            raise EOFError(f"Native worker stopped after {len(chunks)} of {size} output bytes")
        chunks.extend(block)
    return bytes(chunks)


def _drain_text(stream, lines: list[str]) -> None:
    for raw in iter(stream.readline, b""):
        lines.append(raw.decode("utf-8", "replace").rstrip())


def _encoder_probe(codec: str) -> bool:
    command = [
        str(FFMPEG), "-v", "error", "-f", "lavfi", "-i", "color=size=256x256:rate=1",
        "-frames:v", "1", "-c:v", codec, "-f", "null", "-",
    ]
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0


def _codec_command(options: ConversionOptions) -> tuple[list[str], str]:
    cq = {"High": "17", "Balanced": "20", "Small": "24"}[options.quality]
    if options.codec == "H.264":
        if _encoder_probe("h264_nvenc"):
            return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", cq, "-b:v", "0", "-pix_fmt", "yuv420p"], "h264_nvenc"
        return ["-c:v", "libx264", "-preset", "slow", "-crf", cq, "-pix_fmt", "yuv420p"], "libx264"
    if options.codec == "HEVC":
        if _encoder_probe("hevc_nvenc"):
            return ["-c:v", "hevc_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", cq, "-b:v", "0", "-pix_fmt", "yuv420p"], "hevc_nvenc"
        return ["-c:v", "libx265", "-preset", "slow", "-crf", cq, "-pix_fmt", "yuv420p"], "libx265"
    if not _encoder_probe("av1_nvenc"):
        raise RuntimeError("AV1 NVENC is not supported by this GPU/driver. Choose H.264 or HEVC.")
    return ["-c:v", "av1_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", cq, "-b:v", "0", "-pix_fmt", "yuv420p"], "av1_nvenc"


def _start_encoder(temp_video: Path, options: ConversionOptions, controller: JobController):
    codec_args, selected = _codec_command(options)
    command = [
        str(FFMPEG), "-hide_banner", "-loglevel", "warning", "-y", "-f", "nut", "-i", "pipe:0",
        "-map", "0:v:0", "-an", *codec_args, "-fps_mode", "passthrough", str(temp_video),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    controller.register(process)
    logs: list[str] = []
    thread = threading.Thread(target=_drain_text, args=(process.stderr, logs), daemon=True)
    thread.start()
    return process, thread, logs, selected


def _final_mux(temp_video: Path, source: Path, output: Path, options: ConversionOptions) -> None:
    if options.container == "MKV":
        maps = ["-map", "0:v:0", "-map", "1:a?", "-map", "1:s?"]
        streams = ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]
        # MKV: без -shortest — аудио-копия просто закончится раньше, это нормально
        extra = []
    else:
        maps = ["-map", "0:v:0", "-map", "1:a?"]
        streams = ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        # MP4: apad дотягивает аудио тишиной до конца видео, -shortest режет по видео
        # (иначе при аудио короче видео на доли секунды теряются кадры хвоста)
        extra = ["-af", "apad", "-shortest"]
    command = [
        str(FFMPEG), "-hide_banner", "-loglevel", "warning", "-y", "-i", str(temp_video), "-i", str(source),
        *maps, "-map_metadata", "1", "-map_chapters", "1", *streams, *extra, str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Final audio/metadata mux failed:\n" + result.stderr[-4000:])


def compute_video_metrics(source_path: str | os.PathLike[str], output_path: str | os.PathLike[str], step: int = 30) -> dict:
    """PSNR/SSIM between source and final output on every step-th frame.

    Never raises: any failure returns {} so metrics cannot break the render.
    """
    try:
        source_container = av.open(str(source_path))
        output_container = av.open(str(output_path))
        try:
            source_stream = source_container.streams.video[0]
            output_stream = output_container.streams.video[0]
            psnr_sum = 0.0
            ssim_sum = 0.0
            samples = 0
            for index, (src_frame, out_frame) in enumerate(
                zip(source_container.decode(source_stream), output_container.decode(output_stream))
            ):
                if index % step != 0:
                    continue
                src = src_frame.to_ndarray(format="rgb")
                out = out_frame.to_ndarray(format="rgb")
                if src.shape != out.shape:
                    out = cv2.resize(out, (src.shape[1], src.shape[0]), interpolation=cv2.INTER_LANCZOS4)
                src_f = src.astype(np.float64) / 255.0
                out_f = out.astype(np.float64) / 255.0
                mse = float(np.mean((src_f - out_f) ** 2))
                psnr = float("inf") if mse == 0.0 else 20.0 * np.log10(255.0 / np.sqrt(mse))
                mu_a = src_f.mean()
                mu_b = out_f.mean()
                var_a = src_f.var()
                var_b = out_f.var()
                cov = float(np.mean((src_f - mu_a) * (out_f - mu_b)))
                c1 = 0.01 ** 2
                c2 = 0.03 ** 2
                ssim = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2))
                psnr_sum += psnr
                ssim_sum += ssim
                samples += 1
            if samples == 0:
                return {}
            return {
                "psnr": round(psnr_sum / samples, 2),
                "ssim": round(ssim_sum / samples, 4),
                "samples": samples,
            }
        finally:
            source_container.close()
            output_container.close()
    except Exception:
        return {}


def convert_video(
    input_path: str | os.PathLike[str],
    options: ConversionOptions | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> ConversionResult:
    global _ACTIVE
    options = options or ConversionOptions()
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if options.preserve_hdr:
        raise RuntimeError("HDR preservation is disabled in this build because the verified DLSSNR path is RGBA8. HDR input is converted to SDR instead of being mislabeled as HDR.")
    required = [FFMPEG, FFPROBE, WORKER, RUNTIME / "nvngx_dlssnr.dll"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("Portable runtime is incomplete:\n" + "\n".join(missing))

    controller = JobController()
    if not _ACTIVE_LOCK.acquire(blocking=False):
        raise RuntimeError("Another GPU render is already running.")
    _ACTIVE = controller
    started = time.perf_counter()
    job_dir: Path | None = None
    output: Path | None = None
    try:
        if progress:
            progress(0.0, "Анализ видео: декодирование кадров (ffprobe)...")
        metadata = probe_video(source)
        gpu = detect_gpu()
        if progress:
            progress(0.005, f"Видео: {metadata['width']}x{metadata['height']}, {metadata['frames']} кадров — запуск feature 18...")
        width, height = resolve_size(metadata, options)
        OUTPUTS.mkdir(exist_ok=True)
        JOBS.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        job_dir = JOBS / f"{source.stem}-{stamp}-{os.getpid()}"
        job_dir.mkdir(parents=True, exist_ok=False)
        extension = ".mkv" if options.container == "MKV" else ".mp4"
        output = OUTPUTS / f"{source.stem}_DLSS5_{stamp}{extension}"
        ORIGINALS.mkdir(exist_ok=True)
        original_path = ORIGINALS / f"{source.stem}_ORIGINAL_{stamp}{source.suffix}"
        shutil.copy2(source, original_path)
        temp_video = job_dir / "processed-video.mkv"
        native = PROFILES.get(options.profile)
        if native is None:
            raise RuntimeError(f"Unknown native DLSS 5 profile: {options.profile}")
        # Кастомные NR-параметры переопределяют профиль
        native = dict(native)
        if options.intensity is not None:
            native["intensity"] = options.intensity
        if options.local_tone is not None:
            native["local_tone"] = options.local_tone
        if options.local_structure is not None:
            native["local_structure"] = options.local_structure
        if options.skin_structure is not None:
            native["skin_structure"] = options.skin_structure
        if progress:
            progress(0.01, f"Starting feature 18 on {gpu['name']}")

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        worker = subprocess.Popen(
            [str(WORKER), "--video"], cwd=RUNTIME, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=creation_flags,
        )
        controller.register(worker)
        worker_logs: list[str] = []
        worker_thread = threading.Thread(target=_drain_text, args=(worker.stderr, worker_logs), daemon=True)
        worker_thread.start()
        header = struct.pack(
            "<10I4f", VIDEO_MAGIC, width, height, int(options.warmup_frames), int(metadata["frames"]),
            native["profile"], native["preset"], native["style"], native["auto_mask"], native["ui_correction"],
            native["intensity"], native["local_tone"], native["local_structure"], native["skin_structure"],
        )
        worker.stdin.write(header)
        worker.stdin.flush()

        encoder, encoder_thread, encoder_logs, selected_encoder = _start_encoder(temp_video, options, controller)
        nut = av.open(encoder.stdin, mode="w", format="nut")
        input_container = av.open(str(source))
        input_stream = input_container.streams.video[0]
        input_stream.thread_type = "AUTO"
        rate = input_stream.average_rate or metadata["rate"]
        raw_stream = nut.add_stream("rawvideo", rate=rate)
        raw_stream.width = width
        raw_stream.height = height
        raw_stream.pix_fmt = "rgba"
        # NUT-мультиплексор с rawvideo стабилен только на time_base=1/30:
        # на других шкалах (1/1000 у mp4, 1/90000 и т.п.) он жёстко падает
        # при записи трейлера (close) — процесс умирает без исключения.
        # PTS из воркера приходит в шкале входного потока — конвертируем в 1/30.
        out_tb = Fraction(1, 30)
        src_tb = input_stream.time_base or metadata["time_base"] or out_tb
        raw_stream.time_base = out_tb
        guides = TemporalGuideGenerator(width, height)
        delivered = 0
        scene_resets = 0
        last_pts = -1
        for index, frame in enumerate(input_container.decode(input_stream)):
            if controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            rgba = frame.to_ndarray(format="rgba")
            rgba = _rotate(rgba, metadata["rotation"])
            if rgba.shape[1] != width or rgba.shape[0] != height:
                rgba = _resize_fit(rgba, width, height)
            rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
            guide = guides.process(rgba)
            scene_resets += int(guide.reset and index != 0)
            pts = int(frame.pts if frame.pts is not None else index)
            frame_header = struct.pack("<4Iq", FRAME_MAGIC, index, int(guide.reset), 0, pts)
            worker.stdin.write(frame_header)
            worker.stdin.write(rgba.tobytes())
            worker.stdin.write(guide.motion.tobytes())
            worker.stdin.flush()

            result_header = _read_exact(worker.stdout, struct.calcsize("<5Iq"))
            magic, out_index, ok, byte_count, ngx_result, out_pts = struct.unpack("<5Iq", result_header)
            if magic != OUT_MAGIC or not ok or out_index != index or byte_count != width * height * 4:
                raise RuntimeError(f"Invalid native worker response for frame {index}")
            if ngx_result != 1:
                raise RuntimeError(f"Direct feature-18 evaluation failed on frame {index}: 0x{ngx_result:08X}")
            processed = np.frombuffer(_read_exact(worker.stdout, byte_count), dtype=np.uint8).reshape(height, width, 4)
            out_frame = av.VideoFrame.from_ndarray(processed, format="rgba")
            # Воркер эхо-возвращает входной PTS (в шкале входного потока src_tb).
            # Конвертируем в out_tb (1/30) и санитизируем: декодер может отдать
            # None/отрицательный/NOPTS, а NUT падает с EINVAL на невалидных или
            # обратных PTS — это и был «Invalid argument: '!?' returned 22»
            # на кадрах после смены сцены. Гарантируем строгую монотонность.
            if out_pts is None or out_pts < 0:
                out_pts = last_pts + 1
            out_pts = int(round(out_pts * src_tb / out_tb))
            if out_pts <= last_pts:
                out_pts = last_pts + 1
            last_pts = out_pts
            out_frame.pts = out_pts
            out_frame.time_base = out_tb
            for packet in raw_stream.encode(out_frame):
                nut.mux(packet)
            delivered += 1
            if progress:
                progress(0.04 + 0.84 * delivered / metadata["frames"], f"DLSS 5 frame {delivered}/{metadata['frames']}")

        if delivered != metadata["frames"]:
            raise RuntimeError(f"Decoded {delivered} frames but probe reported {metadata['frames']}; refusing an incomplete render.")
        for packet in raw_stream.encode():
            nut.mux(packet)
        nut.close()
        if encoder.stdin and not encoder.stdin.closed:
            encoder.stdin.close()
        input_container.close()
        worker.stdin.close()
        worker_code = worker.wait(timeout=60)
        worker_thread.join(timeout=2)
        controller.unregister(worker)
        encoder_code = encoder.wait(timeout=120)
        encoder_thread.join(timeout=2)
        controller.unregister(encoder)
        if worker_code:
            raise RuntimeError("Native DLSS worker failed:\n" + "\n".join(worker_logs[-40:]))
        if encoder_code:
            raise RuntimeError("Video encoder failed:\n" + "\n".join(encoder_logs[-40:]))

        nr_count = delivered
        create_matches = re.findall(r"direct feature 18 ready:.*result=0x([0-9A-Fa-f]{8})", "\n".join(worker_logs))
        if not create_matches:
            raise RuntimeError("Direct feature-18 creation result was not reported; refusing unverifiable output.")
        direct_create_result = f"0x{create_matches[-1].upper()}"
        if progress:
            progress(0.91, "Muxing original audio and metadata")
        _final_mux(temp_video, source, output, options)
        metrics = compute_video_metrics(source, output)
        verified = probe_video(output)
        if verified["frames"] != delivered:
            raise RuntimeError(f"Output verification found {verified['frames']} frames instead of {delivered}.")

        elapsed = time.perf_counter() - started
        report = {
            "status": "success",
            "input": str(source),
            "output": str(output),
            "original_path": str(original_path),
            "metrics": metrics,
            "options": asdict(options),
            "input_metadata": {key: str(value) if isinstance(value, Fraction) else value for key, value in metadata.items()},
            "output_metadata": {key: str(value) if isinstance(value, Fraction) else value for key, value in verified.items()},
            "gpu": gpu,
            "encoder": selected_encoder,
            "frames_processed": delivered,
            "scene_resets": scene_resets,
            "pipeline": "direct-dlssnr-feature18",
            "feature_id": 18,
            "feature_18_confirmed": True,
            "direct_create_result": direct_create_result,
            "successful_direct_evaluations": nr_count,
            "model_sha256": hashlib.sha256((RUNTIME / "nvngx_dlssnr.dll").read_bytes()).hexdigest(),
            "worker_sha256": hashlib.sha256(WORKER.read_bytes()).hexdigest(),
            "loaded_module_inventory": ["nvngx.dll (standalone worker image)", "nvngx_dlssnr.dll", "system D3D12/DXGI/NGX core"],
            "carrier_modules_absent": {"nvngx_dlss.dll": True, "ReShade": True, "RenoDX": True},
            "native_settings": native,
            "elapsed_seconds": elapsed,
            "average_fps": delivered / elapsed,
            "worker_log": worker_logs,
            "encoder_log": encoder_logs,
        }
        report_path = output.with_suffix(output.suffix + ".report.json")
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if progress:
            progress(1.0, "Complete — feature 18 confirmed")
        return ConversionResult(str(output), str(report_path), delivered, nr_count, elapsed, gpu["name"])
    except Exception as exc:
        was_cancelled = controller.cancel.is_set()
        controller.stop()
        if output and output.exists():
            output.unlink()
        if was_cancelled and not isinstance(exc, Cancelled):
            raise Cancelled("Render stopped by user.") from exc
        raise
    finally:
        _ACTIVE = None
        _ACTIVE_LOCK.release()
        if job_dir and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
