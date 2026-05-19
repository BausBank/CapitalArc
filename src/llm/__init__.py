"""LLM clients used by CapitalArc.

Currently only Gemini 2.5 Flash (the Level 3 final arbiter) lives here.
Additional model providers, if ever needed, should sit alongside the
Gemini client with a uniform `generate_json(...)` style interface.
"""

from src.llm.gemini_client import GeminiClient, GeminiClientConfig

__all__ = ["GeminiClient", "GeminiClientConfig"]
