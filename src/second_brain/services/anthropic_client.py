"""Anthropic API client wrapper for Claude Haiku 4.5 and Claude Sonnet 4.6.

This module provides a typed interface for calling Anthropic's Claude models
with structured JSON output validated against Pydantic response models.
"""

import json
import logging
import time

import anthropic
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0  # seconds
BACKOFF_MULTIPLIER = 2.0

# Anthropic errors worth retrying
_RETRYABLE_ERRORS = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
)


class AnthropicClientError(Exception):
    """Raised when all retries are exhausted."""

    def __init__(self, message: str, partial_data: str | None = None) -> None:
        super().__init__(message)
        self.partial_data = partial_data


class AnthropicClient:
    """Client for Anthropic Claude API calls with structured output.

    Provides methods for calling Haiku (fast/cheap, used for enrichment,
    scoring, simple queries) and Sonnet (powerful, used for synthesis,
    scheduling, pattern detection).

    Both methods accept a Pydantic model class as response_model and return
    a validated instance of that model parsed from Claude's JSON response.
    """

    def __init__(self, api_key: str) -> None:
        """Initialize the Anthropic client.

        Args:
            api_key: Anthropic API key for authentication.
        """
        self.api_key = api_key
        self.client = anthropic.Anthropic(api_key=api_key)

    def call_haiku(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[BaseModel],
    ) -> BaseModel:
        """Call Claude Haiku 4.5 with structured JSON output.

        Args:
            system_prompt: System message defining Claude's role and output format.
            user_prompt: User message containing the content to process.
            response_model: Pydantic model class to validate and parse the response.

        Returns:
            An instance of response_model populated from Claude's JSON response.

        Raises:
            AnthropicClientError: If all retries are exhausted.
        """
        return self._call_model(HAIKU_MODEL, system_prompt, user_prompt, response_model)

    def call_sonnet(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[BaseModel],
    ) -> BaseModel:
        """Call Claude Sonnet 4.6 with structured JSON output.

        Args:
            system_prompt: System message defining Claude's role and output format.
            user_prompt: User message containing the content to process.
            response_model: Pydantic model class to validate and parse the response.

        Returns:
            An instance of response_model populated from Claude's JSON response.

        Raises:
            AnthropicClientError: If all retries are exhausted.
        """
        return self._call_model(SONNET_MODEL, system_prompt, user_prompt, response_model)

    def _call_model(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_model: type[BaseModel],
    ) -> BaseModel:
        """Call a Claude model and parse the response into a Pydantic model.

        Retries on API errors (rate limits, 5xx) and JSON parse/validation
        failures, up to MAX_RETRIES attempts with exponential backoff.
        """
        last_raw_text: str | None = None
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result, raw_text = self._make_api_call(model, system_prompt, user_prompt)
                last_raw_text = raw_text
                return self._parse_response(raw_text, response_model)

            except _RETRYABLE_ERRORS as exc:
                last_error = exc
                logger.warning(
                    "API error on attempt %d/%d for %s: %s",
                    attempt, MAX_RETRIES, model, exc,
                )
                if attempt < MAX_RETRIES:
                    self._backoff(attempt)

            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                logger.warning(
                    "Parse/validation error on attempt %d/%d for %s: %s | raw=%s",
                    attempt, MAX_RETRIES, model, exc,
                    (last_raw_text or "")[:200],
                )
                if attempt < MAX_RETRIES:
                    self._backoff(attempt)

        raise AnthropicClientError(
            f"All {MAX_RETRIES} attempts failed for {model}: {last_error}",
            partial_data=last_raw_text,
        )

    def _make_api_call(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[anthropic.types.Message, str]:
        """Send a request to the Anthropic API and return the message + raw text."""
        start = time.monotonic()

        response = self.client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        elapsed = time.monotonic() - start
        raw_text = response.content[0].text

        logger.info(
            "Anthropic API call | model=%s | input_tokens=%d | output_tokens=%d | latency=%.2fs",
            model,
            response.usage.input_tokens,
            response.usage.output_tokens,
            elapsed,
        )

        return response, raw_text

    @staticmethod
    def _parse_response(raw_text: str, response_model: type[BaseModel]) -> BaseModel:
        """Parse raw text as JSON and validate against the Pydantic model.

        Handles common LLM output quirks:
        - JSON wrapped in markdown code fences (```json ... ```)
        - JSON followed by explanatory text
        - JSON preceded by commentary
        """
        text = raw_text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```json or ```) and last line (```)
            lines = [l for l in lines[1:] if l.strip() != "```"]
            text = "\n".join(lines).strip()

        # Try direct parse first (fast path)
        try:
            data = json.loads(text)
            return response_model.model_validate(data)
        except json.JSONDecodeError:
            pass

        # Extract the first JSON object or array from the text
        # Find the first { or [ and use a decoder to consume just that object
        for i, ch in enumerate(text):
            if ch in "{[":
                decoder = json.JSONDecoder()
                data, _ = decoder.raw_decode(text, i)
                return response_model.model_validate(data)

        raise json.JSONDecodeError("No JSON object found in response", text, 0)

    @staticmethod
    def _backoff(attempt: int) -> None:
        """Sleep with exponential backoff."""
        delay = INITIAL_BACKOFF * (BACKOFF_MULTIPLIER ** (attempt - 1))
        logger.debug("Backing off %.1fs before retry", delay)
        time.sleep(delay)
