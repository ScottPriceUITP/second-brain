"""Anthropic API client wrapper for Claude Haiku 4.5 and Claude Sonnet 4.6.

This module provides a typed interface for calling Anthropic's Claude models
with structured JSON output validated against Pydantic response models.

Other services should depend on this interface. The implementation will be
completed in Task 4.
"""

from pydantic import BaseModel


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

    def call_haiku(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[BaseModel],
    ) -> BaseModel:
        """Call Claude Haiku 4.5 with structured JSON output.

        Used for: enrichment, intent classification, connection scoring,
        query complexity routing, simple queries, entity disambiguation,
        nudge response parsing.

        Args:
            system_prompt: System message defining Claude's role and output format.
            user_prompt: User message containing the content to process.
            response_model: Pydantic model class to validate and parse the response.

        Returns:
            An instance of response_model populated from Claude's JSON response.

        Raises:
            NotImplementedError: Stub — will be implemented in Task 4.
        """
        raise NotImplementedError("Stub: to be implemented in T4")

    def call_sonnet(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[BaseModel],
    ) -> BaseModel:
        """Call Claude Sonnet 4.6 with structured JSON output.

        Used for: synthesis queries, scheduler reasoning, pattern detection,
        pre-meeting brief generation.

        Args:
            system_prompt: System message defining Claude's role and output format.
            user_prompt: User message containing the content to process.
            response_model: Pydantic model class to validate and parse the response.

        Returns:
            An instance of response_model populated from Claude's JSON response.

        Raises:
            NotImplementedError: Stub — will be implemented in Task 4.
        """
        raise NotImplementedError("Stub: to be implemented in T4")
