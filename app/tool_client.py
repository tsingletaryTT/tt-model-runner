#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Synchronous OpenAI-compatible multi-turn tool-call session.

Designed to run in a background threading.Thread (no asyncio).
Yields (step, payload) tuples; AppController emits on_tool_result for each.

Usage
-----
    from tool_client import run_session, ToolCall

    for kind, payload in run_session(base_url, model, tools, prompt):
        if kind == "tool_call":
            # payload is a ToolCall dataclass
            print(f"model called {payload.name}({payload.arguments})")
        elif kind == "tool_result":
            # payload is the JSON string injected back as the tool's answer
            print(f"auto-result: {payload}")
        elif kind == "final":
            # payload is the assistant's final text reply
            print(f"answer: {payload}")

Auto-generated results
----------------------
When the server calls a tool, this client has no real implementation to invoke.
Instead it injects a deterministic placeholder so the conversation can always
complete.  The placeholder JSON is::

    {"result": "<tool_name: auto-generated demo result>"}

The caller (AppController) can inspect the yielded ("tool_result", str) step
and replace the placeholder with a real implementation if desired.
"""
import json
from dataclasses import dataclass
from typing import Iterator

import httpx


@dataclass
class ToolCall:
    """One tool invocation the model requested.

    Attributes
    ----------
    id:         Opaque identifier assigned by the model (used as tool_call_id
                when submitting the result back in the next turn).
    name:       Name of the function the model wants to call.
    arguments:  JSON-encoded argument object as a raw string (not parsed).
    """
    id: str
    name: str
    arguments: str  # JSON string — parse with json.loads() if needed


def run_session(
    base_url: str,
    model: str,
    tools: list,
    prompt: str,
) -> Iterator[tuple]:
    """Drive a multi-turn conversation until the model produces a final text reply.

    Parameters
    ----------
    base_url:
        Root URL of an OpenAI-compatible server, e.g. ``"http://localhost:8000"``.
        The path ``/v1/chat/completions`` is appended automatically.
    model:
        Model name to pass in the ``model`` field of each request.
    tools:
        List of tool definitions in OpenAI function-calling format.
        Passed verbatim in every request with ``tool_choice="auto"``.
    prompt:
        The initial user message that starts the conversation.

    Yields
    ------
    ("tool_call",   ToolCall)
        The model requested a tool call.  ``payload.name`` and
        ``payload.arguments`` identify what should be executed.

    ("tool_result", str)
        A placeholder result JSON string was automatically injected into the
        conversation so the round-trip can complete without a live tool backend.

    ("final", str)
        The assistant's final non-tool text reply.  This is always the last
        item in the sequence.

    Raises
    ------
    httpx.HTTPStatusError
        If the server returns a non-2xx response.  Propagated from
        ``resp.raise_for_status()``.
    httpx.RequestError
        On network-level errors (connection refused, timeout, etc.).

    Notes
    -----
    * Runs synchronously; safe to call from a ``threading.Thread``.
    * Uses a single ``httpx.Client`` for all turns (connection reuse).
    * Timeout is set to 120 s per request — suitable for large models with
      slow first-token latency.
    """
    # Conversation history, accumulated across turns.
    # Starts with the user's prompt; grows as the model and tool turns are added.
    messages: list = [{"role": "user", "content": prompt}]

    with httpx.Client(timeout=120.0) as client:
        while True:
            # ----------------------------------------------------------------
            # POST to the chat completions endpoint with the full history.
            # ----------------------------------------------------------------
            resp = client.post(
                f"{base_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]

            tool_calls = msg.get("tool_calls")
            if tool_calls:
                # ----------------------------------------------------------------
                # The model wants to call one or more tools.
                # Append the raw assistant message (with tool_calls) to history
                # so subsequent turns see the full context.
                # ----------------------------------------------------------------
                messages.append(msg)

                for tc in tool_calls:
                    # Build a ToolCall dataclass for the caller.
                    call = ToolCall(
                        id=tc.get("id", ""),
                        name=tc["function"]["name"],
                        arguments=tc["function"]["arguments"],
                    )
                    yield ("tool_call", call)

                    # Auto-generate a deterministic placeholder result.
                    # The caller may inspect the ("tool_result", str) step and
                    # substitute a real result by modifying the last messages entry,
                    # but for demo/test purposes the placeholder is sufficient.
                    result = json.dumps(
                        {"result": f"<{call.name}: auto-generated demo result>"}
                    )
                    yield ("tool_result", result)

                    # Inject the tool result so the model can incorporate it
                    # in its next reply.  Each tool call requires a separate
                    # "tool" role message referencing the matching tool_call_id.
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    })

                # Continue the while loop — send the updated history back to the
                # model so it can produce the next turn (possibly more tools or
                # a final text reply).

            else:
                # ----------------------------------------------------------------
                # No tool calls: the model has produced its final text reply.
                # Yield it and terminate the loop.
                # ----------------------------------------------------------------
                yield ("final", msg.get("content", ""))
                break
