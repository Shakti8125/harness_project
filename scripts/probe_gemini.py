"""Probe the configured Gemini model with one tiny request. **Costs one request.**

    uv run python scripts/probe_gemini.py [--model gemini-3.6-flash]

Sends `Reply with the single word: ok` to the model the settings name (or `--model`) and
prints either

    OK: <text> | <usage metadata>

and exits 0, or

    ERROR: <exception type> | <provider message, key scrubbed>

and exits 1. Use it on an overloaded evening before spending a replay: a `503` answer
means wait 30-60 minutes (the free tier bills a `503` like a success), a `429` means the
Pacific day is spent. Run `scripts/quota_ledger.py` first; this request counts too, and
no database records it.

The key and model come from `get_settings()` (only `src/settings.py` reads the
environment). The message is scrubbed of the key's stripped value and of both key shapes
before it is printed: the deployed key is `AQ.`-shaped, which the app's own patterns do
not cover yet (security assessment SEC-13).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.settings import get_settings  # noqa: E402

PROMPT = "Reply with the single word: ok"
KEY_SHAPES = re.compile(r"AIza[0-9A-Za-z_\-]{35}|AQ\.[A-Za-z0-9_\-]{20,}")
MAX_MESSAGE_CHARS = 600


def scrub(text: str, key: str) -> str:
    """`text` without the key: its stripped value, then anything shaped like a key."""
    stripped = key.strip()
    if stripped:
        text = text.replace(stripped, "***")
    return KEY_SHAPES.sub("***", text)


async def probe(model: str, key: str) -> tuple[bool, str]:
    from google import genai  # noqa: PLC0415 - the SDK import is slow; keep --help fast

    client = genai.Client(api_key=key.strip())
    try:
        response = await client.aio.models.generate_content(model=model, contents=PROMPT)
    except Exception as exc:  # noqa: BLE001 - every failure is a result to print
        message = scrub(str(exc), key)[:MAX_MESSAGE_CHARS]
        return False, f"ERROR: {type(exc).__name__} | {message}"
    text = scrub((response.text or "").strip(), key)[:40]
    return True, f"OK: {text} | {response.usage_metadata}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default=None, help="override HARNESS_GEMINI_MODEL")
    args = parser.parse_args()
    settings = get_settings()
    model = args.model or settings.gemini_model
    print(f"model: {model} (one request)")
    ok, line = asyncio.run(probe(model, settings.gemini_api_key.get_secret_value()))
    print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
