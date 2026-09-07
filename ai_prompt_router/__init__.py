"""AI Prompt Router — one HTTP contract in front of N bridges.

Deliberately kept import-light: importing this package must not drag in fastapi or
httpx, so `from ai_prompt_router.config import load_config` works in a thin context
(a config-lint script, a test collecting only the pure modules).
"""

__version__ = "1.0.0"
