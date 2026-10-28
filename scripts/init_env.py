"""Create a .env file with placeholders:  python -m scripts.init_env
Works on Mac, Linux and Windows (no cp/copy needed, nothing hidden to find).
Then open .env in an editor and replace each xxxx with your real key."""
from __future__ import annotations

import os
import sys

from agent.config import ROOT

TEMPLATE = """\
# Your private keys. This file is git-ignored. NEVER commit it or share it.
# xxxx means "not set". Replace xxxx with your real value. No quotes or spaces.

GEMINI_API_KEY=xxxx
GEMINI_MODELS=xxxx
TAVILY_API_KEY=xxxx

# Optional
JINA_API_KEY=xxxx
COMPOSIO_API_KEY=xxxx
SUPABASE_URL=xxxx
SUPABASE_SERVICE_KEY=xxxx

# Limits that protect your free quota
RATE_LIMIT_PER_IP=4
DAILY_GENERATION_CAP=60
TRUST_PROXY=false
"""


def main() -> int:
    path = ROOT / ".env"
    if path.exists() and "--force" not in sys.argv:
        print(f"{path} already exists, left untouched. Use --force to overwrite it with placeholders.")
        return 0
    path.write_text(TEMPLATE, encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # owner-only; no-op on Windows
    except OSError:
        pass
    print(f"Created {path}\nNext: open it in an editor (it starts with a dot, so Finder/Explorer may hide it) and replace each xxxx with your real key.")
    print("Then run: python -m scripts.check_setup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
