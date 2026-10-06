"""
Natural Language to API Request Parser
Uses LLM to convert natural language questions into API requests based on OpenAPI spec.
"""

from typing import Dict, Any, Optional
import logging
import json
import re
import yaml
from openai import APIConnectionError, APIError, APITimeoutError

from utils.llm_client import build_extra_body, build_openai_client, disable_thinking_suffix, resolve_llm_settings
from utils.schema import get_safety_rules

logger = logging.getLogger(__name__)

class APIRequestParser:
    """Converts natural language to API requests using LLM and OpenAPI schema."""

    def __init__(self, llm_config: Dict[str, Any], schema: Dict[str, Any], unsafe_mode: bool = False):
        """
        Initialize API parser with LLM configuration.

        Args:
            llm_config: LLM settings
            schema: OpenAPI schema from APIAdapter
            unsafe_mode: If True, allows non-GET requests
        """
        self.llm_config = llm_config
        self.schema = schema
        self.unsafe_mode = unsafe_mode

        settings = resolve_llm_settings(llm_config)
        self.client = build_openai_client(settings)
        self.model = settings["model"]
        self.temperature = settings["temperature"]
        self.max_tokens = settings["max_tokens"]
        self.extra_body = build_extra_body(settings) or None
        self.no_think_suffix = disable_thinking_suffix(settings)

    def parse(self, natural_language: str) -> Dict[str, Any]:
        """
        Convert natural language to API Request definition.

        Returns:
            Dict with 'success' and either request fields, or 'error_code' + 'error'.
            error_code is one of "LLM_UNAVAILABLE" | "QUERY_REJECTED" | "INTERNAL".
        """
        try:
            prompt = self._build_prompt(natural_language) + self.no_think_suffix

            create_kwargs = dict(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert API client. Convert natural language to structured API requests. Return ONLY valid JSON."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format={ "type": "json_object" }
            )
            if self.extra_body:
                create_kwargs["extra_body"] = self.extra_body

            response = self.client.chat.completions.create(**create_kwargs)
        except (APIConnectionError, APITimeoutError, APIError) as exc:
            status_code = getattr(exc, "status_code", None)
            logger.error("LLM call failed: %s (status_code=%s)", type(exc).__name__, status_code, exc_info=True)
            return {"success": False, "error_code": "LLM_UNAVAILABLE", "error": "the configured LLM endpoint is unavailable", "natural_language": natural_language}
        except Exception:
            logger.error("Unexpected error calling the LLM", exc_info=True)
            return {"success": False, "error_code": "INTERNAL", "error": "unexpected error while generating the API request", "natural_language": natural_language}

        try:
            content = response.choices[0].message.content.strip()
            request_def = json.loads(content)

            # Validate Method Safety
            method = request_def.get("method", "GET").upper()
            if not self.unsafe_mode and method != "GET":
                return {
                    "success": False,
                    "error_code": "QUERY_REJECTED",
                    "error": f"method {method} is not allowed in safe mode; only GET requests are permitted",
                    "natural_language": natural_language
                }

            return {
                "success": True,
                "method": method,
                "endpoint": request_def.get("endpoint"),
                "params": request_def.get("params", {}),
                "body": request_def.get("body", None),
                "natural_language": natural_language
            }

        except Exception:
            logger.error("Failed to process LLM response", exc_info=True)
            return {
                "success": False,
                "error_code": "INTERNAL",
                "error": "unexpected error while generating the API request",
                "natural_language": natural_language
            }

    def _build_prompt(self, natural_language: str) -> str:
        """Build prompt with condensed schema context."""

        # We need to simplify the schema for the prompt to save tokens
        # Extract paths and their methods/descriptions
        paths_summary = {}
        for path, methods in self.schema.get("paths", {}).items():
            paths_summary[path] = {}
            for method, details in methods.items():
                paths_summary[path][method] = {
                    "summary": details.get("summary", ""),
                    "description": details.get("description", ""),
                    "parameters": [p.get("name") for p in details.get("parameters", [])]
                }

        # Load external API context if available (generated by generate_api_schema.py)
        context_text = self._build_api_context()

        prompt = f"""
You are an intelligent API Client. Your task is to convert a natural language request into a specific API call based on the provided OpenAPI specification.

CONTEXT:
{context_text}

AVAILABLE ENDPOINTS (Simplified):
{json.dumps(paths_summary, indent=2)}

USER REQUEST: "{natural_language}"

INSTRUCTIONS:
1. Identify the most appropriate endpoint and HTTP method.
2. Extract parameters for the query string ('params') or request body ('body').
3. If no matching endpoint is found, return an error in the JSON.
4. If the user asks for something dangerous (DELETE, POST) and it's available, include it (safety checks are done elsewhere).

OUTPUT FORMAT (JSON ONLY):
{{
  "method": "GET | POST | ...",
  "endpoint": "/path/to/resource",
  "params": {{ "key": "value" }},  // Query parameters
  "body": {{ "key": "value" }}     // JSON body (if needed)
}}
"""
        return prompt

    def _build_api_context(self) -> str:
        """Build API context section from config (if available)."""
        api_context = self.llm_config.get("database_context", {}) # Reusing the key name or we can change logic in main
        # Actually, main.py injects "database_context". I should probably rename it to "context" or "schema_context" generally,
        # but for now let's assume `api_context` might be passed in llm_config under a generic key or `api_context`.
        # Let's check how main.py loads it. It loads `database_schema.yaml` into `database_context`.
        # I will likely change main.py to load `api_schema.yaml` into `api_context` or just `schema_context`.

        # For now, let's look for `api_context` or fall back to `database_context`
        context = self.llm_config.get("api_context") or self.llm_config.get("database_context", {})

        if not context:
            return ""

        return yaml.dump(context)
