"""Settings. Each one comes from an environment variable if set, else from a TOML file, else a
default that works for a plain single-machine install.

The file is the first that exists of: $SAYTHELINE_CONFIG, ./saytheline.toml, the repo's
saytheline.toml, ~/.config/saytheline/config.toml. See saytheline.example.toml for every key.

Loading only fills in os.environ (setdefault), so each module keeps reading its own SAYTHELINE_*
variable and an explicit environment variable always wins over the file."""
import os
import shutil
import sys

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
    ("server", "host"): "SAYTHELINE_HOST",
    ("server", "port"): "SAYTHELINE_PORT",
    ("server", "public_url"): "SAYTHELINE_PUBLIC_URL",
    ("paths", "data"): "SAYTHELINE_DATA",
    ("paths", "preview_cache_gb"): "SAYTHELINE_PREVIEW_CACHE_GB",
    ("tools", "stt_python"): "SAYTHELINE_STT_PYTHON",
    ("tools", "demucs"): "SAYTHELINE_DEMUCS",
    ("tools", "vaapi"): "SAYTHELINE_VAAPI",
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
                with open(p, "rb") as f:
                    data = tomllib.load(f)
                base = os.path.dirname(os.path.abspath(p))
                for (sec, key), env in KEYS.items():
                    v = (data.get(sec) or {}).get(key)
                    if v in (None, ""):
                        continue
                    v = _flat(v)
                    if key in ("data", "db", "stt_python", "demucs"):    # relative to the file
                        v = os.path.normpath(os.path.join(base, os.path.expanduser(v)))
                    os.environ.setdefault(env, v)
                _loaded_from = p
                break
    # older variable name
    if os.environ.get("JELLYFIN_DB"):
        os.environ.setdefault("SAYTHELINE_JELLYFIN_DB", os.environ["JELLYFIN_DB"])


def data_dir():
    return os.environ.get("SAYTHELINE_DATA") or os.path.join(REPO, "data")


def loaded_from():
    return _loaded_from


def nice():
    """Lower the priority of heavy child processes where the OS has `nice` (not on Windows)."""
    return ["nice", "-n", "10"] if shutil.which("nice") else []


def stt_python():
    """The Python that has faster-whisper and spaCy: configured, or the one running Say the Line."""
    return os.environ.get("SAYTHELINE_STT_PYTHON") or sys.executable


def demucs():
    """Stem separation is optional: the configured binary, else `demucs` on PATH, else None."""
    p = os.environ.get("SAYTHELINE_DEMUCS") or shutil.which("demucs")
    return p if p and os.path.exists(p) else None


def vaapi():
    """Hardware preview encoding (Linux VAAPI). Default: the first render node, if there is one."""
    p = os.environ.get("SAYTHELINE_VAAPI")
    if p is not None:
        return p or None
    return "/dev/dri/renderD128" if os.path.exists("/dev/dri/renderD128") else None


load()
