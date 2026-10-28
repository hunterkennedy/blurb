import os
import logging
from typing import Optional

import numpy as np
from faster_whisper import WhisperModel, BatchedInferencePipeline
import torch
import subprocess

logger = logging.getLogger(__name__)

# Global model instance
_model: Optional[WhisperModel] = None


def get_model() -> WhisperModel:
    """Get or create global Whisper model instance"""
    global _model

    if _model is None:
        device = os.getenv("WHISPER_DEVICE") or "auto"
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"

        model_name = os.getenv("WHISPER_MODEL") or "distil-large-v3"
        # int8 is the practical default for CPU (float16 has no CPU kernel path in ctranslate2)
        compute_type = os.getenv("WHISPER_COMPUTE_TYPE") or ("float16" if device == "cuda" else "int8")

        logger.info(f"Loading Faster-Whisper model: {model_name}, device: {device}, compute: {compute_type}")
        base_model = WhisperModel(model_name, device=device, compute_type=compute_type)

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
    proc = subprocess.run([
        'ffmpeg',
        '-i', 'pipe:0',      # read from stdin
        '-ar', '16000',      # 16kHz sample rate
        '-ac', '1',          # mono
        '-f', 'f32le',       # raw float32 little-endian PCM
        'pipe:1'             # write to stdout
    ], input=audio_data, capture_output=True, check=True)

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
