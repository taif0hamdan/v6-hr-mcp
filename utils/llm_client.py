"""
Shared LLM client configuration.

Centralizes how every caller (QueryParser, APIRequestParser, the two
generate_*_schema.py scripts) resolves LLM connection settings, so there is
one place that implements the air-gapped-by-default policy:

  - LLM_BASE_URL (new, preferred name) overrides LLM_API_BASE (legacy name,
    kept for backward compatibility) overrides config.yaml's llm.api_base.
  - If nothing is configured at all, default to a local endpoint
    (DEFAULT_LOCAL_BASE_URL) rather than falling through to the OpenAI SDK's
    own default of https://api.openai.com/v1 - that fallback would silently
    break air-gapped operation.
  - LLM_API_KEY is optional: many local OpenAI-compatible servers (Ollama,
    OpenWebUI, vLLM, llama.cpp server) don't check it.
"""

import os
from typing import Any, Dict

# NOTE: the `openai` package is imported lazily inside build_openai_client(),
# not here, so resolve_llm_settings() (pure dict/env logic, no I/O) stays
# importable and unit-testable without the openai package installed.

# A local OpenAI-compatible endpoint. Matches the shipped config.yaml example
# (OpenWebUI on localhost:8080). Only used if no base URL is configured
# anywhere - normal operation should set LLM_BASE_URL or llm.api_base
# explicitly for whatever local/offline LLM server is actually in use.
DEFAULT_LOCAL_BASE_URL = "http://localhost:8080/v1"

# Local/CPU-partial models can be very slow on the first call (model load +
# compute). The openai SDK's own default is 10 minutes, but we set an
# explicit, configurable floor here so a slow deployment doesn't need code
# changes - just LLM_TIMEOUT_SECONDS.
DEFAULT_TIMEOUT_SECONDS = 60.0


def resolve_llm_settings(llm_config: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve effective LLM settings. Priority: env (new name) -> env (legacy name) -> config.yaml -> local default."""
    base_url = (
        os.getenv("LLM_BASE_URL")
        or os.getenv("LLM_API_BASE")  # legacy name, kept for backward compatibility
        or llm_config.get("api_base")
        or DEFAULT_LOCAL_BASE_URL
    )
    api_key = os.getenv("LLM_API_KEY") or llm_config.get("api_key_env") or "dummy-key"
    model = os.getenv("LLM_MODEL") or llm_config.get("model") or "gpt-3.5-turbo"
    temperature = float(os.getenv("LLM_TEMPERATURE") or llm_config.get("temperature") or 0.0)
    max_tokens = int(os.getenv("LLM_MAX_TOKENS") or llm_config.get("max_tokens") or 500)
    timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS") or llm_config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)

    # Ollama-specific extras, passed through the OpenAI-compatible endpoint
    # via extra_body (the standard chat/completions schema has no fields for
    # either of these - Ollama accepts them as top-level additions).
    num_ctx_raw = os.getenv("LLM_NUM_CTX") or llm_config.get("num_ctx")
    num_ctx = int(num_ctx_raw) if num_ctx_raw else None

    disable_thinking_raw = os.getenv("LLM_DISABLE_THINKING")
    if disable_thinking_raw is not None:
        disable_thinking = disable_thinking_raw.lower() == "true"
    else:
        disable_thinking = bool(llm_config.get("disable_thinking", False))

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout_seconds": timeout_seconds,
        "num_ctx": num_ctx,
        "disable_thinking": disable_thinking,
    }


def build_openai_client(settings: Dict[str, Any]):
    """Build an OpenAI-compatible client from resolved settings. Never logs the api_key."""
    from openai import OpenAI

    return OpenAI(
        api_key=settings["api_key"],
        base_url=settings["base_url"],
        timeout=settings.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
    )


def build_extra_body(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ollama-specific fields to pass through via the openai SDK's extra_body
    kwarg on chat.completions.create() - not part of the standard OpenAI
    schema, so they can't be passed as named create() arguments.
    """
    extra_body: Dict[str, Any] = {}
    if settings.get("num_ctx"):
        extra_body["options"] = {"num_ctx": settings["num_ctx"]}
    if settings.get("disable_thinking"):
        # Supported directly by recent Ollama versions over the OpenAI-
        # compatible endpoint. Harmless no-op if the server ignores it.
        extra_body["think"] = False
    return extra_body


def disable_thinking_suffix(settings: Dict[str, Any]) -> str:
    """
    Qwen's chat template honors a literal "/no_think" directive in the
    prompt text itself to suppress its <think> block - a second, more
    broadly compatible mechanism alongside extra_body's "think": false,
    for servers/model templates that only respect the in-text form.
    """
    return "\n\n/no_think" if settings.get("disable_thinking") else ""
