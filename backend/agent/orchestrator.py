from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from backend.observability import redact, redact_prompt, span, truncate_response


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
        """Word the deterministic diagnosis, traced end to end.

        The span records what the run settled on, and the `groq.chat_completion`
        spans nested under it record what was sent and what came back. Read
        together with the `reconciliation.compare_records` span that precedes
        them, they are the evidence that the model was handed a finished verdict
        and never got a vote on it.
        """
        with span(
            "agent.run",
            **{
                "agent.txn_id": txn_id,
                "agent.max_steps": self.max_steps,
                "groq.model": self.model,
                "groq.fallback_model": self.fallback_model,
                "reconciliation.match_status": diagnosis.get("match_status"),
                "reconciliation.reason_code": diagnosis.get("reason_code"),
            },
        ) as active:
            result = self._run(txn_id, diagnosis, on_trace)
            active.set(
                **{
                    "groq.model_used": result.model,
                    "groq.fallback_used": result.fallback_used,
                    "agent.trace_events": len(result.trace),
                    "agent.diagnosis_divergence": any(event.kind == "diagnosis_divergence" for event in result.trace),
                    "agent.final_status": result.response.status,
                    "agent.final_action": result.response.action,
                }
            )
            return result

    def _run(
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
                    with span(f"agent.tool.{name}", **{"tool.name": name, "tool.step": step, "tool.arguments": args}) as tool_span:
                        if name not in self.handlers:
                            result = {"error": "tool_not_available"}
                        else:
                            result = self.handlers[name](**args)
                        payload = result if isinstance(result, dict) else {"value": result}
                        tool_span.set(**{"tool.result_status": payload.get("status") or ("error" if payload.get("error") else "ok"), "tool.result": redact(payload)})
                    record(TraceEvent(step, "tool_result", name, payload))
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

    def chat(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Answer a conversational message without inventing transaction facts.

        Resolution requests use ``run`` because their status and action are
        deterministic. This path is for the assistant's conversational shell
        (greetings, follow-ups, and questions that do not require a new
        reconciliation) and deliberately has no action tools attached.
        """
        question = message.strip()
        if not question:
            raise GroqOrchestrationError("Chat message is empty")

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are PayPilot, a concise and helpful transaction reconciliation assistant. "
                    "Reply naturally to the user's message; do not repeat a canned greeting or fixed "
                    "template. Never invent transaction facts. If the user asks about a transaction but "
                    "no grounded context or transaction ID is available, ask for the transaction ID. "
                    "Keep the answer in plain natural language, usually 1-4 sentences."
                ),
            }
        ]
        for item in (history or [])[-8:]:
            role = item.get("role")
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content[:2000]})
        if context:
            messages.append({"role": "system", "content": "Grounded ticket context (treat as authoritative): " + json.dumps(context, default=str, sort_keys=True)[:6000]})
        messages.append({"role": "user", "content": question})

        model = self.model
        for _ in range(2):
            try:
                with span(
                    "agent.chat",
                    **{
                        "groq.model": model,
                        "groq.prompt": redact_prompt(messages),
                        "groq.prompt_messages": len(messages),
                    },
                ) as active:
                    completion = self.client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=0.7,
                        max_tokens=500,
                        timeout=self.timeout_seconds,
                    )
                    answer = str(completion.choices[0].message.content or "").strip()
                    if not answer:
                        raise GroqOrchestrationError("Groq returned an empty chat response")
                    active.set(**{"groq.response": truncate_response(answer)})
                    return answer
            except Exception as exc:
                if model == self.fallback_model:
                    raise GroqOrchestrationError("Groq chat unavailable after fallback") from exc
                model = self.fallback_model
        raise GroqOrchestrationError("Groq chat unavailable")

    def _complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        step: int,
        on_trace: Callable[[TraceEvent], None],
    ) -> Any:
        """One Groq call, with retries, captured as a single span.

        Both sides of the exchange land on the span: the prompt as it was sent,
        redacted and truncated, and the response as it came back. Full records do
        travel in the user message — `Diagnosis.detail` embeds them — so
        `redact_prompt` is the boundary that keeps `customer_name` and the bank
        `utr` out of Logfire while leaving the amounts and statuses that make the
        capture worth reading.
        """
        last_error: Exception | None = None
        with span(
            "groq.chat_completion",
            **{
                "groq.model": model,
                "groq.step": step,
                "groq.max_retries": self.max_retries,
                "groq.timeout_seconds": self.timeout_seconds,
                "groq.prompt": redact_prompt(messages),
                "groq.prompt_messages": len(messages),
            },
        ) as active:
            for attempt in range(self.max_retries + 1):
                try:
                    completion = self.client.chat.completions.create(
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
                    continue
                content, tool_call_names = self._completion_summary(completion)
                active.set(
                    **{
                        "groq.attempts": attempt + 1,
                        "groq.response": truncate_response(content),
                        "groq.response_tool_calls": tool_call_names,
                    }
                )
                return completion
            active.set(**{"groq.attempts": self.max_retries + 1, "groq.failure_reason": str(last_error)[:200]})
            raise last_error or GroqOrchestrationError("Groq completion failed")

    @staticmethod
    def _completion_summary(completion: Any) -> tuple[str, list[str]]:
        """What the model said, defensively: never fail a run to describe one."""
        try:
            message = completion.choices[0].message
            names = [call.function.name for call in (getattr(message, "tool_calls", None) or [])]
            return str(message.content or ""), names
        except Exception:
            return "", []

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
