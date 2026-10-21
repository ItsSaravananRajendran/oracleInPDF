"""Shared sync SSE streaming against an OpenAI-compatible /chat/completions
endpoint. Text-only in the common path: the anchor is sent as extracted
PDF text. Image-region anchors (Ctrl+drag a rectangle on a page; see
reader/view.py) instead attach a PNG crop as a multimodal `image_url`
content part alongside the page's extracted text — all three configured
providers (OpenRouter, Google AI Studio, MiniMax) accept the
OpenAI-compatible image_url format natively.

Ported + collapsed from anchored-pdf-reader's openrouter_client.py /
google_client.py / minimax_client.py, which were ~95% identical. Runs
synchronously (httpx.Client) because callers drive it from a worker thread
and push events back to the GTK main loop via GLib.idle_add — no asyncio
loop needed in a desktop app.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import httpx

SYSTEM_PROMPT = """You are a careful, knowledgeable reading assistant embedded in a PDF reader, helping the user understand what they're reading. The user has anchored their question to one specific page: either by selecting a span of text on it ("text anchor") or by Ctrl+drag-capturing a rectangular image region of it ("image anchor"). They've asked a question about that anchor.

Rules:
- Ground your answer in the anchor and the surrounding page text — that's what the user is looking at, so start there and quote it when it clarifies the answer.
- For image anchors: a screenshot of the region is attached to the message. Treat that image as the primary ground truth for what's being asked about, with the surrounding page text as supplementary context (the page text covers the full page, so it tells you what surrounds the region and may repeat content from inside it).
- For text anchors: the selected span is the primary ground truth; the page text is supplementary context.
- The provided text and image are what the user is looking at, not the limit of what you're allowed to know. When the page states a result without spelling out the reasoning (e.g. a derivation, a proof, a worked example, background on a term it assumes the reader already knows), work it out or explain it yourself using your own knowledge, consistent with the notation and definitions already established earlier in the page/book when possible. This is the main way you're useful — don't refuse just because the derivation itself isn't printed on the page.
- Be clear about what's your own explanation versus what the page itself says, when the distinction matters — e.g. "the page doesn't spell this out, but here's the derivation:" rather than implying the book states something it doesn't.
- Don't invent claims about what the text or image contains. If you're not sure what the anchor is even about, say so — but that's different from refusing to explain a concept the text merely doesn't elaborate on.
- The user is anchored to one specific page; do not pretend to know about other pages of this specific document unless context is provided.
- Use plain prose. Avoid bullet lists unless they genuinely help. No headings unless the answer is long. Use LaTeX ($...$ / $$...$$) for math.
"""


DEFAULT_MAX_TOKENS = 8192


@dataclass(frozen=True)
class Provider:
    name: str
    endpoint: str
    model: str
    api_key: str
    extra_headers: dict | None = None
    # Reasoning-capable models spend hidden "thinking" tokens out of this
    # same budget before any visible content (see stream_chat) — how much
    # of that a model needs varies a lot by provider, so this is
    # per-provider rather than one constant. MiniMax's M1 in particular
    # needs a much larger budget (see llm/providers.py) than the default.
    max_tokens: int = DEFAULT_MAX_TOKENS


# Reasoning-model tag names some providers leak into the content stream
# instead of a separate reasoning_content field — e.g. MiniMax M3 uses
# <think>, some Gemma variants use <thought>. Stripping is always on and
# is a no-op when a tag never appears, so this list covers any provider/
# model without per-provider configuration.
REASONING_TAGS = ("think", "thought")

TRUNCATION_NOTICE = "\n\n*(Response cut off at the length limit — ask it to continue, or narrow the question.)*"

# HTTP statuses where the *identical* request can plausibly succeed a
# moment later, so it's worth sending again: overload (529), rate
# limiting (429), request/gateway timeouts (408/504), and the bare 5xx
# family that includes the 503 "high demand, try again later" these
# providers emit under load.
#
# Deliberately excluded: 400/401/403/404/422. Those mean the request or
# the credentials are wrong, so a retry re-sends a request that is
# guaranteed to be rejected the same way — it only delays the real error
# from reaching the user (and on a 429, retrying makes the limit worse).
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})


def _user_content(anchor_text: Optional[str], page_text: Optional[str],
                  question: str, image_data_url: Optional[str] = None):
    """Build the user-role message content. Returns either a plain
    string (text-only path, what every text anchor call uses) or a
    list of content parts (text + image_url) for image-anchor calls.

    The conditional shape is intentional: most calls go through the
    text-only path, and OpenAI-style providers are happy to receive a
    string in a content field. The multimodal path uses a list so
    multiple content parts can be combined. `_serialize_content`
    flattens whichever shape we have into the JSON the wire format
    expects.

    `image_data_url` is expected to be a fully-formed `data:` URL
    (e.g. `data:image/png;base64,iVBOR...`) — this function does not
    encode bytes for you, so the caller (ChatPanel) handles base64
    encoding with its own logging/error surface.
    """
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
    text = "\n\n".join(lines)
    if image_data_url is None:
        return text
    # Multimodal: keep the text part first so models that prefer early
    # text (most do) see the question first, with the image as supporting
    # evidence. The image_url type follows the OpenAI-compatible spec,
    # which all three configured providers (OpenRouter, Google AI Studio,
    # MiniMax) accept natively.
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]


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
    image_data_url: Optional[str] = None,
) -> Iterator[dict]:
    """Yields {"type": "token"|"error"|"done", ...} dicts. Blocking — call
    from a worker thread, not the GTK main thread.

    Error events carry two extra fields beyond `message`, so a caller can
    decide whether re-sending the request could plausibly help:
    `status` (the HTTP code, or None when no response arrived) and
    `retryable` (True only for transient conditions — see
    RETRYABLE_STATUS). `stream_chat_with_retry` below is the wrapper that
    acts on them; this function stays single-shot so it can also be used
    directly where retry isn't wanted.

    `image_data_url`, if supplied, is attached as a multimodal content
    part to the user message — used by the Ctrl+drag image-region
    feature (see reader/view.py). Must be a fully-formed `data:` URL;
    the caller is responsible for base64-encoding the PNG bytes."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history[-16:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({
        "role": "user",
        "content": _user_content(anchor_text, page_text, question, image_data_url),
    })

    body = {
        "model": provider.model,
        # Reasoning-capable models spend hidden "thinking" tokens out of
        # this same budget before any visible content — 2048 was observed
        # (live) to be fully consumed by thinking alone on a demanding
        # question, producing zero visible output despite streaming
        # normally otherwise. provider.max_tokens leaves real headroom for
        # both, sized per-provider since how much thinking a model needs
        # varies a lot; still finite, so an intentionally unbounded ask
        # ("explain literally everything") can still hit the cap —
        # handled below by surfacing finish_reason == "length" instead of
        # just silently ending.
        "max_tokens": provider.max_tokens,
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
                    yield {
                        "type": "error",
                        "message": f"HTTP {r.status_code}: {msg}",
                        "status": r.status_code,
                        "retryable": r.status_code in RETRYABLE_STATUS,
                    }
                    return

                truncated = False
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        leftover = stripper.flush()
                        if leftover:
                            yield {"type": "token", "text": leftover}
                        if truncated:
                            yield {"type": "token", "text": TRUNCATION_NOTICE}
                        yield {"type": "done"}
                        return
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choice = (obj.get("choices") or [{}])[0]
                    if choice.get("finish_reason") == "length":
                        truncated = True
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        cleaned = stripper.feed(content)
                        if cleaned:
                            yield {"type": "token", "text": cleaned}
                leftover = stripper.flush()
                if leftover:
                    yield {"type": "token", "text": leftover}
                if truncated:
                    yield {"type": "token", "text": TRUNCATION_NOTICE}
                yield {"type": "done"}
    except httpx.HTTPError as e:
        # No HTTP response at all — DNS, connect, TLS, or a read timeout.
        # Nothing about the request itself is implicated (we never even got
        # to a verdict on it), so this is always worth another attempt.
        yield {"type": "error", "message": str(e), "status": None, "retryable": True}


def stream_chat_with_retry(
    provider: Provider,
    question: str,
    anchor_text: Optional[str],
    page_text: Optional[str],
    history: list[dict],
    image_data_url: Optional[str] = None,
    max_attempts: int = 3,
    base_delay: float = 1.5,
    max_delay: float = 20.0,
) -> Iterator[dict]:
    """`stream_chat` with exponential backoff over transient failures.

    Emits the same event shapes as `stream_chat`, plus:
      - {"type": "retry", "attempt", "max_attempts", "delay", "message"}
        yielded *before* each backoff sleep, so a UI can tell the user
        what's happening instead of the request just appearing to hang.
        `attempt` is the attempt that just failed, so the next one is
        `attempt + 1`.

    Only failures flagged `retryable` by `stream_chat` are retried (see
    RETRYABLE_STATUS) — a 400/401 comes straight back to the caller, as
    does a non-retryable error on the final attempt. That final error's
    message gets the attempt count appended, so the user isn't left
    guessing whether anything was retried at their request.

    A retry restarts the whole turn rather than resuming mid-stream, so
    the caller must discard any tokens it has accumulated for the failed
    attempt — the "retry" event is the signal to do that. (See
    `_stream_worker` in chat/panel.py, which relies on the assistant
    bubble being replaced wholesale on each token update.)

    `max_attempts` counts total attempts, so 1 disables retrying. Delays
    are `base_delay * 2**(attempt-1)`, capped at `max_delay`.
    """
    attempt = 0
    while True:
        attempt += 1
        failure = None
        gen = stream_chat(provider, question, anchor_text, page_text, history, image_data_url)
        try:
            for event in gen:
                if event["type"] == "error":
                    failure = event
                    break
                yield event
        finally:
            # `stream_chat` is itself a generator; breaking out of the loop
            # above leaves it suspended mid-body, holding an open httpx
            # connection. Close it deterministically instead of waiting
            # for the GC.
            gen.close()

        if failure is None:
            return
        if not failure.get("retryable") or attempt >= max_attempts:
            if attempt > 1:
                failure = dict(
                    failure,
                    message=f"{failure['message']} (gave up after {attempt} attempts)",
                )
            yield failure
            return

        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
        yield {
            "type": "retry",
            "attempt": attempt,
            "max_attempts": max_attempts,
            "delay": delay,
            "message": failure["message"],
        }
        time.sleep(delay)
