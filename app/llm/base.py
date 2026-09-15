"""Shared sync SSE streaming against an OpenAI-compatible /chat/completions
endpoint. Text-only: the anchor is sent as extracted PDF text, never as an
image crop, per the desktop app's design (unlike the webapp original).

Ported + collapsed from anchored-pdf-reader's openrouter_client.py /
google_client.py / minimax_client.py, which were ~95% identical. Runs
synchronously (httpx.Client) because callers drive it from a worker thread
and push events back to the GTK main loop via GLib.idle_add — no asyncio
loop needed in a desktop app.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import httpx

SYSTEM_PROMPT = """You are a careful, focused reading assistant embedded in a PDF reader. The user has selected a specific span of text on one page (the "anchor") and asked a question about it.

Rules:
- Answer using the anchor text and the surrounding page text provided below it.
- Be precise and concise. Quote relevant text from the page when it clarifies the answer.
- If the anchor or question can't be answered from the given text, say so directly. Do not invent content.
- The user is anchored to one specific page; do not pretend to know about other pages unless context is provided.
- Use plain prose. Avoid bullet lists unless they genuinely help. No headings unless the answer is long.
"""


@dataclass(frozen=True)
class Provider:
    name: str
    endpoint: str
    model: str
    api_key: str
    extra_headers: dict | None = None
    # Some providers (MiniMax M3) leak <think>...</think> into the content
    # stream instead of using a separate reasoning_content field.
    strip_think_tags: bool = False


def _user_content(anchor_text: Optional[str], page_text: Optional[str], question: str) -> str:
    lines = []
    if anchor_text:
        lines.append("SELECTED TEXT (the anchor the user is asking about):\n" + anchor_text.strip())
    else:
        lines.append("SELECTED TEXT: (none — general question about the page)")
    if page_text:
        lines.append("SURROUNDING PAGE TEXT:\n" + page_text)
    else:
        lines.append("SURROUNDING PAGE TEXT: (this page has no extractable text layer)")
    lines.append("USER QUESTION:\n" + question)
    return "\n\n".join(lines)


def _think_stripper():
    """Mirrors minimax_client.py's `_strip`: MiniMax M3 sometimes embeds
    <think>...</think> in the content stream instead of reasoning_content."""
    state = {"in_think": False}

    def strip(text: str) -> str:
        out = text
        if not state["in_think"]:
            i = out.find("<think>")
            if i != -1:
                out = out[:i]
                state["in_think"] = True
            return out
        i = out.find("</think>")
        if i != -1:
            out = out[i + len("</think>"):]
            state["in_think"] = False
            return strip(out)
        return ""

    return strip


def stream_chat(
    provider: Provider,
    question: str,
    anchor_text: Optional[str],
    page_text: Optional[str],
    history: list[dict],
) -> Iterator[dict]:
    """Yields {"type": "token"|"error"|"done", ...} dicts. Blocking — call
    from a worker thread, not the GTK main thread."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history[-16:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": _user_content(anchor_text, page_text, question)})

    body = {
        "model": provider.model,
        "max_tokens": 2048,
        "temperature": 0.3,
        "stream": True,
        "messages": messages,
    }
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **(provider.extra_headers or {}),
    }

    strip = _think_stripper() if provider.strip_think_tags else None

    try:
        with httpx.Client(timeout=httpx.Timeout(120.0, read=120.0)) as client:
            with client.stream("POST", provider.endpoint, headers=headers, json=body) as r:
                if r.status_code >= 400:
                    err_body = r.read()
                    try:
                        err = json.loads(err_body)
                        msg = err.get("error", {}).get("message") or err.get("message") or err_body.decode("utf-8", "replace")
                    except Exception:
                        msg = err_body.decode("utf-8", "replace")
                    yield {"type": "error", "message": f"HTTP {r.status_code}: {msg}"}
                    return

                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        yield {"type": "done"}
                        return
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choice = (obj.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        cleaned = strip(content) if strip else content
                        if cleaned:
                            yield {"type": "token", "text": cleaned}
                yield {"type": "done"}
    except httpx.HTTPError as e:
        yield {"type": "error", "message": str(e)}
