"""`python -m saytheline doctor`: check that everything Say the Line needs is in place, and say how to fix
what is not. Exit code 1 if something required is missing."""
import os
import shutil
import subprocess

from . import config, db, jellyfin, media

OK, WARN, BAD = "ok  ", "warn", "FAIL"


def _run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, **kw)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def checks():
    yield OK, f"settings: {config.loaded_from() or 'no saytheline.toml found (environment variables / defaults)'}"
    yield OK, f"data folder: {config.data_dir()}"

    ff = shutil.which("ffmpeg")
    if not ff or not shutil.which("ffprobe"):
        yield BAD, "ffmpeg/ffprobe not on PATH (install ffmpeg)"
    else:
        filters = _run([ff, "-hide_banner", "-filters"]).stdout
        if " subtitles " not in filters:
            yield BAD, "ffmpeg has no 'subtitles' filter: install a build with libass"
        else:
            yield OK, "ffmpeg with libass"
        if config.vaapi():
            yield OK, f"hardware previews via VAAPI ({config.vaapi()}), with CPU fallback"
        if config.encoder() == "nvenc":
            yield (OK, "previews and MP4 renders encoded on the Nvidia GPU (NVENC)") if media.nvenc() else \
                (WARN, f"encoder = nvenc, but h264_nvenc fails here ({media.nvenc_check()}): using x264 on the CPU")

    py = config.stt_python()
    r = _run([py, "-c", "import faster_whisper, numpy; print('ok')"])
    yield (OK, f"speech: faster-whisper in {py}") if r.returncode == 0 else \
        (WARN, f"speech features off: {py} has no faster-whisper (pip install -r requirements.txt)")
    if r.returncode == 0 and os.environ.get("SAYTHELINE_STT_DEVICE") == "cuda":
        here = os.path.dirname(os.path.abspath(__file__))
        r = _run([py, "-I", "-c", "import sys, os, ctypes; sys.path.insert(0, sys.argv[1]); import stt_worker; "
                  "stt_worker._cuda_libs(); import ctranslate2; n = ctranslate2.get_cuda_device_count(); "
                  "[ctypes.CDLL(x) for x in (('cublas64_12.dll', 'cudnn64_9.dll') if os.name == 'nt' else ())]; "
                  "print(n)", here])
        yield (OK, "speech on the Nvidia GPU (CUDA)") if r.returncode == 0 and r.stdout.strip() not in ("", "0") else \
            (WARN, "stt_device = cuda, but CUDA or cuBLAS/cuDNN won't load: speech stays on the CPU "
                   "(install a CUDA build of PyTorch, or pip install nvidia-cublas-cu12 nvidia-cudnn-cu12)")
    r = _run([py, "-c", "import spacy; spacy.load('en_core_web_sm'); print('ok')"])
    yield (OK, "Poster look: spaCy + en_core_web_sm") if r.returncode == 0 else \
        (WARN, "Poster look off: python -m spacy download en_core_web_sm")
    try:
        import PIL  # noqa: F401
        yield OK, "Pillow (Poster layout)"
    except ImportError:
        yield WARN, "Poster look off: pip install pillow"
    yield (OK, f"stem audio: {config.demucs()}") if config.demucs() else \
        (WARN, "Dialogue only / No dialogue off (optional: pip install -r requirements-stems.txt)")
    if config.demucs() and config.demucs_device() != "cpu":
        dpy = shutil.which("python", path=os.path.dirname(config.demucs()))     # the venv demucs lives in
        r = _run([dpy, "-c", "import torch; print(torch.cuda.is_available())"]) if dpy else None
        yield (OK, f"stem audio on the GPU ({config.demucs_device()})") if r and r.stdout.strip() == "True" else \
            (WARN, f"demucs_device = {config.demucs_device()}, but its PyTorch sees no CUDA GPU "
                   "(install a CUDA build: pytorch.org/get-started)")
    from . import pgs
    yield (OK, f"Blu-ray and DVD picture subtitles read by OCR: {pgs.TESSERACT}") if pgs.available() else \
        (WARN, "Blu-ray and DVD picture subtitles can't be searched (optional: apt install tesseract-ocr tesseract-ocr-eng, pip install pillow)")
    from . import slowmo
    yield (OK, f"slow motion: {slowmo.rife()[0]}") if slowmo.rife() else \
        (WARN, "Slow motion off (optional: rife-ncnn-vulkan with its rife-v4.6 model, see README)")
    from . import laser
    yield (OK, f"laser eyes: MediaPipe in {config.laser_python()}") if laser.available() else \
        (WARN, "Laser eyes off (optional: pip install -r requirements-laser.txt, and the face model, see README)")
    missing = [f for f in media.FONTS if not os.path.isfile(os.path.join(media.FONT_DIR, media.FONT_FILES[f]))]
    yield (BAD, f"bundled fonts missing: {', '.join(missing)}") if missing else (OK, "caption fonts")

    try:
        src = jellyfin.source()
    except RuntimeError as e:
        yield BAD, str(e)
        return
    try:
        items = jellyfin.list_items(src, movies=True)[:200] or jellyfin.list_items(src)[:200]
    except Exception as e:
        yield BAD, f"Jellyfin ({src[0]}): {e}"
        return
    yield OK, f"Jellyfin via {'API ' + src[1] if src[0] == 'api' else 'database ' + src[1]}"
    if not items:
        yield WARN, "Jellyfin returned no movies or episodes"
        return
    pmap = db.path_map()
    found = sum(os.path.exists(db.to_host(i["path"], pmap)) for i in items)
    if found == 0:
        yield BAD, (f"none of {len(items)} sample files exist here, e.g. {items[0]['path']} -> "
                    f"{db.to_host(items[0]['path'], pmap)}: set [jellyfin] path_map")
    elif found < len(items):
        missing = [db.to_host(i["path"], pmap) for i in items if not os.path.exists(db.to_host(i["path"], pmap))]
        # path_map works for the rest, so a folder that is there but shows no files is usually a
        # folder of symlinks the share won't follow (or that point inside a container)
        hidden = sum(os.path.isdir(os.path.dirname(p)) for p in missing)
        yield WARN, (f"{len(missing)} of {len(items)} sample files not found, e.g. {missing[0]}"
                     + (f" ({hidden} in folders that exist here: symlinks on the server that this share can't follow?)"
                        if hidden else " (check path_map)"))
    else:
        yield OK, f"media files readable ({found} sampled)"


def main():
    bad = False
    for level, msg in checks():
        print(f"[{level}] {msg}")
        bad = bad or level == BAD
    return 1 if bad else 0
