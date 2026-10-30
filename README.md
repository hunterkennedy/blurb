<p align="center">
  <img src="blurb.png" alt="Blurb" width="128">
</p>

<h1 align="center">Blurb</h1>

<p align="center">
  GPU-accelerated audio transcription worker built on
  <a href="https://github.com/SYSTRAN/faster-whisper">Faster-Whisper</a>
</p>

---

Blurb runs as a pull-worker: it polls a remote service for jobs, downloads audio locally, transcribes it with Whisper, and posts the result back. It also handles YouTube/Patreon/RSS discovery and downloads. Only outbound connections are made; cookies stay local.

**Requirements:** Linux x86_64. The NVIDIA AppImage bundles Python, FFmpeg, Node.js, and cuBLAS. The ROCm image requires ROCm 7.x on the host. A source checkout needs Python 3.12, FFmpeg, and Node.js.

NVIDIA CUDA is recommended, but Blurb falls back to CPU. AMD GPUs are supported by the ROCm build with ROCm 7.x installed.

## Quick Start (AppImage)

```bash
chmod +x Blurb-nvidia-x86_64.AppImage
./Blurb-nvidia-x86_64.AppImage
```

For AMD:

```bash
chmod +x Blurb-rocm-x86_64.AppImage
./Blurb-rocm-x86_64.AppImage
```

First launch opens Settings. Paste in your Remote URL and API key, then Save. Config and worker ID live in `~/.config/blurb`; rotating logs live in `~/.cache/blurb`.

Useful flags:

```bash
./Blurb-nvidia-x86_64.AppImage --self-test
./Blurb-nvidia-x86_64.AppImage --transcribe audio.wav
./Blurb-nvidia-x86_64.AppImage --worker
./Blurb-nvidia-x86_64.AppImage --demo
```

If FUSE is unavailable, run with `--appimage-extract-and-run`.

## Quick Start (source)

```bash
sudo dnf install -y python3.12 ffmpeg nodejs
./scripts/dev-install.sh
./scripts/dev-run.sh
```

On Ubuntu/Debian the system packages are `python3.12 ffmpeg nodejs` (Ubuntu 22.04 needs the deadsnakes PPA for Python 3.12). The manager GUI is Qt-based; PyQt6 is installed from `requirements.txt`, so no Tk packages are needed.

`scripts/dev-install.sh` creates the venv, installs dependencies, and adds `nvidia-cublas-cu12` when an NVIDIA GPU is detected. PyTorch and cuDNN are not needed. The dev helpers are only for source checkouts; the AppImage has no install step.

For a manual setup, install `requirements.txt` and add `nvidia-cublas-cu12==12.4.5.8` on NVIDIA machines. For AMD source installs, use the ROCm `ctranslate2` wheel from CTranslate2's GitHub releases; the ROCm AppImage is simpler.

## Usage

**Manager GUI:**

```bash
./scripts/dev-run.sh
# or
./Blurb-nvidia-x86_64.AppImage
```

The GUI has a live status card, progress, recent jobs, logs, and settings. The footer reports whether the bundled CTranslate2 runtime actually detects a GPU (`GPU: CUDA` / `GPU: ROCm`) or only CPU. Settings covers the server connection (with a reachability test), launch at login, automatic worker start, cookie files, and Whisper model/device options. The **Launch at login** toggle writes `~/.config/autostart/blurb.desktop`. Closing the window keeps the worker running in the system tray; use the tray menu to reopen it or quit. In the AppImage, the manager relaunches the same executable with `--worker`.

For scheduled or one-shot runs, enable **Quit when the queue is empty** in Startup (or set `BLURB_QUIT_WHEN_IDLE=true`). Blurb drains any available jobs, then exits on the first empty poll; the manager closes with it. Network errors and failed jobs do not trigger this.

**Demo mode:**

```bash
./Blurb-rocm-x86_64.AppImage --demo
# or from a source checkout
./scripts/dev-demo.sh
```

Demo mode opens the manager with simulated jobs, progress, errors, and settings. It never starts a worker or contacts a server, and settings are written to a temporary directory. Use the **DEMO** selector at the top to switch between auto-cycle, downloading, transcribing, waiting, error, no-jobs, and stopped states.

**Headless:**

```bash
venv-linux/bin/python web_worker.py
# or
./Blurb-nvidia-x86_64.AppImage --worker
```

Requires `WEB_URL` and `BLURB_API_KEY` in `.env` (or `~/.config/blurb/.env` for the AppImage).

## Configuration

Settings live in `.env`. The AppImage uses `~/.config/blurb/.env`; a source checkout uses `blurb/.env`. Override the AppImage path with `BLURB_CONFIG_DIR`.

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `distil-large-v3` | Faster-Whisper model name |
| `WHISPER_DEVICE` | `auto` | `auto` picks GPU if available, else CPU; force with `cuda` or `cpu` |
| `WHISPER_COMPUTE_TYPE` | `float16` GPU / `int8` CPU | `float16`, `int8`, or `float32` |
| `WEB_URL` | _(empty)_ | Remote API base URL |
| `BLURB_API_KEY` | _(empty)_ | Shared API key |
| `BLURB_AUTO_START_WORKER` | `true` | Start the worker when the manager opens |
| `BLURB_QUIT_WHEN_IDLE` | `false` | Exit after the first poll with an empty queue |
| `POLL_INTERVAL` | `5` | Initial poll interval; backs off to 12 hours |
| `HEARTBEAT_INTERVAL` | `60` | Seconds between liveness pings |
| `YOUTUBE_COOKIES_FILE` | _(empty)_ | Netscape cookies.txt for age-restricted YouTube |
| `PATREON_COOKIES_FILE` | _(empty)_ | Netscape cookies.txt for Patreon |
| `RSS_MAX_FEED_BYTES` | `26214400` | Max decompressed RSS/Atom feed size |
| `RSS_MAX_AUDIO_MB` | `1024` | Max RSS enclosure download size |
| `RSS_MAX_ICON_MB` | `5` | Max RSS artwork size |
| `ALLOW_PRIVATE_RSS_URLS` | `false` | Allow localhost/private-LAN feed URLs |

Cookies are only read during discover/download jobs and are sent only to YouTube/Patreon by yt-dlp.

## Building AppImages

```bash
./packaging/appimage/build-all.sh
# -> dist/Blurb-nvidia-x86_64.AppImage
# -> dist/Blurb-rocm-x86_64.AppImage

# Or build one backend:
./packaging/appimage/build-nvidia.sh
./packaging/appimage/build-rocm.sh
```

`build-all.sh` runs both builds sequentially because they share the venv, download cache, PyInstaller work directory, and AppDir. It is one command, but the two images are still produced one after the other.

The build runs PyInstaller, bundles FFmpeg, Node.js, and the selected CTranslate2 backend, then wraps everything with `appimagetool`.

| Flag | Effect |
|------|--------|
| `--cuda`, `--nvidia` | NVIDIA + CPU fallback |
| `--rocm` | AMD ROCm 7.x |
| `--cpu` | CPU only |
| `--output PATH` | Output file |

The ROCm build swaps in CTranslate2's official ROCm wheel and requires ROCm 7.x on the machine that runs it (`sudo dnf install rocm` on Fedora 44). For non-standard ROCm installs, set `BLURB_ROCM_LIB_DIR` to the directory containing `libamdhip64.so.7`. Radeon 780M/gfx1103 gets `HSA_OVERRIDE_GFX_VERSION=11.0.0` automatically; ROCm 7 hosts need a modern libstdc++ (Ubuntu 24.04+/Fedora 40+).

### Docker (Ubuntu 22.04)

```bash
docker build -f packaging/docker/Dockerfile -t blurb-appimage .

# NVIDIA (default)
docker run --rm -v "$PWD/dist:/out:z" blurb-appimage

# AMD ROCm
docker run --rm -v "$PWD/dist:/out:z" blurb-appimage \
    packaging/appimage/build-rocm.sh
```

The `:z` label is for Fedora SELinux; omit it elsewhere. Downloads are cached under `build/cache`.

## How It Works

Blurb makes only outbound connections. Audio never crosses the network: it is downloaded locally, transcribed, and deleted.

`web_worker.py` polls `GET /worker/jobs/next`:

- **`discover`** — scrape a YouTube, Patreon, or RSS source and return entries.
- **`process`** — download audio and return a transcript.

YouTube/Patreon use yt-dlp with local cookies. RSS needs no cookies; auth can be embedded in the feed URL. Results go to `POST /worker/jobs/{id}/complete`, failures to `/fail` with an `error_type`. Heartbeats and progress are posted to `/worker/heartbeat` and `/worker/jobs/{id}/progress`.

Faster-Whisper handles the decoding loop; CTranslate2 executes the model on CPU or GPU. PyTorch is not used at runtime.

## Tests

```bash
venv-linux/bin/python -m unittest discover -s tests
```
