"""High-level agent orchestration for CapitalArc.

This package hosts the top-level autonomous agent loop that:
    1. Builds a market context (prices, on-chain snapshot).
    2. Runs the `DecisionEngine` (Level 1 + Level 2 + Level 3).
    3. Hands the result to the allocation router for execution.

LLM provider code lives in `src.llm` (currently Gemini 2.5 Flash).
"""
