"""Tests for whisper client."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from second_brain.services.whisper_client import (
    TranscriptionResult,
    WhisperClient,
    WhisperClientError,
    MAX_RETRIES,
)


def _make_whisper_response(text="Hello world", segments=None, language="en"):
    """Build a mock Whisper API response object."""
    resp = SimpleNamespace()
    resp.text = text
    resp.language = language
    if segments is not None:
        resp.segments = segments
    else:
        # Default: segments with low no_speech_prob => high confidence
        resp.segments = [
            {"no_speech_prob": 0.05},
            {"no_speech_prob": 0.1},
        ]
    return resp


def _make_client_with_mock():
    """Create a WhisperClient with its internal openai client replaced by a mock."""
    with patch("openai.OpenAI"):
        client = WhisperClient(api_key="test-key")
    client.client = MagicMock()
    return client


class TestTranscriptionResultModel:
    """Tests for the TranscriptionResult pydantic model."""

    def test_valid_result(self):
        result = TranscriptionResult(text="Hello", confidence=0.95, language="en")
        assert result.text == "Hello"
        assert result.confidence == 0.95
        assert result.language == "en"

    def test_default_language(self):
        result = TranscriptionResult(text="Hello", confidence=0.9)
        assert result.language == "en"

    def test_confidence_bounds_min(self):
        result = TranscriptionResult(text="", confidence=0.0)
        assert result.confidence == 0.0

    def test_confidence_bounds_max(self):
        result = TranscriptionResult(text="", confidence=1.0)
        assert result.confidence == 1.0

    def test_confidence_below_zero_rejected(self):
        with pytest.raises(Exception):
            TranscriptionResult(text="", confidence=-0.1)

    def test_confidence_above_one_rejected(self):
        with pytest.raises(Exception):
            TranscriptionResult(text="", confidence=1.1)


class TestWhisperTranscribe:
    """Tests for WhisperClient.transcribe()."""

    def test_successful_transcription(self):
        client = _make_client_with_mock()
        response = _make_whisper_response(
            text="Hello world",
            segments=[{"no_speech_prob": 0.1}],
        )
        client.client.audio.transcriptions.create.return_value = response

        result = client.transcribe(b"fake audio bytes", filename="test.ogg")

        assert isinstance(result, TranscriptionResult)
        assert result.text == "Hello world"
        assert result.language == "en"
        client.client.audio.transcriptions.create.assert_called_once()

    def test_high_confidence_transcription(self):
        """Confidence >= 0.8 when no_speech_prob is low."""
        client = _make_client_with_mock()
        # Low no_speech_prob => high confidence (1.0 - 0.05 = 0.95)
        response = _make_whisper_response(
            text="Clear speech detected",
            segments=[
                {"no_speech_prob": 0.05},
                {"no_speech_prob": 0.05},
            ],
        )
        client.client.audio.transcriptions.create.return_value = response

        result = client.transcribe(b"audio")

        assert result.confidence >= 0.8
        assert result.confidence == pytest.approx(0.95)

    def test_low_confidence_transcription(self):
        """Confidence < 0.8 when no_speech_prob is high."""
        client = _make_client_with_mock()
        # High no_speech_prob => low confidence (1.0 - 0.5 = 0.5)
        response = _make_whisper_response(
            text="Maybe some speech",
            segments=[
                {"no_speech_prob": 0.5},
                {"no_speech_prob": 0.5},
            ],
        )
        client.client.audio.transcriptions.create.return_value = response

        result = client.transcribe(b"audio")

        assert result.confidence < 0.8
        assert result.confidence == pytest.approx(0.5)

    def test_no_segments_with_text_defaults_confidence(self):
        """When no segments data, confidence defaults to 0.85 if text present."""
        client = _make_client_with_mock()
        response = _make_whisper_response(text="Some text")
        # Remove segments to simulate missing data
        del response.segments
        client.client.audio.transcriptions.create.return_value = response

        result = client.transcribe(b"audio")

        assert result.confidence == 0.85

    def test_no_segments_empty_text_zero_confidence(self):
        """When no segments and empty text, confidence is 0.0."""
        client = _make_client_with_mock()
        response = _make_whisper_response(text="")
        del response.segments
        client.client.audio.transcriptions.create.return_value = response

        result = client.transcribe(b"audio")

        assert result.confidence == 0.0

    def test_language_detection(self):
        client = _make_client_with_mock()
        response = _make_whisper_response(text="Bonjour", language="fr")
        client.client.audio.transcriptions.create.return_value = response

        result = client.transcribe(b"audio")

        assert result.language == "fr"

    def test_uses_whisper_model_and_verbose_json(self):
        client = _make_client_with_mock()
        response = _make_whisper_response()
        client.client.audio.transcriptions.create.return_value = response

        client.transcribe(b"audio")

        call_kwargs = client.client.audio.transcriptions.create.call_args
        assert call_kwargs.kwargs["model"] == "whisper-1"
        assert call_kwargs.kwargs["response_format"] == "verbose_json"

    def test_confidence_clamped_to_valid_range(self):
        """Confidence is clamped between 0.0 and 1.0 even with extreme values."""
        client = _make_client_with_mock()
        # Very high no_speech_prob would give negative before clamping
        response = _make_whisper_response(
            text="test",
            segments=[{"no_speech_prob": 1.5}],
        )
        client.client.audio.transcriptions.create.return_value = response

        result = client.transcribe(b"audio")

        assert result.confidence >= 0.0
        assert result.confidence <= 1.0

    def test_null_language_defaults_to_en(self):
        """When language is None, defaults to 'en'."""
        client = _make_client_with_mock()
        response = _make_whisper_response(text="test", language=None)
        client.client.audio.transcriptions.create.return_value = response

        result = client.transcribe(b"audio")

        assert result.language == "en"


class TestWhisperRetryBehavior:
    """Tests for API error handling and retry logic."""

    @patch("second_brain.services.whisper_client.time")
    def test_retries_on_rate_limit_then_succeeds(self, mock_time):
        import openai

        client = _make_client_with_mock()
        response = _make_whisper_response(text="Success after retry")

        client.client.audio.transcriptions.create.side_effect = [
            openai.RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429),
                body=None,
            ),
            response,
        ]
        # monotonic() is called twice per successful attempt (start + elapsed)
        mock_time.monotonic.side_effect = [0.0, 0.0, 0.5]

        result = client.transcribe(b"audio")

        assert result.text == "Success after retry"
        assert client.client.audio.transcriptions.create.call_count == 2

    @patch("second_brain.services.whisper_client.time")
    def test_raises_after_all_retries_exhausted(self, mock_time):
        import openai

        client = _make_client_with_mock()

        client.client.audio.transcriptions.create.side_effect = openai.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429),
            body=None,
        )
        mock_time.monotonic.return_value = 0.0

        with pytest.raises(WhisperClientError, match="All .* attempts failed"):
            client.transcribe(b"audio")

        assert client.client.audio.transcriptions.create.call_count == MAX_RETRIES

    @patch("second_brain.services.whisper_client.time")
    def test_retries_on_generic_exception(self, mock_time):
        client = _make_client_with_mock()
        response = _make_whisper_response(text="Recovered")

        client.client.audio.transcriptions.create.side_effect = [
            RuntimeError("connection reset"),
            response,
        ]
        # monotonic() is called twice per successful attempt (start + elapsed)
        mock_time.monotonic.side_effect = [0.0, 0.0, 0.5]

        result = client.transcribe(b"audio")

        assert result.text == "Recovered"
        assert client.client.audio.transcriptions.create.call_count == 2

    @patch("second_brain.services.whisper_client.time")
    def test_retries_on_internal_server_error(self, mock_time):
        import openai

        client = _make_client_with_mock()
        response = _make_whisper_response(text="Recovered from 500")

        client.client.audio.transcriptions.create.side_effect = [
            openai.InternalServerError(
                message="internal error",
                response=MagicMock(status_code=500),
                body=None,
            ),
            response,
        ]
        # monotonic() is called twice per successful attempt (start + elapsed)
        mock_time.monotonic.side_effect = [0.0, 0.0, 0.5]

        result = client.transcribe(b"audio")

        assert result.text == "Recovered from 500"

    @patch("second_brain.services.whisper_client.time")
    def test_backoff_timing(self, mock_time):
        """Verify exponential backoff delays between retries."""
        import openai

        client = _make_client_with_mock()

        client.client.audio.transcriptions.create.side_effect = openai.RateLimitError(
            message="limited",
            response=MagicMock(status_code=429),
            body=None,
        )
        mock_time.monotonic.return_value = 0.0

        with pytest.raises(WhisperClientError):
            client.transcribe(b"audio")

        # Should have slept between retries (MAX_RETRIES - 1 times)
        sleep_calls = mock_time.sleep.call_args_list
        assert len(sleep_calls) == MAX_RETRIES - 1
        # First backoff: 1.0s, second: 2.0s
        assert sleep_calls[0].args[0] == pytest.approx(1.0)
        assert sleep_calls[1].args[0] == pytest.approx(2.0)


class TestWhisperClientInit:
    """Tests for WhisperClient initialization."""

    @patch("openai.OpenAI")
    def test_init_creates_openai_client(self, mock_openai_cls):
        client = WhisperClient(api_key="sk-test-key")
        mock_openai_cls.assert_called_once_with(api_key="sk-test-key")
