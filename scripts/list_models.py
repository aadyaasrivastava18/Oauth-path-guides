"""List Gemini models your key can call:  python -m scripts.list_models
Pick one or two and put them in GEMINI_MODELS (comma separated). Prefer 'flash'-class models for the free tier."""
from __future__ import annotations

import sys

from agent.config import get_settings


def main() -> int:
    key = get_settings().gemini_api_key
    if not key:
        print("Set GEMINI_API_KEY in .env first.")
        return 2
    from google import genai

    client = genai.Client(api_key=key)
    rows = []
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None)
        if actions is not None and "generateContent" not in actions:
            continue
        rows.append(str(getattr(m, "name", "")).removeprefix("models/"))
    if not rows:
        print("No models returned. Check the key and your region/eligibility at aistudio.google.com.")
        return 1
    print("Models that support generateContent:")
    for r in sorted(rows):
        print(" ", r)
    print("\nThen check each model's free-tier limits at https://aistudio.google.com/rate-limit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
