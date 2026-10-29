import os
import logging
import shutil
from typing import Optional

import numpy as np
import ctranslate2
from faster_whisper import WhisperModel, BatchedInferencePipeline
import subprocess

logger = logging.getLogger(__name__)

# Global model instance
_model: Optional[WhisperModel] = None


def cuda_available() -> bool:
    """Whether CTranslate2 can see a usable CUDA device.

    This deliberately asks CTranslate2 instead of PyTorch. CTranslate2 is the
    actual inference engine, and dropping the torch dependency keeps the
    AppImage several gigabytes smaller (torch ships its own CUDA libraries).
    """
    try:
        return ctranslate2.get_cuda_device_count() > 0
    except Exception as exc:  # missing driver, missing cuBLAS, no GPU, ...
        logger.debug("CUDA device detection failed: %s", exc)
        return False


def ffmpeg_binary() -> str:
    exe = os.getenv("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "ffmpeg not found. Install ffmpeg, set FFMPEG_BINARY, or use the "
            "Blurb AppImage (which bundles it)."
        )
    return exe


def get_model() -> WhisperModel:
    """Get or create global Whisper model instance"""
    global _model

    if _model is None:
        requested = (os.getenv("WHISPER_DEVICE") or "auto").strip().lower()
        device = requested
        if device == "auto":
            device = "cuda" if cuda_available() else "cpu"

        model_name = os.getenv("WHISPER_MODEL") or "distil-large-v3"
        override_compute = os.getenv("WHISPER_COMPUTE_TYPE") or None

        def load(target: str) -> WhisperModel:
            # int8 is the practical default for CPU (float16 has no CPU kernel
            # path in ctranslate2).
            compute_type = override_compute or ("float16" if target == "cuda" else "int8")
            logger.info(
                f"Loading Faster-Whisper model: {model_name}, device: {target}, compute: {compute_type}"
            )
            return WhisperModel(model_name, device=target, compute_type=compute_type)

        try:
            base_model = load(device)
        except Exception as exc:
            # A driver/library mismatch (common with a bundled cuBLAS and an
            # old driver) should not make an otherwise-usable machine fail.
            # Only fall back when the user asked for automatic detection.
            if requested != "auto" or device != "cuda":
                raise
            logger.warning(f"CUDA model load failed ({exc}); falling back to CPU")
            base_model = load("cpu")

        # Wrap with BatchedInferencePipeline for better performance
        _model = BatchedInferencePipeline(model=base_model)
        logger.info("Model loaded successfully with batched inference pipeline")

    return _model


def transcribe_audio(
    audio_data: bytes,
    language: Optional[str] = None,
    on_progress=None,
) -> dict:
    """
    Transcribe audio bytes

    Args:
        audio_data: Raw audio file bytes
        language: Language code (optional, auto-detected if None)
        on_progress: Optional callback(percent, detail) called during decoding
            and segment iteration. Used by blurb to report live progress to
            conductor; failures in the callback must never break transcription.

    Returns:
        dict with text, segments, and language
    """
    model = get_model()

    def report(percent: float, detail: str | None = None) -> None:
        if not on_progress:
            return
        try:
            on_progress(percent, detail)
        except Exception:
            pass

    # Pipe audio through ffmpeg entirely in memory — no temp files
    logger.info("Downsampling audio to 16kHz mono")
    report(0, "decoding audio")
    try:
        proc = subprocess.run([
            ffmpeg_binary(),
            '-i', 'pipe:0',      # read from stdin
            '-ar', '16000',      # 16kHz sample rate
            '-ac', '1',          # mono
            '-f', 'f32le',       # raw float32 little-endian PCM
            'pipe:1'             # write to stdout
        ], input=audio_data, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(f"ffmpeg could not decode the audio: {detail}") from exc

    # Convert raw PCM bytes to float32 numpy array (what Whisper expects)
    audio_array = np.frombuffer(proc.stdout, dtype=np.float32)

    # Transcribe with batched inference (batch_size=8) and word timestamps
    segments, info = model.transcribe(
        audio_array,
        language=language or "en",
        word_timestamps=True,
        batch_size=8,
        beam_size=5,
        vad_filter=True
    )

    # Collect results
    segments_list = []
    text_parts = []
    duration = getattr(info, "duration", None) or 0

    for seg in segments:
        segments_list.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text,
            "words": [
                {"start": w.start, "end": w.end, "word": w.word, "probability": w.probability}
                for w in (seg.words or [])
            ]
        })
        text_parts.append(seg.text)
        if duration > 0:
            report(min(99.0, seg.end / duration * 100.0), "transcribing audio")

    report(99, "transcription complete")
    return {
        "text": " ".join(text_parts).strip(),
        "segments": segments_list,
        "language": info.language
    }
