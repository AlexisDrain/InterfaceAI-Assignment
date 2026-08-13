"""CLI entry points.

  discover  -- run the LLM-driven discovery loop against a goal, compile and
               save a capability artifact (draft)
  approve   -- mark an artifact reviewed/approved so it can be replayed
  replay    -- deterministically invoke a capability with typed params
  list      -- catalog of saved capabilities (the agent-facing surface)
  operator  -- send a command to a waiting escalation (headless operator path)
  inject    -- set the demo target app's failure-injection mode
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from .compiler import compile_artifact, load_app_profile, save_artifact
from .escalation import InterventionManager
from .policy import Policy, Redactor
from .schema import Artifact, ParamSpec, ReplayStatus, RiskSpec
from .tracelog import RunLogger

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines from a local .env file (gitignored) into the
    environment. Real env vars take precedence; never logged."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv()
DEFAULT_BASE_URL = os.environ.get("CUA_TARGET_BASE_URL", "http://127.0.0.1:8300")


def _parse_params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--param must be name=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def _load_param_specs(path: str | None, params: dict[str, str],
                      sensitive: list[str]) -> list[ParamSpec]:
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return [ParamSpec.model_validate(x) for x in raw]
    return [
        ParamSpec(name=k, sensitive=(k in (sensitive or [])),
                  description=f"input parameter '{k}'")
        for k in params
    ]


def cmd_discover(args) -> int:
    from .agent import DiscoveryAgent  # deferred: needs anthropic client
    from .surface import BrowserSurface

    policy = Policy.load(ROOT / "policy" / "policy.json")
    params = _parse_params(args.param)
    specs = _load_param_specs(args.param_spec, params, args.sensitive)
    redactor = Redactor(policy)
    for spec in specs:
        if spec.sensitive and spec.name in params:
            redactor.register_value(params[spec.name])

    logger = RunLogger("discover", redactor)
    print(f"[discover] run {logger.run_id}")
    print(f"[discover] evidence -> {logger.dir}")

    manager = InterventionManager(policy, logger)
    surface = BrowserSurface(policy, headed=args.headed,
                             on_human_action=manager.record_human_action)
    try:
        agent = DiscoveryAgent(surface, policy, logger, params, specs,
                               intervention_manager=manager)
        outcome = agent.run(goal=args.goal, entry_url=args.entry_url)
        print(f"[discover] outcome: {outcome.status} ({outcome.steps_used} steps)")
        if outcome.status != "success":
            print(f"[discover] {outcome.summary}")
            logger.save_json("result.json", {
                "status": outcome.status, "summary": outcome.summary,
            })
            return 1

        profile = load_app_profile(ROOT / "profiles" / args.profile) if args.profile else None
        artifact = compile_artifact(
            outcome,
            capability_id=args.id,
            name=args.name or args.id.replace("_", " ").title(),
            description=args.goal,
            goal=args.goal,
            entry_url=args.entry_url,
            params=params,
            param_specs=specs,
            discovery_run_id=logger.run_id,
            app_profile=profile,
            risk=RiskSpec(level=args.risk, stops_before=args.stops_before or None,
                          rationale=args.risk_rationale or ""),
        )
        path = save_artifact(artifact)
        logger.save_json("result.json", {
            "status": "success", "summary": outcome.summary,
            "artifact": str(path), "checkpoint": outcome.checkpoint_text,
        })
        print(f"[discover] artifact saved: {path} (status=draft)")
        print(f"[discover] review it, then: python -m cua.cli approve {path}")
        return 0
    finally:
        surface.close()
        logger.close()


def cmd_approve(args) -> int:
    path = Path(args.artifact)
    artifact = Artifact.model_validate_json(path.read_text(encoding="utf-8"))
    artifact.status = "approved"
    path.write_text(artifact.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    print(f"[approve] {artifact.capability_id} v{artifact.version} -> approved")
    return 0


def cmd_replay(args) -> int:
    from .replay import ReplayEngine
    from .surface import BrowserSurface

    policy = Policy.load(ROOT / "policy" / "policy.json")
    artifact = Artifact.model_validate_json(Path(args.artifact).read_text(encoding="utf-8"))
    if args.base_url:
        artifact.base_url = args.base_url
    params = _parse_params(args.param)

    redactor = Redactor(policy)
    for v in artifact.sensitive_values(params):
        redactor.register_value(v)

    logger = RunLogger("replay", redactor)
    print(f"[replay] run {logger.run_id} -> {artifact.capability_id} v{artifact.version}")
    print(f"[replay] evidence -> {logger.dir}")

    if args.inject:
        _set_inject(artifact.base_url, args.inject)
        print(f"[replay] target app injection mode set: {args.inject}")

    manager = InterventionManager(policy, logger) if args.escalate else None
    surface = BrowserSurface(policy, headed=args.headed,
                             on_human_action=(manager.record_human_action if manager else None))
    try:
        try:
            engine = ReplayEngine(artifact, params, policy, surface, logger,
                                  intervention_manager=manager,
                                  allow_draft=args.allow_draft)
        except ValueError as e:
            print(f"[replay] invalid invocation: {e}")
            return 2
        result = engine.run()
        print(json.dumps(result.model_dump(exclude_none=True), indent=2))
        return 0 if result.status in (ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME) else 2
    finally:
        surface.close()
        logger.close()


def cmd_list(args) -> int:
    rows = []
    for p in sorted((ROOT / "artifacts").glob("*.json")):
        a = Artifact.model_validate_json(p.read_text(encoding="utf-8"))
        rows.append(a)
        ins = ", ".join(f"{i.name}:{i.type}{'!' if i.sensitive else ''}" for i in a.inputs)
        outs = ", ".join(f"{o.name}:{o.type}" for o in a.outputs)
        print(f"{a.capability_id} v{a.version} [{a.status}] risk={a.risk.level}")
        print(f"    {a.description}")
        print(f"    inputs:  {ins or '-'}    (! = sensitive)")
        print(f"    outputs: {outs or '-'}")
        print(f"    file:    {p}")
    if not rows:
        print("(no artifacts yet -- run a discovery)")
    return 0


def cmd_operator(args) -> int:
    cmd: dict = {"op": args.op}
    if args.op == "click":
        cmd["text"] = args.value
    elif args.op == "type":
        cmd["field"], cmd["value"] = args.field, args.value
    data = json.dumps(cmd).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/api/command", data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(resp.read().decode())
    return 0


def _set_inject(base_url: str, mode: str) -> None:
    with urllib.request.urlopen(f"{base_url}/admin/inject?mode={mode}", timeout=10):
        pass


def cmd_inject(args) -> int:
    _set_inject(args.base_url, args.mode)
    print(f"inject mode = {args.mode}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cua", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery run -> artifact")
    d.add_argument("--goal", required=True)
    d.add_argument("--id", required=True, help="capability id, e.g. lookup_member_savings")
    d.add_argument("--name", default="")
    d.add_argument("--entry-url", default=DEFAULT_BASE_URL)
    d.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    d.add_argument("--sensitive", action="append", default=[], metavar="NAME",
                   help="mark a --param as sensitive (redacted, never persisted)")
    d.add_argument("--param-spec", default=None, help="JSON file with full ParamSpec list")
    d.add_argument("--profile", default="legacy-teller.json",
                   help="app profile in profiles/ to merge (detectors, recoveries)")
    d.add_argument("--risk", default="read_only",
                   choices=["read_only", "prepares_change", "makes_change"])
    d.add_argument("--stops-before", default="", dest="stops_before")
    d.add_argument("--risk-rationale", default="", dest="risk_rationale")
    d.add_argument("--headed", action="store_true")
    d.set_defaults(fn=cmd_discover)

    a = sub.add_parser("approve", help="mark a reviewed artifact as approved")
    a.add_argument("artifact")
    a.set_defaults(fn=cmd_approve)

    r = sub.add_parser("replay", help="deterministically invoke a capability")
    r.add_argument("artifact")
    r.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    r.add_argument("--base-url", default="", help="override artifact base_url (tenant instance)")
    r.add_argument("--escalate", action="store_true",
                   help="on unrecoverable failure, hand off to a human instead of failing")
    r.add_argument("--allow-draft", action="store_true")
    r.add_argument("--inject", default="",
                   choices=["", "none", "slow", "session_expiry", "unknown_modal"],
                   help="set demo failure injection before the run")
    r.add_argument("--headed", action="store_true")
    r.set_defaults(fn=cmd_replay)

    l = sub.add_parser("list", help="catalog of saved capabilities")
    l.set_defaults(fn=cmd_list)

    o = sub.add_parser("operator", help="send a command to a waiting escalation")
    o.add_argument("op", choices=["click", "type", "refresh", "resume", "abort"])
    o.add_argument("value", nargs="?", default="")
    o.add_argument("--field", default="", help="target field label (for 'type')")
    o.add_argument("--port", type=int, default=7100)
    o.set_defaults(fn=cmd_operator)

    i = sub.add_parser("inject", help="set target app failure-injection mode")
    i.add_argument("mode", choices=["none", "slow", "session_expiry", "unknown_modal"])
    i.add_argument("--base-url", default=DEFAULT_BASE_URL)
    i.set_defaults(fn=cmd_inject)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
