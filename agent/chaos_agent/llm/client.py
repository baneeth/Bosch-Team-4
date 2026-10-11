"""OpenAI Agents SDK plumbing: settings from ~/chaos-agent/.env, one bounded agent run per call, transcripts.

Every call is saved to results/ai/<ts>-<name>.json (instructions, input, tool calls, output, model, tokens) so each
AI decision can be shown and audited later. The SDK also uploads a trace to the OpenAI dashboard (Traces) unless
OPENAI_AGENTS_DISABLE_TRACING=1.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from typing import Any

from .. import config as cfg

_env_loaded = False


class AIError(RuntimeError):
    """The AI could not produce an answer (no key, API error, timeout, invalid output)."""


def load_env() -> None:
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    if cfg.ENV_FILE.exists():
        from dotenv import load_dotenv
        load_dotenv(cfg.ENV_FILE, override=False)


def api_key() -> str | None:
    load_env()
    return os.environ.get("OPENAI_API_KEY") or None


def model(fast: bool = False) -> str:
    load_env()
    if fast:
        return os.environ.get("OPENAI_MODEL_FAST") or cfg.DEFAULT_MODEL_FAST
    return os.environ.get("OPENAI_MODEL") or cfg.DEFAULT_MODEL


def effort(agent: str) -> str | None:
    """Reasoning effort for an agent: OPENAI_EFFORT_<AGENT> (none/minimal/low/medium/high), else the default."""
    load_env()
    return os.environ.get(f"OPENAI_EFFORT_{agent.upper()}") or cfg.DEFAULT_EFFORT.get(agent)


def model_settings(agent: str) -> Any:
    from agents import ModelSettings
    from openai.types.shared import Reasoning
    e = effort(agent)
    return ModelSettings(reasoning=Reasoning(effort=e)) if e else ModelSettings()


def masked_key() -> str:
    k = api_key()
    return "not set" if not k else f"{k[:7]}...{k[-4:]} ({len(k)} chars)"


def ai_available() -> bool:
    return api_key() is not None


def strict_output(model_type: type) -> Any:
    """Structured-output schema for the SDK, with keys OpenAI's strict mode rejects ('default') removed.

    Our pydantic models keep their defaults for normal use; the AI simply has to fill every field.
    """
    from agents import AgentOutputSchema

    class _Strict(AgentOutputSchema):
        def json_schema(self) -> dict[str, Any]:
            return _strip(super().json_schema())

    return _Strict(model_type)


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in ("default", "discriminator")}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


async def run_agent(agent: Any, user_input: str, *, name: str, context: Any = None,
                    max_turns: int = cfg.AI_MAX_TURNS, timeout_s: float = cfg.AI_TIMEOUT_S,
                    attempts: int = 2) -> tuple[Any, str]:
    """Run one agent to its final structured output. Returns (output, transcript path). Raises AIError."""
    if not ai_available():
        raise AIError(f"OPENAI_API_KEY is not set (put it in {cfg.ENV_FILE}, chmod 600)")
    from agents import RunConfig, Runner

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                Runner.run(agent, user_input, context=context, max_turns=max_turns,
                           run_config=RunConfig(workflow_name=f"chaos-agent {name}")),
                timeout_s)
        except Exception as e:                       # API error, timeout, max turns, schema mismatch
            last = e
            _save(name, agent, user_input, None, None, time.time() - t0, error=f"attempt {attempt}: {e!r}")
            continue
        path = _save(name, agent, user_input, result, result.final_output, time.time() - t0)
        return result.final_output, path
    raise AIError(f"{name} failed after {attempts} attempts: {last!r}")


def _save(name: str, agent: Any, user_input: str, result: Any, output: Any, seconds: float,
          error: str | None = None) -> str:
    cfg.AI_DIR.mkdir(parents=True, exist_ok=True)
    path = cfg.AI_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.json"
    record = {
        "agent": name, "model": getattr(agent, "model", None),
        "effort": getattr(getattr(getattr(agent, "model_settings", None), "reasoning", None), "effort", None),
        "seconds": round(seconds, 1), "error": error,
        "instructions": agent.instructions if isinstance(agent.instructions, str) else None,
        "input": user_input, "steps": _steps(result) if result is not None else [], "usage": _usage(result),
        "output": output.model_dump(mode="json") if hasattr(output, "model_dump") else output,
    }
    path.write_text(json.dumps(record, indent=2, default=str))
    return str(path.relative_to(cfg.RESULTS_DIR)) if path.is_relative_to(cfg.RESULTS_DIR) else str(path)


def _steps(result: Any) -> list[dict]:
    """Tool calls and their outputs, in order (what the agent looked at before deciding)."""
    steps = []
    for item in getattr(result, "new_items", []) or []:
        kind = getattr(item, "type", type(item).__name__)
        raw = getattr(item, "raw_item", None)
        if kind == "tool_call_item":
            steps.append({"tool": getattr(raw, "name", None), "arguments": getattr(raw, "arguments", None)})
        elif kind == "tool_call_output_item":
            out = str(getattr(item, "output", ""))
            steps.append({"tool_output": out[:2000] + ("..." if len(out) > 2000 else "")})
    return steps


def _usage(result: Any) -> dict:
    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    if usage is None:
        return {}
    return {k: getattr(usage, k, None) for k in ("requests", "input_tokens", "output_tokens", "total_tokens")}


def list_models() -> list[str]:
    """Model ids this key can use (for choosing OPENAI_MODEL / OPENAI_MODEL_FAST)."""
    from openai import OpenAI
    return sorted(m.id for m in OpenAI(api_key=api_key()).models.list())
