from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field


MAX_AGENT_STEPS = 8
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MODEL = "llama-3.3-70b-versatile"
DEFAULT_FALLBACK_MODEL = "llama-3.1-8b-instant"


class GroqOrchestrationError(RuntimeError):
    """A controlled failure from the LLM orchestration layer."""


class AgentFinalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explanation: str = Field(min_length=1)
    status: str = Field(min_length=1)
    action: str = Field(min_length=1)


@dataclass
class TraceEvent:
    step: int
    kind: str
    name: str
    payload: dict[str, Any]


@dataclass
class AgentRunResult:
    response: AgentFinalResponse
    trace: list[TraceEvent] = field(default_factory=list)
    model: str | None = None
    fallback_used: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def as_groq_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


TOOL_SPECS = (
    ToolSpec("lookup_gateway", "Look up the gateway record for a transaction.", {"type": "object", "properties": {"txn_id": {"type": "string"}}, "required": ["txn_id"], "additionalProperties": False}),
    ToolSpec("lookup_bank", "Look up the bank settlement for a transaction.", {"type": "object", "properties": {"txn_id": {"type": "string"}}, "required": ["txn_id"], "additionalProperties": False}),
    ToolSpec("lookup_ledger", "Look up the ledger entry for a transaction.", {"type": "object", "properties": {"txn_id": {"type": "string"}}, "required": ["txn_id"], "additionalProperties": False}),
    ToolSpec("create_ledger_entry", "Create an authorized ledger entry after deterministic diagnosis permits it.", {"type": "object", "properties": {"txn_id": {"type": "string"}, "evidence": {"type": "object"}}, "required": ["txn_id", "evidence"], "additionalProperties": False}),
    ToolSpec("raise_ticket", "Raise a review ticket for an authorized exception.", {"type": "object", "properties": {"txn_id": {"type": "string"}, "reason": {"type": "string"}, "evidence": {"type": "object"}}, "required": ["txn_id", "reason", "evidence"], "additionalProperties": False}),
    ToolSpec("close_as_resolved", "Close a transaction only when deterministic diagnosis authorizes closure.", {"type": "object", "properties": {"txn_id": {"type": "string"}, "evidence": {"type": "object"}}, "required": ["txn_id", "evidence"], "additionalProperties": False}),
    ToolSpec("search_similar_tickets", "Find bounded historical tickets similar to an explanation.", {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["query"], "additionalProperties": False}),
)


def tool_schemas() -> list[dict[str, Any]]:
    return [spec.as_groq_schema() for spec in TOOL_SPECS]


def load_system_prompt() -> str:
    return (Path(__file__).with_name("system_prompt.txt")).read_text(encoding="utf-8")


class GroqOrchestrator:
    def __init__(
        self,
        client: Any,
        handlers: dict[str, Callable[..., Any]],
        model: str = DEFAULT_MODEL,
        fallback_model: str = DEFAULT_FALLBACK_MODEL,
        max_steps: int = MAX_AGENT_STEPS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = 2,
    ) -> None:
        self.client = client
        self.handlers = handlers
        self.model = model
        self.fallback_model = fallback_model
        self.max_steps = max(1, min(max_steps, MAX_AGENT_STEPS))
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, min(max_retries, 3))

    def run(
        self,
        txn_id: str,
        diagnosis: dict[str, Any],
        on_trace: Callable[[TraceEvent], None] | None = None,
    ) -> AgentRunResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": load_system_prompt() + '\n\nFinal answer must be a JSON object with exactly these keys: {"explanation": string (2-4 sentences), "status": string, "action": string}. Set "status" to the diagnosis match_status and "action" to the diagnosis action, verbatim.'},
            {"role": "user", "content": json.dumps({"txn_id": txn_id, "diagnosis": diagnosis}, default=str)},
        ]
        trace: list[TraceEvent] = []
        used_fallback = False
        model = self.model

        def record(event: TraceEvent) -> None:
            trace.append(event)
            if on_trace is not None:
                on_trace(event)

        for step in range(1, self.max_steps + 1):
            try:
                completion = self._complete(model, messages, step, record)
            except Exception as exc:
                if model == self.fallback_model:
                    raise GroqOrchestrationError("Groq unavailable after fallback") from exc
                record(TraceEvent(step, "retry", "groq_completion", {"reason": "primary_model_failed", "from_model": model, "to_model": self.fallback_model}))
                model = self.fallback_model
                used_fallback = True
                continue
            message = completion.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                messages.append({"role": "assistant", "tool_calls": [self._tool_call_dict(call) for call in tool_calls]})
                for call in tool_calls:
                    name = call.function.name
                    args = json.loads(call.function.arguments or "{}")
                    record(TraceEvent(step, "tool_call", name, args))
                    if name not in self.handlers:
                        result = {"error": "tool_not_available"}
                    else:
                        result = self.handlers[name](**args)
                    record(TraceEvent(step, "tool_result", name, result if isinstance(result, dict) else {"value": result}))
                    messages.append({"role": "tool", "tool_call_id": call.id, "name": name, "content": json.dumps(result, default=str)})
                continue
            try:
                parsed = self._extract_json(message.content)
                final = AgentFinalResponse.model_validate(parsed)
            except Exception as exc:
                raise GroqOrchestrationError("Groq returned an invalid final response") from exc
            authoritative_status = str(diagnosis.get("match_status", diagnosis.get("status", final.status)))
            if final.status != authoritative_status or final.action != str(diagnosis.get("action", final.action)):
                record(TraceEvent(step, "diagnosis_divergence", "model_output", {"model_status": final.status, "authoritative_status": authoritative_status, "model_action": final.action, "authoritative_action": diagnosis.get("action")}))
            final.status = authoritative_status
            if diagnosis.get("action"):
                final.action = str(diagnosis["action"])
            return AgentRunResult(final, trace, model, used_fallback)
        raise GroqOrchestrationError("Maximum agent steps exceeded")

    def _complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        step: int,
        on_trace: Callable[[TraceEvent], None],
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                # No response_format=json_object here: several Groq models reject
                # JSON mode combined with tools ("json mode cannot be combined
                # with tool/function calling"). The system prompt requests JSON
                # for the final answer, and _extract_json parses it tolerantly.
                return self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tool_schemas(),
                    tool_choice="auto",
                    timeout=self.timeout_seconds,
                )
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    on_trace(TraceEvent(step, "retry", "groq_completion", {"model": model, "attempt": attempt + 1, "reason": str(exc)[:120]}))
                    time.sleep(min(0.25 * (2**attempt), 1.0))
        raise last_error or GroqOrchestrationError("Groq completion failed")

    @staticmethod
    def _extract_json(content: str | None) -> dict[str, Any]:
        """Parse a final answer that may be wrapped in prose or ``` fences."""
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
            if text.lstrip().lower().startswith("json"):
                text = text.lstrip()[4:]
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
        return json.loads(text)

    @staticmethod
    def _tool_call_dict(call: Any) -> dict[str, Any]:
        return {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}}
