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
        # Check CUDA availability
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available. This service requires GPU acceleration.")

        model_name = os.getenv("WHISPER_MODEL", "distil-large-v3")
        compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "float16")

        logger.info(f"Loading Faster-Whisper model: {model_name}, compute: {compute_type}")
        base_model = WhisperModel(model_name, device="cuda", compute_type=compute_type)

        # Wrap with BatchedInferencePipeline for better performance
        _model = BatchedInferencePipeline(model=base_model)
        logger.info("Model loaded successfully with batched inference pipeline")

    return _model


def transcribe_audio(audio_data: bytes, language: Optional[str] = None) -> dict:
    """
    Transcribe audio bytes

    Args:
        audio_data: Raw audio file bytes
        language: Language code (optional, auto-detected if None)

    Returns:
        dict with text, segments, and language
    """
    model = get_model()

    # Pipe audio through ffmpeg entirely in memory — no temp files
    logger.info("Downsampling audio to 16kHz mono")
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

    return {
        "text": " ".join(text_parts).strip(),
        "segments": segments_list,
        "language": info.language
    }
