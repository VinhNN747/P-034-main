"""Explicit parameter policy for the currently implemented SDK profile."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class OpenAICompatibilityProfile:
    parameter_support: dict[str, Literal["supported", "noop", "unsupported"]] = field(default_factory=dict)
    unknown_parameter_policy: Literal["reject", "ignore"] = "reject"


STRICT_CHAT_PROFILE = OpenAICompatibilityProfile(
    parameter_support={
        **dict.fromkeys(
            (
                "model",
                "messages",
                "max_tokens",
                "max_completion_tokens",
                "response_format",
                "stream",
                "stream_options",
                "n",
                "user",
                "metadata",
                "tools",
                "tool_choice",
                "gateway",
                "temperature", "top_p", "stop", "seed", "frequency_penalty", "presence_penalty", "parallel_tool_calls",
            ),
            "supported",
        ),
        **dict.fromkeys(
            (
                "logprobs",
                "top_logprobs",
                "logit_bias",
                "functions",
                "function_call",
                "audio",
                "modalities",
                "prediction",
                "reasoning_effort",
                "service_tier",
                "store",
                "web_search_options",
                "verbosity",
                "prompt_cache_key",
                "prompt_cache_retention",
            ),
            "unsupported",
        ),
    }
)
