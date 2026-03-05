import os
import tempfile
import logging
from typing import Optional

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

    # Write original audio to temp file
    with tempfile.NamedTemporaryFile(suffix=".input", delete=False) as tmp_input:
        tmp_input.write(audio_data)
        tmp_input_path = tmp_input.name

    # Prepare output path for downsampled audio
    tmp_output_path = tempfile.mktemp(suffix=".wav")

    try:
        # Downsample to 16kHz mono using ffmpeg (matches Groq's preprocessing)
        logger.info("Downsampling audio to 16kHz mono")
        subprocess.run([
            'ffmpeg',
            '-i', tmp_input_path,
            '-ar', '16000',      # 16kHz sample rate
            '-ac', '1',          # Mono
            '-y',                # Overwrite output
            tmp_output_path
        ], check=True, capture_output=True)

        # Transcribe with batched inference (batch_size=8) and word timestamps
        segments, info = model.transcribe(
            tmp_output_path,
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
    finally:
        # Clean up temp files
        if os.path.exists(tmp_input_path):
            os.remove(tmp_input_path)
        if os.path.exists(tmp_output_path):
            os.remove(tmp_output_path)
