# Say the Line on Windows (native)

Everything runs natively on Windows 10/11: no Docker or WSL needed. Tested on Windows 11 with
Python 3.12, ffmpeg 8.1 (gyan.dev "full" build), Jellyfin 10.11 on another machine with the media
read over SMB, and an Nvidia GTX 1660 Ti laptop GPU.

The commands below are for **PowerShell**. Where the README says `cp`, use `Copy-Item`.

## 1. Install the tools

| Tool | Get it | Notes |
|---|---|---|
| Python 3.11+ | `winget install Python.Python.3.12` or python.org | |
| ffmpeg with libass | `winget install Gyan.FFmpeg` (the gyan.dev **full** build) | `ffmpeg -hide_banner -filters \| findstr subtitles` must list `subtitles` |
| git | `winget install Git.Git` | |
| Tesseract (optional, picture subtitles) | `winget install UB-Mannheim.TesseractOCR` | See below |
| rife-ncnn-vulkan (optional, slow motion / 60 fps) | the `-windows.zip` from [its releases](https://github.com/nihui/rife-ncnn-vulkan/releases) | Keep `rife-v4.6\` beside the `.exe` |

**Tesseract** is found on PATH, in `C:\Program Files\Tesseract-OCR`, or wherever `[tools] tesseract`
points; its installer doesn't add it to PATH, and Say the Line doesn't need it to. The installer asks
for admin rights; without them, open the installer `.exe` with 7-Zip, extract it to any folder and
point `[tools] tesseract` at the `tesseract.exe` inside (its `tessdata` folder with `eng` comes along).

## 2. Install Say the Line

```powershell
git clone https://github.com/alex-colleran/say-the-line
cd say-the-line
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m spacy download en_core_web_sm
Copy-Item saytheline.example.toml saytheline.toml
notepad saytheline.toml
python -m saytheline doctor
```

If PowerShell answers *running scripts is disabled on this system* at `Activate.ps1`, either skip
activating and call the venv's Python by its path (`.venv\Scripts\python.exe -m saytheline doctor`),
or allow local scripts for your account: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

**A small C: drive?** The venv with the speech model and PyTorch takes 2–6 GB, and renders write
temporary frames. Clone onto a roomier drive, set `[paths] tmp` (render scratch space), and point the
model caches there too before the first run: `$env:HF_HOME` (the speech model) and `$env:TORCH_HOME`
(the demucs model).

## 3. `saytheline.toml` on Windows

- **Paths in single quotes.** `rife = 'C:\tools\rife\rife-ncnn-vulkan.exe'`. In double quotes a
  backslash starts an escape (`"C:\Users"` is an error); forward slashes work in either.
- **Media on another machine:** map the server's shares to drive letters (File Explorer → *Map network
  drive*, or `net use M: \\server\media /persistent:yes`) and translate Jellyfin's paths to them:

  ```toml
  [jellyfin]
  url = "http://server:8096"
  path_map = { "/srv/media" = "M:", "/data/media" = "S:" }
  ```

  If Windows refuses a guest share ("your organization's security policies block unauthenticated guest
  access"), give the share a user and password on the server rather than turning guest logons back on.
- **The API key:** Dashboard → API Keys → +, pasted into `api_key`. If copying it onto this machine is
  awkward, run `python -m saytheline login`: it shows a six-digit Quick Connect code to approve in any
  signed-in Jellyfin app (profile → Quick Connect) and saves the token as `api_key` without printing it.
  The token shows under Dashboard → Devices as "Say the Line" and can be revoked there.
- Notepad and PowerShell 5's `Set-Content -Encoding utf8` may save the file with a byte-order mark;
  that is fine.

## 4. Index and run

```powershell
python -m saytheline index --series "Your Show"     # one show first
python -m saytheline serve                           # http://127.0.0.1:8765/
```

Over a network share, external subtitle files index in a second or two a show, but **embedded
tracks** mean reading each whole video across the network, which takes hours for a big library: index
a show or two first, or run the indexer on the machine that has the disks and copy `data\cues.db`.
Picture subtitles (OCR) took 1.5–3 minutes a film here.

Heavy work (renders, OCR, speech) runs at below-normal priority, so the desktop stays responsive.
To index nightly, a Task Scheduler task running `.venv\Scripts\python.exe -m saytheline index -q`
in the repo folder does what the systemd timer in `deploy/` does on Linux.

## Nvidia GPU

Each of these is off by default and falls back to the CPU when it can't run; `doctor` checks every one
it is asked for and says why one is off.

| Work | Default | Nvidia setting | Needs |
|---|---|---|---|
| Slow motion, 60 fps (RIFE) | Vulkan GPU, picked by itself | `[tools] rife_threads = "8:8:8"` | the RIFE zip |
| Speech (snap, word timing, Say Anything) | CPU, int8 | `[tools] stt_device = "cuda"` | cuBLAS 12 + cuDNN 9: a CUDA PyTorch in the same venv carries them, or `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` |
| Dialogue only / No dialogue (demucs) | CPU | `[tools] demucs_device = "cuda"` | a CUDA build of PyTorch |
| Encoding previews and MP4 renders | x264, CPU | `[tools] encoder = "nvenc"` | an Nvidia driver new enough for your ffmpeg (ffmpeg 8.1 wants 610+) |

Install the CUDA PyTorch **before** `requirements-stems.txt`, or pip pulls the CPU build:

```powershell
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements-stems.txt
python -c "import torch; print(torch.cuda.is_available())"     # True
```

Measured on a GTX 1660 Ti laptop (i7-9750H), a 2.8 s 720p clip from a 1080p HEVC film read over
SMB, same code with the settings off and on:

| Job | CPU / defaults | GPU settings on |
|---|---|---|
| Render, classic captions | 1.3 s | 1.4 s (NVENC not used: driver too old) |
| + slow motion 2× over 2 s | 27.4 s | 12.5 s |
| + 60 fps | 35.7 s | 15.2 s |
| Word timing (20 s window) | 6.7 s | 4.2 s |
| Dialogue only | 10.4 s | 7.1 s |

RIFE alone on 96 frames at 720p: 17.0 s with its default threads, 5.1 s with `8:8:8` (the GPU was
waiting on PNG decoding). Speech threads made no difference on this 6-core CPU (4 vs 6).

## Things that were broken on Windows (fixed)

For anyone on an older checkout:

- Captions failed to render (`No option name near '/...'`): filter paths with a drive colon needed
  escaping for both of ffmpeg's parsers.
- OCR of picture subtitles crashed on curly quotes (`'charmap' codec can't decode`): tool output was
  read in the locale's codec (cp1252) instead of UTF-8.
- The test suite used a shell script as its stand-in for RIFE, and left the index open so Windows
  couldn't delete it.
