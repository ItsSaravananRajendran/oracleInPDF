"""Provider selection: OpenRouter > Google AI Studio > MiniMax, whichever
API key is configured first, matching the webapp's priority order."""
from __future__ import annotations

from app import settings
from app.llm.base import Provider


def active_provider() -> Provider | None:
    if settings.OPENROUTER_API_KEY:
        return Provider(
            name="openrouter",
            endpoint="https://openrouter.ai/api/v1/chat/completions",
            model=settings.OPENROUTER_MODEL,
            api_key=settings.OPENROUTER_API_KEY,
            extra_headers={
                "HTTP-Referer": "https://github.com/ItsSaravananRajendran/customPdfReader",
                "X-Title": "Custom PDF Reader",
            },
        )
    if settings.GOOGLE_AI_STUDIO_API_KEY:
        return Provider(
            name="google",
            endpoint="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            model=settings.GOOGLE_MODEL,
            api_key=settings.GOOGLE_AI_STUDIO_API_KEY,
        )
    if settings.MINIMAX_API_KEY:
        return Provider(
            name="minimax",
            endpoint="https://api.minimax.io/v1/chat/completions",
            model=settings.MINIMAX_MODEL,
            api_key=settings.MINIMAX_API_KEY,
        )
    return None
