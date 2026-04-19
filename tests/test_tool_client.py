"""Synchronous tool-call session tests — httpx requests mocked with respx.

Tests follow TDD: written before app/tool_client.py exists.
Each test exercises one distinct behavior of run_session().
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import json

import pytest
import respx
import httpx

BASE_URL = "http://localhost:8000"

# A minimal OpenAI-style tools definition used across all tests.
SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Return current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


@respx.mock
def test_single_tool_call_round_trip():
    """model calls tool → auto result injected → model replies → final step.

    Sequence:
        POST /v1/chat/completions  → assistant message with one tool_call
        POST /v1/chat/completions  → assistant final text reply

    Expected yields: tool_call, tool_result, final  (3 steps total)
    """
    from tool_client import run_session

    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        side_effect=[
            # First call: model requests a tool
            httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city": "Austin"}',
                            },
                        }],
                    }
                }]
            }),
            # Second call: model gives final answer
            httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "It's 82°F and sunny in Austin.",
                        "tool_calls": None,
                    }
                }]
            }),
        ]
    )

    steps = list(run_session(BASE_URL, "test-model", SAMPLE_TOOLS,
                              "What's the weather in Austin?"))

    assert len(steps) == 3
    assert steps[0][0] == "tool_call"
    assert steps[0][1].name == "get_weather"
    assert json.loads(steps[0][1].arguments)["city"] == "Austin"
    assert steps[1][0] == "tool_result"
    # The auto-generated result string should reference the tool name
    assert "get_weather" in steps[1][1]
    assert steps[2][0] == "final"
    assert "Austin" in steps[2][1]


@respx.mock
def test_no_tool_call_passthrough():
    """Direct answer (no tools): yields exactly one final step.

    When the model answers without invoking any tools, run_session()
    should terminate immediately with a single ("final", content) tuple.
    """
    from tool_client import run_session

    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "The capital of France is Paris.",
                    "tool_calls": None,
                }
            }]
        })
    )

    steps = list(run_session(BASE_URL, "test-model", SAMPLE_TOOLS,
                              "What is the capital of France?"))

    assert len(steps) == 1
    assert steps[0][0] == "final"
    assert "Paris" in steps[0][1]


@respx.mock
def test_http_error_raises():
    """HTTP 500 propagates as httpx.HTTPStatusError to the caller.

    run_session() calls resp.raise_for_status(); a 500 from the server
    must bubble up rather than being swallowed.
    """
    from tool_client import run_session

    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    with pytest.raises(httpx.HTTPStatusError):
        list(run_session(BASE_URL, "test-model", SAMPLE_TOOLS, "Hello"))


@respx.mock
def test_two_tool_calls_in_one_turn():
    """Multiple tool_calls in one assistant message are each yielded as separate steps.

    When the model returns two tool_calls in a single turn, run_session()
    must yield:
        tool_call A, tool_result A, tool_call B, tool_result B, final

    That is 5 steps total, with each tool call and its injected result
    appearing as consecutive pairs before the final reply.
    """
    from tool_client import run_session

    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        side_effect=[
            # First call: model requests two tools at once
            httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"A"}',
                                },
                            },
                            {
                                "id": "c2",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"B"}',
                                },
                            },
                        ],
                    }
                }]
            }),
            # Second call: model gives final answer
            httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Done.",
                        "tool_calls": None,
                    }
                }]
            }),
        ]
    )

    steps = list(run_session(BASE_URL, "m", SAMPLE_TOOLS, "weather A and B?"))

    # Expected: call A, result A, call B, result B, final
    assert len(steps) == 5
    assert steps[0][0] == "tool_call"
    assert steps[0][1].name == "get_weather"
    # Third step (index 2) is the second tool_call, for city B
    assert steps[2][0] == "tool_call"
    assert steps[2][1].arguments == '{"city":"B"}'
    assert steps[4][0] == "final"
