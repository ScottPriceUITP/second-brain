"""Whisper client — transcribes audio via OpenAI's Whisper API."""

import io
import logging
import time

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0
BACKOFF_MULTIPLIER = 2.0


class TranscriptionResult(BaseModel):
    """Result from a Whisper transcription."""

    text: str = Field(description="Transcribed text.")
    confidence: float = Field(
        description="Confidence score from 0 to 1.", ge=0.0, le=1.0
    )
    language: str = Field(default="en", description="Detected language code.")


class WhisperClientError(Exception):
    """Raised when transcription fails after all retries."""


class WhisperClient:
    """Client for OpenAI Whisper API transcription."""

    def __init__(self, api_key: str) -> None:
        import openai

        self.client = openai.OpenAI(api_key=api_key)

    def transcribe(
        self, audio_file: bytes, filename: str = "voice.ogg"
    ) -> TranscriptionResult:
        """Transcribe audio bytes via OpenAI Whisper API.

        Args:
            audio_file: Raw audio bytes to transcribe.
            filename: Filename hint for the API (helps with format detection).

        Returns:
            TranscriptionResult with text, confidence, and language.

        Raises:
            WhisperClientError: If all retries are exhausted.
        """
        import openai

        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                file_obj = io.BytesIO(audio_file)
                file_obj.name = filename

                start = time.monotonic()
                response = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=file_obj,
                    response_format="verbose_json",
                )
                elapsed = time.monotonic() - start

                text = response.text or ""
                language = getattr(response, "language", "en") or "en"

                # Whisper verbose_json includes segment-level data; derive
                # an overall confidence from average of segment no_speech_prob.
                # Lower no_speech_prob = higher confidence.
                segments = getattr(response, "segments", None)
                if segments:
                    avg_no_speech = sum(
                        s.get("no_speech_prob", 0.0)
                        if isinstance(s, dict)
                        else getattr(s, "no_speech_prob", 0.0)
                        for s in segments
                    ) / len(segments)
                    confidence = max(0.0, min(1.0, 1.0 - avg_no_speech))
                else:
                    # No segment data; assume reasonable confidence if text present
                    confidence = 0.85 if text.strip() else 0.0

                logger.info(
                    "Whisper transcription | language=%s | confidence=%.2f | "
                    "chars=%d | latency=%.2fs",
                    language,
                    confidence,
                    len(text),
                    elapsed,
                )

                return TranscriptionResult(
                    text=text,
                    confidence=confidence,
                    language=language,
                )

            except (
                openai.RateLimitError,
                openai.InternalServerError,
                openai.APIConnectionError,
            ) as exc:
                last_error = exc
                logger.warning(
                    "Whisper API error on attempt %d/%d: %s",
                    attempt,
                    MAX_RETRIES,
                    exc,
                )
                if attempt < MAX_RETRIES:
                    delay = INITIAL_BACKOFF * (BACKOFF_MULTIPLIER ** (attempt - 1))
                    time.sleep(delay)

            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Whisper transcription error on attempt %d/%d: %s",
                    attempt,
                    MAX_RETRIES,
                    exc,
                )
                if attempt < MAX_RETRIES:
                    delay = INITIAL_BACKOFF * (BACKOFF_MULTIPLIER ** (attempt - 1))
                    time.sleep(delay)

        raise WhisperClientError(
            f"All {MAX_RETRIES} attempts failed for Whisper transcription: {last_error}"
        )
