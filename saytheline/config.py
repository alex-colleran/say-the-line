"""Settings. Each one comes from an environment variable if set, else from a TOML file, else a
default that works for a plain single-machine install.

The file is the first that exists of: $SAYTHELINE_CONFIG, ./saytheline.toml, the repo's
saytheline.toml, ~/.config/saytheline/config.toml. See saytheline.example.toml for every key.

Loading only fills in os.environ (setdefault), so each module keeps reading its own SAYTHELINE_*
variable and an explicit environment variable always wins over the file."""
import os
import shutil
import sys
import tempfile

try:
    import tomllib
except ImportError:                     # Python < 3.11: env vars only
    tomllib = None

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (section, key) in the TOML file -> environment variable
KEYS = {
    ("jellyfin", "url"): "SAYTHELINE_JELLYFIN_URL",
    ("jellyfin", "api_key"): "SAYTHELINE_JELLYFIN_KEY",
    ("jellyfin", "db"): "SAYTHELINE_JELLYFIN_DB",
    ("jellyfin", "path_map"): "SAYTHELINE_PATH_MAP",
    ("jellyfin", "subtitle_cache"): "SAYTHELINE_JELLYFIN_SUBS",
    ("server", "host"): "SAYTHELINE_HOST",
    ("server", "port"): "SAYTHELINE_PORT",
    ("server", "public_url"): "SAYTHELINE_PUBLIC_URL",
    ("paths", "data"): "SAYTHELINE_DATA",
    ("paths", "preview_cache_gb"): "SAYTHELINE_PREVIEW_CACHE_GB",
    ("paths", "tmp"): "SAYTHELINE_TMP",
    ("tools", "stt_python"): "SAYTHELINE_STT_PYTHON",
    ("tools", "stt_device"): "SAYTHELINE_STT_DEVICE",
    ("tools", "stt_threads"): "SAYTHELINE_STT_THREADS",
    ("tools", "demucs"): "SAYTHELINE_DEMUCS",
    ("tools", "demucs_device"): "SAYTHELINE_DEMUCS_DEVICE",
    ("tools", "vaapi"): "SAYTHELINE_VAAPI",
    ("tools", "encoder"): "SAYTHELINE_ENCODER",
    ("tools", "rife"): "SAYTHELINE_RIFE",
    ("tools", "rife_model"): "SAYTHELINE_RIFE_MODEL",
    ("tools", "rife_threads"): "SAYTHELINE_RIFE_THREADS",
    ("tools", "tesseract"): "SAYTHELINE_TESSERACT",
    ("tools", "laser_python"): "SAYTHELINE_LASER_PYTHON",
    ("tools", "laser_model"): "SAYTHELINE_LASER_MODEL",
    ("tools", "wasted_font"): "SAYTHELINE_WASTED_FONT",
}
_loaded_from = None


def _candidates():
    if os.environ.get("SAYTHELINE_CONFIG"):
        yield os.environ["SAYTHELINE_CONFIG"]
    yield os.path.join(os.getcwd(), "saytheline.toml")
    yield os.path.join(REPO, "saytheline.toml")
    yield os.path.expanduser("~/.config/saytheline/config.toml")


def _flat(v):
    if isinstance(v, dict):             # path_map = { "/media" = "/srv/media" }
        return ",".join(f"{k}={x}" for k, x in v.items())
    return str(v)


def load():
    global _loaded_from
    if tomllib is not None:
        for p in _candidates():
            if os.path.isfile(p):
                with open(p, encoding="utf-8-sig") as f:    # -sig: Windows editors may add a BOM
                    data = tomllib.loads(f.read())
                base = os.path.dirname(os.path.abspath(p))
                for (sec, key), env in KEYS.items():
                    v = (data.get(sec) or {}).get(key)
                    if v in (None, ""):
                        continue
                    v = _flat(v)
                    if key in ("data", "tmp", "db", "stt_python", "demucs", "rife", "rife_model", "tesseract",
                               "laser_python", "laser_model", "wasted_font"):    # relative to the file
                        v = os.path.normpath(os.path.join(base, os.path.expanduser(v)))
                    os.environ.setdefault(env, v)
                _loaded_from = p
                break
    # older variable name
    if os.environ.get("JELLYFIN_DB"):
        os.environ.setdefault("SAYTHELINE_JELLYFIN_DB", os.environ["JELLYFIN_DB"])
    tmp = os.environ.get("SAYTHELINE_TMP")
    if tmp:     # slow motion frames and stems can take GBs: keep them off a small system drive
        os.makedirs(tmp, exist_ok=True)
        tempfile.tempdir = tmp
        for v in ("TMPDIR", "TMP", "TEMP"):    # and the children's (demucs, whisper)
            os.environ[v] = tmp


def data_dir():
    return os.environ.get("SAYTHELINE_DATA") or os.path.join(REPO, "data")


def loaded_from():
    return _loaded_from


def nice():
    """Lower the priority of heavy child processes where the OS has `nice` (not on Windows: see
    lower_priority)."""
    return ["nice", "-n", "10"] if shutil.which("nice") else []


def lower_priority():
    """Windows has no `nice`, but children inherit their parent's priority class: run the whole
    server or indexer below normal, so a render never makes the desktop stutter."""
    if os.name == "nt":
        import ctypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x4000
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)


def stt_python():
    """The Python that has faster-whisper and spaCy: configured, or the one running Say the Line."""
    return os.environ.get("SAYTHELINE_STT_PYTHON") or sys.executable


def _beside_python(name):
    """A tool installed into the running Python's venv: on PATH only while the venv is activated."""
    return shutil.which(name, path=os.path.dirname(sys.executable))


def demucs():
    """Stem separation is optional: the configured binary, else `demucs` on PATH or in this venv, else None."""
    p = os.environ.get("SAYTHELINE_DEMUCS") or shutil.which("demucs") or _beside_python("demucs")
    return p if p and os.path.exists(p) else None


def demucs_device():
    """cpu (the default), or cuda for an Nvidia GPU with a CUDA build of PyTorch (several times faster)."""
    return os.environ.get("SAYTHELINE_DEMUCS_DEVICE") or "cpu"


def laser_python():
    """The Python that has MediaPipe, for laser eyes: configured, or the one running Say the Line."""
    return os.environ.get("SAYTHELINE_LASER_PYTHON") or sys.executable


def laser_model():
    """MediaPipe's face_landmarker.task: configured, else data/models/face_landmarker.task."""
    return os.environ.get("SAYTHELINE_LASER_MODEL") or os.path.join(data_dir(), "models", "face_landmarker.task")


def tesseract():
    """OCR for picture subtitles: configured, else on PATH, else where the Windows installer puts it."""
    p = os.environ.get("SAYTHELINE_TESSERACT") or shutil.which("tesseract")
    if not p and os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            c = os.path.join(base or "", "Tesseract-OCR", "tesseract.exe")
            if base and os.path.isfile(c):
                return c
    return p if p and os.path.exists(p) else None


def encoder():
    """H.264 encoder for previews and MP4 renders: "" (x264 on the CPU, the default) or "nvenc"
    (an Nvidia GPU; the CPU is left for captions and filters)."""
    return (os.environ.get("SAYTHELINE_ENCODER") or "").lower()


def vaapi():
    """Hardware preview encoding (Linux VAAPI). Default: the first render node, if there is one."""
    p = os.environ.get("SAYTHELINE_VAAPI")
    if p is not None:
        return p or None
    return "/dev/dri/renderD128" if os.path.exists("/dev/dri/renderD128") else None


load()
