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


class ReasoningTagStripper:
    """Strips <think>/<thought>-style reasoning blocks from a streamed
    response. Generalizes minimax_client.py's original `_strip` to any of
    `tags` and — the important difference — buffers suppressed text
    instead of discarding it as it's seen.

    Why buffering matters: MiniMax has been observed (live, via this app)
    to open a <think> tag and then never close it for the rest of the
    stream — the actual final answer ends up inside the "still open" tag
    with no closing marker ever arriving. A discard-on-sight stripper
    loses that answer permanently and silently. Buffering lets flush()
    release it instead when the stream ends without a close tag."""

    def __init__(self, tags: tuple[str, ...] = REASONING_TAGS):
        self.tags = tags
        self.open_tag: str | None = None
        self.buffer = ""

    def feed(self, text: str) -> str:
        out = ""
        remaining = text
        while remaining:
            if self.open_tag is None:
                earliest = None
                tag_name = None
                tag_len = 0
                for t in self.tags:
                    marker = f"<{t}>"
                    i = remaining.find(marker)
                    if i != -1 and (earliest is None or i < earliest):
                        earliest, tag_name, tag_len = i, t, len(marker)
                if earliest is None:
                    out += remaining
                    remaining = ""
                else:
                    out += remaining[:earliest]
                    self.open_tag = tag_name
                    self.buffer = ""
                    remaining = remaining[earliest + tag_len:]
            else:
                close = f"</{self.open_tag}>"
                i = remaining.find(close)
                if i == -1:
                    self.buffer += remaining
                    remaining = ""
                else:
                    # Everything up to the close tag is real reasoning —
                    # discarded on purpose, the tag closed as expected.
                    self.open_tag = None
                    self.buffer = ""
                    remaining = remaining[i + len(close):]
        return out

    def flush(self) -> str:
        """Call once the stream has ended. Releases buffered content only
        if a tag was left open — i.e. the close tag never arrived — so
        the response doesn't just vanish."""
        if self.open_tag is not None:
            leftover = self.buffer
            self.open_tag = None
            self.buffer = ""
            return leftover
        return ""


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

    stripper = ReasoningTagStripper()

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
                        leftover = stripper.flush()
                        if leftover:
                            yield {"type": "token", "text": leftover}
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
                        cleaned = stripper.feed(content)
                        if cleaned:
                            yield {"type": "token", "text": cleaned}
                leftover = stripper.flush()
                if leftover:
                    yield {"type": "token", "text": leftover}
                yield {"type": "done"}
    except httpx.HTTPError as e:
        yield {"type": "error", "message": str(e)}
