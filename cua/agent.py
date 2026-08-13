"""Discovery: an LLM-driven observe -> decide -> act loop against a live surface.

The model is used exactly once per capability -- to figure the flow out. What it
produces here is a *trace* (every action + how the element was found + what the
page looked like), which the compiler then turns into a deterministic artifact.
Production replays never call the model.

Safety properties of this loop:
* The model proposes actions; the Surface enforces policy (allowlist, risky
  controls). A hostile page that prompt-injects the model still cannot push an
  action past the surface.
* Sensitive parameter values are NEVER placed in the model's context. The model
  is told to type "{{param_name}}" literally; the surface substitutes the real
  value at fill time. Traces and logs only ever contain the placeholder.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from .policy import Policy, PolicyViolation
from .schema import ElementTarget, LocatorStrategy, ParamSpec, utc_now
from .surface import BrowserSurface, ResolveError
from .tracelog import RunLogger

DEFAULT_MODEL = os.environ.get("CUA_MODEL", "claude-opus-5")

_TARGET_PROPS = {
    "role": {"type": "string", "description": "ARIA role, e.g. button, textbox, link, combobox"},
    "name": {"type": "string", "description": "Accessible name shown in the tree, e.g. 'Search'"},
    "label": {"type": "string", "description": "Associated label text, e.g. 'Member Number'"},
    "text": {"type": "string", "description": "Visible text of the element (fallback)"},
    "css": {"type": "string", "description": "CSS selector (last resort only)"},
}

TOOLS = [
    {
        "name": "navigate",
        "description": "Navigate the browser to a URL. Only allowlisted origins are permitted.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": "Click an element. Prefer role+name from the accessibility tree; "
                       "use label/text as fallbacks and css only as a last resort.",
        "input_schema": {
            "type": "object",
            "properties": {**_TARGET_PROPS, "why": {"type": "string", "description": "One short sentence: what this click accomplishes"}},
        },
    },
    {
        "name": "type",
        "description": "Fill a text input. To enter the value of a declared input parameter, "
                       "type the placeholder {{param_name}} EXACTLY -- the executor substitutes "
                       "the real value. Never guess sensitive values.",
        "input_schema": {
            "type": "object",
            "properties": {
                **_TARGET_PROPS,
                "value": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["value"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option (by visible label) in a dropdown/select control.",
        "input_schema": {
            "type": "object",
            "properties": {**_TARGET_PROPS, "option": {"type": "string"}, "why": {"type": "string"}},
            "required": ["option"],
        },
    },
    {
        "name": "extract",
        "description": "Read a value off the page and declare it as a typed output of this "
                       "capability. For table data prefer row_text+column_header addressing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_name": {"type": "string", "description": "snake_case output name, e.g. savings_balance"},
                "description": {"type": "string"},
                "row_text": {"type": "string", "description": "Text identifying the table row, e.g. 'Savings'"},
                "column_header": {"type": "string", "description": "Header of the column to read, e.g. 'Current Balance'"},
                **_TARGET_PROPS,
            },
            "required": ["output_name"],
        },
    },
    {
        "name": "done",
        "description": "Declare the goal accomplished. Provide checkpoint_text: a distinctive "
                       "piece of text visible on the CURRENT page that proves the goal state "
                       "was reached (used to verify every future replay). It must be stable "
                       "across invocations: never include a parameter value, a name, a date, "
                       "or any per-record data -- prefer fixed page headings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "checkpoint_text": {"type": "string"},
            },
            "required": ["summary", "checkpoint_text"],
        },
    },
    {
        "name": "give_up",
        "description": "Declare that you are stuck and cannot safely proceed. A human operator "
                       "will be asked to intervene.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]

SYSTEM_PROMPT = """You are the discovery component of a computer-use automation system for
bank/credit-union back-office applications. You operate a real browser through tools to
accomplish ONE task, and your successful run will be compiled into a deterministic, replayable
automation -- so work cleanly and predictably.

Rules:
- After every action you receive a fresh accessibility-tree observation. Target elements by
  role + accessible name whenever possible; label or visible text otherwise; CSS only as a
  last resort.
- When a form field should receive the value of a declared input parameter, type the
  placeholder {{param_name}} exactly. The executor substitutes the real value. You may see
  the substituted value reflected back on subsequent pages; that is expected.
- Stay strictly on task. Do not explore unrelated pages. Do not click controls that would
  make irreversible changes (anything matching: confirm/open account, transfer, wire, delete,
  close account, submit payment) -- the policy layer will refuse them anyway.
- If the goal asks you to READ data, use the extract tool to declare it as a named output.
- When the goal state is reached, call done with a checkpoint_text that uniquely identifies
  the goal page. If you are stuck (unexpected state, blocked, cannot find a control), call
  give_up rather than improvising something risky.
- One tool call per turn."""


@dataclass
class TraceStep:
    action: str
    llm_target: dict[str, Any]
    value: Optional[str] = None
    option: Optional[str] = None
    url: Optional[str] = None
    why: str = ""
    strategy_used: Optional[dict] = None
    descriptor: Optional[dict] = None
    url_before: str = ""
    url_after: str = ""


@dataclass
class DiscoveryOutcome:
    status: str                       # "success" | "gave_up" | "max_steps" | "error"
    summary: str = ""
    checkpoint_text: str = ""
    trace: list[TraceStep] = field(default_factory=list)
    extracts: list[dict] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    steps_used: int = 0


def _target_from_args(args: dict) -> ElementTarget:
    strategies: list[LocatorStrategy] = []
    if args.get("role"):
        strategies.append(LocatorStrategy(kind="role", role=args["role"], name=args.get("name")))
    if args.get("label"):
        strategies.append(LocatorStrategy(kind="label", label=args["label"]))
    if args.get("text"):
        strategies.append(LocatorStrategy(kind="text", text=args["text"]))
    if args.get("css"):
        strategies.append(LocatorStrategy(kind="css", selector=args["css"]))
    if args.get("row_text") and args.get("column_header"):
        strategies.append(
            LocatorStrategy(kind="row_cell", row_text=args["row_text"], column_header=args["column_header"])
        )
    if not strategies:
        raise ValueError("no element-targeting fields supplied")
    desc = args.get("why") or args.get("name") or args.get("label") or args.get("text") or ""
    return ElementTarget(description=desc, strategies=strategies)


class DiscoveryAgent:
    def __init__(
        self,
        surface: BrowserSurface,
        policy: Policy,
        logger: RunLogger,
        params: dict[str, str],
        param_specs: list[ParamSpec],
        intervention_manager=None,
        model: str = DEFAULT_MODEL,
    ):
        self.surface = surface
        self.policy = policy
        self.log = logger
        self.params = params
        self.param_specs = param_specs
        self.interventions = intervention_manager
        self.model = model
        self.client = anthropic.Anthropic()

    # -- substitute {{param}} placeholders with real values at fill time ---
    def _bind(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            key = m.group(1)
            if key not in self.params:
                raise ValueError(f"unknown parameter placeholder {{{{{key}}}}}")
            return self.params[key]

        return re.sub(r"\{\{(\w+)\}\}", repl, text)

    def _params_for_prompt(self) -> str:
        lines = []
        for spec in self.param_specs:
            shown = "(value hidden -- sensitive)" if spec.sensitive else repr(self.params.get(spec.name, ""))
            lines.append(f"- {spec.name} ({spec.type}{', sensitive' if spec.sensitive else ''}): "
                         f"{spec.description or 'input parameter'} = {shown}")
        return "\n".join(lines) or "(none)"

    def run(self, goal: str, entry_url: str) -> DiscoveryOutcome:
        out = DiscoveryOutcome(status="error", model=self.model)
        self.surface.navigate(entry_url)
        obs = self.surface.observe()
        self.log.event("discovery_start", goal=goal, entry_url=entry_url, model=self.model)

        messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    f"GOAL: {goal}\n\nINPUT PARAMETERS (use {{{{name}}}} placeholders when typing):\n"
                    f"{self._params_for_prompt()}\n\nInitial page state:\n{obs.for_llm()}"
                ),
            }
        ]

        for step_no in range(1, self.policy.max_discovery_steps + 1):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                # one action per turn: observe -> decide -> act
                tool_choice={"type": "any", "disable_parallel_tool_use": True},
                messages=messages,
            )
            if response.stop_reason == "refusal":
                self.log.event("model_refusal", detail=str(response.stop_details))
                out.status = "error"
                out.summary = "model refused the request"
                return out

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            tool_use = tool_uses[0] if tool_uses else None
            # every tool_use after the first must still get a tool_result on the
            # next message or the API rejects the conversation
            self._extra_tool_uses = tool_uses[1:]
            thoughts = " ".join(b.text for b in response.content if b.type == "text").strip()
            if thoughts:
                self.log.event("model_note", text=thoughts[:500])
            if tool_use is None:
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": "Continue: use exactly one tool."})
                continue

            args = dict(tool_use.input)
            self.log.event("agent_action", step=step_no, tool=tool_use.name, args=args)
            messages.append({"role": "assistant", "content": response.content})

            if tool_use.name == "done":
                out.status = "success"
                out.summary = args.get("summary", "")
                out.checkpoint_text = args.get("checkpoint_text", "")
                out.steps_used = step_no
                self.log.event("discovery_done", summary=out.summary, checkpoint=out.checkpoint_text)
                return out

            if tool_use.name == "give_up":
                resolution = self._escalate_stuck(args.get("reason", ""), goal)
                if resolution == "resumed":
                    obs = self.surface.observe()
                    messages.append(self._tool_result(
                        tool_use.id,
                        "A human operator intervened on the live session and handed control "
                        f"back. Re-assess and continue.\n\n{obs.for_llm()}",
                    ))
                    continue
                out.status = "gave_up"
                out.summary = args.get("reason", "")
                out.steps_used = step_no
                return out

            # ---- browser actions ----------------------------------------
            result_text, err = self._execute(tool_use.name, args, out)
            obs = self.surface.observe()
            body = f"{result_text}\n\nCurrent page state:\n{obs.for_llm()}"
            messages.append(self._tool_result(tool_use.id, body, is_error=err))

        out.status = "max_steps"
        out.summary = f"stopped after {self.policy.max_discovery_steps} steps without reaching the goal"
        out.steps_used = self.policy.max_discovery_steps
        return out

    def _tool_result(self, tool_use_id: str, text: str, is_error: bool = False) -> dict:
        blocks = [{
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": text,
            "is_error": is_error,
        }]
        # answer any parallel tool calls we chose not to execute
        for extra in getattr(self, "_extra_tool_uses", []) or []:
            blocks.append({
                "type": "tool_result",
                "tool_use_id": extra.id,
                "content": "Not executed: one action per turn. Re-issue this call "
                           "on a later turn if it is still needed.",
                "is_error": True,
            })
        self._extra_tool_uses = []
        return {
            "role": "user",
            "content": blocks,
        }

    def _execute(self, tool: str, args: dict, out: DiscoveryOutcome) -> tuple[str, bool]:
        url_before = self.surface.page.url
        try:
            if tool == "navigate":
                self.surface.navigate(args["url"])
                out.trace.append(TraceStep(action="navigate", llm_target={}, url=args["url"],
                                           url_before=url_before, url_after=self.surface.page.url))
                return "Navigated.", False

            if tool == "click":
                target = _target_from_args(args)
                strat, desc = self.surface.click(target)
                self.surface.wait_load()
                out.trace.append(TraceStep(
                    action="click", llm_target=args, why=args.get("why", ""),
                    strategy_used=strat.model_dump(exclude_none=True),
                    descriptor=desc.__dict__, url_before=url_before, url_after=self.surface.page.url,
                ))
                return "Clicked.", False

            if tool == "type":
                target = _target_from_args(args)
                bound = self._bind(args["value"])
                strat, desc = self.surface.type(target, bound)
                out.trace.append(TraceStep(
                    action="type", llm_target=args, value=args["value"],  # template, not bound value
                    why=args.get("why", ""), strategy_used=strat.model_dump(exclude_none=True),
                    descriptor=desc.__dict__, url_before=url_before, url_after=self.surface.page.url,
                ))
                return "Typed.", False

            if tool == "select":
                target = _target_from_args(args)
                strat, desc = self.surface.select(target, args["option"])
                out.trace.append(TraceStep(
                    action="select", llm_target=args, option=args["option"],
                    why=args.get("why", ""), strategy_used=strat.model_dump(exclude_none=True),
                    descriptor=desc.__dict__, url_before=url_before, url_after=self.surface.page.url,
                ))
                return "Selected.", False

            if tool == "extract":
                target = _target_from_args(args)
                value = self.surface.extract_text(target)
                out.extracts.append({
                    "output_name": args["output_name"],
                    "description": args.get("description", ""),
                    "target": target.model_dump(exclude_none=True),
                    "observed_value": value,
                })
                self.log.event("extract", output=args["output_name"], value=value)
                return f"Extracted {args['output_name']} = {value!r}", False

            return f"Unknown tool '{tool}'.", True

        except PolicyViolation as e:
            self.log.event("policy_violation", tool=tool, detail=str(e))
            return f"POLICY REFUSED this action: {e}", True
        except (ResolveError, Exception) as e:  # noqa: BLE001 - reported to the model
            self.log.event("action_error", tool=tool, detail=str(e)[:300])
            return f"Action failed: {str(e)[:300]}", True

    def _escalate_stuck(self, reason: str, goal: str) -> str:
        self.log.event("agent_stuck", reason=reason)
        if not self.interventions:
            return "none"
        report = self.interventions.request(
            reason=f"Discovery agent is stuck: {reason}",
            context={"goal": goal, "phase": "discovery"},
            surface=self.surface,
        )
        return report.resolution
