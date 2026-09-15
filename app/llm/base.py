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

SYSTEM_PROMPT = """You are a careful, knowledgeable reading assistant embedded in a PDF reader, helping the user understand what they're reading. The user has selected a specific span of text on one page (the "anchor") and asked a question about it.

Rules:
- Ground your answer in the anchor text and the surrounding page text — that's what the user is looking at, so start there and quote it when it clarifies the answer.
- The provided text is what the user selected, not the limit of what you're allowed to know. When the page states a result without spelling out the reasoning (e.g. a derivation, a proof, a worked example, background on a term it assumes the reader already knows), work it out or explain it yourself using your own knowledge, consistent with the notation and definitions already established earlier in the page/book when possible. This is the main way you're useful — don't refuse just because the derivation itself isn't printed on the page.
- Be clear about what's your own explanation versus what the page itself says, when the distinction matters — e.g. "the page doesn't spell this out, but here's the derivation:" rather than implying the book states something it doesn't.
- Don't invent claims about what the text says. If you're not sure what the anchor is even about, say so — but that's different from refusing to explain a concept the text merely doesn't elaborate on.
- The user is anchored to one specific page; do not pretend to know about other pages of this specific document unless context is provided.
- Use plain prose. Avoid bullet lists unless they genuinely help. No headings unless the answer is long. Use LaTeX ($...$ / $$...$$) for math.
"""


@dataclass(frozen=True)
class Provider:
    name: str
    endpoint: str
    model: str
    api_key: str
    extra_headers: dict | None = None


# Reasoning-model tag names some providers leak into the content stream
# instead of a separate reasoning_content field — e.g. MiniMax M3 uses
# <think>, some Gemma variants use <thought>. Stripping is always on and
# is a no-op when a tag never appears, so this list covers any provider/
# model without per-provider configuration.
REASONING_TAGS = ("think", "thought")


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


def _reasoning_tag_stripper(tags: tuple[str, ...] = REASONING_TAGS):
    """Mirrors minimax_client.py's `_strip`, generalized to any of `tags`
    (e.g. <think>, <thought>) instead of just <think>. Only one tag can be
    "open" at a time — reasoning blocks don't nest different tag names."""
    state = {"open_tag": None}

    def strip(text: str) -> str:
        out = text
        if state["open_tag"] is None:
            earliest = None
            tag_name = None
            tag_len = 0
            for t in tags:
                marker = f"<{t}>"
                i = out.find(marker)
                if i != -1 and (earliest is None or i < earliest):
                    earliest, tag_name, tag_len = i, t, len(marker)
            if earliest is None:
                return out
            before = out[:earliest]
            state["open_tag"] = tag_name
            # The close tag (and real content after it) may be in this same
            # chunk — keep processing instead of dropping the rest.
            return before + strip(out[earliest + tag_len:])
        close = f"</{state['open_tag']}>"
        i = out.find(close)
        if i == -1:
            return ""
        state["open_tag"] = None
        return strip(out[i + len(close):])

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

    strip = _reasoning_tag_stripper()

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
                        cleaned = strip(content)
                        if cleaned:
                            yield {"type": "token", "text": cleaned}
                yield {"type": "done"}
    except httpx.HTTPError as e:
        yield {"type": "error", "message": str(e)}
