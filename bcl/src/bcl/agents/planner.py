"""Migration Planner agent + Compliance Narrator.

Two LLM-touched surfaces, both narrow:

  - `plan_migration(...)` — produces a structured MigrationPlan when
    a migration is created (state PLANNED). One LLM call, Pydantic-
    validated. If the LLM is unreachable, slow, or returns
    unparseable output, a deterministic fallback runs in-process
    and produces a valid MigrationPlan anyway.

  - `narrate_completion(...)` — produces a markdown narrative when
    a migration reaches COMPLETED (or ROLLED_BACK). One free-text
    LLM call. Falls back to a templated narrative if the LLM is
    unavailable.

Why a fallback at all: this code path runs against real production
networks (Tachyon on office laptop, Groq from home). The LLM gateway
WILL fail at the wrong moment. The migration engine must produce a
plan and complete its work regardless. AI-accelerated, not AI-
dependent.

Why one agent, not nine: see Anthropic, "Building effective agents"
(Schluntz & Zhang, 2024-12). Start with the simplest agent that does
the work. Multi-agent orchestration adds debugging surface area and
prompt-injection vectors out of proportion to the lift over a single
well-scoped agent. We will add a second agent (OPERATOR_ASSISTANT,
read-only chat) where the requirements differ enough to justify it.

Citations:
  - Anthropic, "Building effective agents" (2024-12).
  - Little, J. D. C. (1961) — surfaced in the plan rationale when
    drain prediction matters.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from bcl.agents.base import run_structured_agent, run_text_agent
from bcl.models.api import FlowSpec
from bcl.models.orm import AgentName

logger = logging.getLogger("bcl.agents.planner")


# ─────────────────────────────────────────────────────────────────────────
# Output schemas — Pydantic-validated. The planner returns these.
# ─────────────────────────────────────────────────────────────────────────


class MigrationRisk(BaseModel):
    """One risk identified by the planner."""

    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    category: Literal[
        "CAPACITY", "DRAIN_TIME", "BRIDGE", "DEPENDENCY",
        "NAMING", "TARGET_READINESS", "OTHER",
    ]
    description: str = Field(min_length=1, max_length=1000)
    mitigation: str = Field(min_length=1, max_length=1000)


class MigrationPlan(BaseModel):
    """The planner's structured output. Persists on Migration.plan."""

    narrative: str = Field(min_length=1, max_length=4000)
    """2-4 sentence summary an operator would read before approving."""

    ordering_rationale: str = Field(min_length=1, max_length=2000)
    """Why this app is being migrated now (utilization, risk, etc.).
    For Phase 2 v1 we accept whatever app the operator selected; the
    planner explains what makes that choice reasonable. A future
    automated-ordering planner would use an MDP / greedy-by-utilization
    heuristic here."""

    predicted_duration_seconds: int = Field(ge=0, le=3600)
    """Coarse estimate. The actual duration is reported in the
    Migration's started_at / completed_at delta."""

    bridge_channel_name: str
    """The SDR/RCVR channel pair name that bridges source -> target.
    Predictable from QM names; surfaced so the operator can confirm
    what they're about to authorise."""

    bridge_xmitq_name: str
    """The XMITQ on the source side that the bridge SDR reads from."""

    queues_to_redirect: list[str] = Field(default_factory=list)
    """Source-side queue names that will be DELETE'd and re-DEFINE'd
    as QREMOTE pointers to the target QM."""

    risks: list[MigrationRisk] = Field(default_factory=list)
    """Risks identified. Empty list is allowed; not all migrations
    are risky."""

    rollback_strategy: str = Field(min_length=1, max_length=2000)
    """High-level rollback approach. The actual rollback engine
    walks MigrationStep.rollback_payload in reverse step_index order;
    this field is the human-readable summary."""


# ─────────────────────────────────────────────────────────────────────────
# Input shape — passed to the agent + to the deterministic fallback
# ─────────────────────────────────────────────────────────────────────────


class PlannerInput(BaseModel):
    """The structured input handed to the planner agent.

    Constructed by the migration engine at PLANNED time from the
    Migration row, the source + target topology specs, and the app's
    flow membership. The planner does NOT reach into the DB; it
    receives everything it needs as a Pydantic object.
    """

    app_id: str
    app_name: str
    neighbourhood: str

    source_qm: str
    target_qm: str
    target_qm_namespace: str
    target_qm_listener_port: int

    bridge_channel_name: str
    bridge_xmitq_name: str
    queues_to_redirect: list[str]
    """Pre-computed by the engine via choreography.app_owns_queues_on_source."""

    target_qm_provisioned: bool
    """Whether the target QM has been provisioned + MQ-realized."""

    source_flow_count: int
    target_flow_count: int

    app_role_summary: str
    """Brief description: 'producer', 'consumer', 'producer + consumer'."""


# ─────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────


async def plan_migration(
    *,
    planner_input: PlannerInput,
    session_factory: async_sessionmaker[AsyncSession],
    correlation_id: str | None = None,
    actor: str | None = None,
) -> tuple[MigrationPlan, dict[str, Any]]:
    """Produce a structured MigrationPlan for one app.

    Returns (plan, audit_metadata). The plan is always a valid
    MigrationPlan — if the LLM is unavailable the deterministic
    fallback supplies it. The audit_metadata dict describes which
    path was taken ('llm' or 'fallback') and is forwarded into the
    Migration row's `plan` JSON column.
    """
    system_prompt = _PLANNER_SYSTEM_PROMPT
    user_prompt = _render_planner_user_prompt(planner_input)

    input_for_audit = planner_input.model_dump()

    parsed, invocation = await run_structured_agent(
        agent_name=AgentName.MIGRATION_PLANNER,
        trigger="POST /migrations (plan)",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        input_for_audit=input_for_audit,
        output_schema=MigrationPlan,
        session_factory=session_factory,
        correlation_id=correlation_id,
        actor=actor,
    )

    if parsed is not None:
        # Even when the LLM succeeded, we re-assert the bridge naming
        # and queues_to_redirect from the structured input. The LLM is
        # advisory on the narrative; the operational fields are not its
        # call to make.
        plan = parsed.model_copy(update={
            "bridge_channel_name": planner_input.bridge_channel_name,
            "bridge_xmitq_name": planner_input.bridge_xmitq_name,
            "queues_to_redirect": planner_input.queues_to_redirect,
        })
        audit_meta = {
            "planner_source": "llm",
            "agent_invocation_id": invocation.id,
            "model": invocation.model,
            "duration_ms": invocation.duration_ms,
        }
        return plan, audit_meta

    # Fallback path
    plan = deterministic_plan(planner_input)
    # Distinguish "stub provider configured" from "real LLM failed".
    # The former is operator intent (offline demo); the latter is a
    # production incident. The audit UI surfaces this distinction.
    from bcl.config import get_settings
    is_stub = get_settings().llm_provider == "stub"
    audit_meta = {
        "planner_source": "stub_fallback" if is_stub else "fallback",
        "agent_invocation_id": invocation.id,
        "model": invocation.model,
        "duration_ms": invocation.duration_ms,
        "fallback_reason": (
            "BCL_LLM_PROVIDER=stub; deterministic plan used by design"
            if is_stub
            else (invocation.error_message or "no LLM output")
        ),
    }
    return plan, audit_meta


def deterministic_plan(p: PlannerInput) -> MigrationPlan:
    """Domain-aware deterministic fallback. Always succeeds.

    Produces a valid MigrationPlan from the structured input alone,
    using only deterministic Python (no LLM). The narrative is templated
    but uses real values, so it reads sensibly to an operator.

    Why a domain-aware fallback (not the generic LLM stub):
        A generic stub returns a constant — useless to operators. This
        function knows the migration domain and produces the same shape
        of output the LLM would produce, just without LLM-grade prose.
    """
    risks: list[MigrationRisk] = []

    if not p.target_qm_provisioned:
        risks.append(MigrationRisk(
            severity="CRITICAL",
            category="TARGET_READINESS",
            description=(
                f"Target QM {p.target_qm} has not been provisioned or "
                "MQ-realized yet."
            ),
            mitigation=(
                f"Run POST /topologies/{{target}}/provision and "
                "POST /topologies/{target}/realize-mq-objects before "
                "starting this migration."
            ),
        ))

    if not p.queues_to_redirect:
        risks.append(MigrationRisk(
            severity="MEDIUM",
            category="DEPENDENCY",
            description=(
                f"App {p.app_id} has no consumer-side queues on the "
                f"source QM {p.source_qm}. Either the app is a pure "
                "producer or the source topology mapping is incomplete."
            ),
            mitigation=(
                "If the app is purely a producer, only the bridge "
                "channel will be created and no QLOCAL->QREMOTE "
                "rewiring will occur. Verify with the topology view."
            ),
        ))

    risks.append(MigrationRisk(
        severity="LOW",
        category="DRAIN_TIME",
        description=(
            "Drain time depends on observed consumer service rate (μ). "
            "Per Little's Law, T_drain ≈ L_0 / μ. The engine measures "
            "μ during the first ~2 seconds of drain polling and surfaces "
            "the prediction in the UI."
        ),
        mitigation=(
            f"Drain timeout is bounded at the BCL default; long drains "
            "fail the migration and trigger automatic rollback. "
            f"{len(p.queues_to_redirect)} queue(s) will be drained."
        ),
    ))

    narrative = (
        f"Migrate app {p.app_id} ({p.app_name}) from source QM "
        f"{p.source_qm} to dedicated target QM {p.target_qm}. The "
        f"choreography establishes a source-to-target bridge "
        f"({p.bridge_xmitq_name} -> {p.bridge_channel_name} -> RCVR on "
        f"{p.target_qm}), then on the source QM replaces "
        f"{len(p.queues_to_redirect)} QLOCAL queue(s) with QREMOTE "
        f"pointers to the target. Producers and consumers do not "
        "reconfigure — connection strings stay bit-identical. Drain is "
        "verified via the zero-window condition (depth=0, IPPROCS=0, "
        "OPPROCS=0 over three consecutive polls) before the source-side "
        "QLOCAL is removed."
    )

    return MigrationPlan(
        narrative=narrative,
        ordering_rationale=(
            f"App {p.app_id} ({p.app_role_summary}, neighbourhood "
            f"'{p.neighbourhood}') was selected by the operator. "
            "Phase 2 v1 does not run automated ordering; the operator "
            "chooses based on the topology view and risk briefing."
        ),
        predicted_duration_seconds=max(60, 30 + 5 * len(p.queues_to_redirect)),
        bridge_channel_name=p.bridge_channel_name,
        bridge_xmitq_name=p.bridge_xmitq_name,
        queues_to_redirect=p.queues_to_redirect,
        risks=risks,
        rollback_strategy=(
            "On any forward-step failure, the rollback engine walks "
            "MigrationStep rows in reverse step_index order. For each "
            "step it executes rollback_payload.mqsc_text on the same "
            "QM that ran the forward command. Per-app locality is a "
            "consequence of this design: the steps cover only this "
            "Migration's app, so reversing them never touches other "
            "apps' state. The bridge SDR/RCVR/XMITQ are torn down "
            "last so any in-flight messages on the XMITQ are not "
            "stranded."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────
# Prompt building
# ─────────────────────────────────────────────────────────────────────────


_PLANNER_SYSTEM_PROMPT = """\
You are the IntelliAI 2.0 Migration Planner. You produce structured
migration plans for one application at a time, moving it from a shared
source IBM MQ queue manager to a dedicated target queue manager.

You must produce VALID JSON conforming to this schema. Do not include
any prose outside the JSON object.

Schema:
{
  "narrative": "2-4 sentences. Concrete: name source QM, target QM, bridge channel, count of queues being redirected.",
  "ordering_rationale": "Why migrate THIS app right now. Cite role, neighbourhood, risk level.",
  "predicted_duration_seconds": <integer 60-1800>,
  "bridge_channel_name": "<the bridge_channel_name from the input>",
  "bridge_xmitq_name": "<the bridge_xmitq_name from the input>",
  "queues_to_redirect": ["<from input verbatim>"],
  "risks": [
    {
      "severity": "CRITICAL"|"HIGH"|"MEDIUM"|"LOW",
      "category": "CAPACITY"|"DRAIN_TIME"|"BRIDGE"|"DEPENDENCY"|"NAMING"|"TARGET_READINESS"|"OTHER",
      "description": "...",
      "mitigation": "..."
    }
  ],
  "rollback_strategy": "Reference Lamport-ordered reverse walk of MigrationStep rollback payloads."
}

Guidance:
  * Be conservative. If you are not sure, prefer LOW severity over HIGH.
  * Cite the bridge XMITQ and channel name in the narrative.
  * The rollback strategy must NOT invent novel steps; describe the
    automatic engine behaviour (reverse-Lamport walk of MigrationStep
    rollback_payload).
  * If queues_to_redirect is empty, surface that as a MEDIUM risk.
  * If target_qm_provisioned is false, surface that as a CRITICAL risk.

Reference: Little's Law (L = λW) governs drain prediction. Cite it in
the narrative or in a DRAIN_TIME risk if relevant.
"""


def _render_planner_user_prompt(p: PlannerInput) -> str:
    return f"""\
Plan a migration for the following application.

app_id: {p.app_id}
app_name: {p.app_name}
neighbourhood: {p.neighbourhood}
app_role: {p.app_role_summary}

source_qm: {p.source_qm}
target_qm: {p.target_qm}
target_qm_namespace: {p.target_qm_namespace}
target_qm_listener_port: {p.target_qm_listener_port}
target_qm_provisioned: {p.target_qm_provisioned}

source_flow_count: {p.source_flow_count}
target_flow_count: {p.target_flow_count}

bridge_channel_name: {p.bridge_channel_name}
bridge_xmitq_name: {p.bridge_xmitq_name}

queues_to_redirect (count={len(p.queues_to_redirect)}):
{chr(10).join(f"  - {q}" for q in p.queues_to_redirect) or "  (none)"}

Produce the migration plan JSON.
"""


# ─────────────────────────────────────────────────────────────────────────
# Compliance Narrator — markdown post-completion narrative
# ─────────────────────────────────────────────────────────────────────────


_NARRATOR_SYSTEM_PROMPT = """\
You are the IntelliAI 2.0 Compliance Narrator. Given the migration
record + recent audit-log entries for one application's migration, you
produce a Markdown narrative of what happened: what was migrated, what
guardrails were enforced, what evidence was captured.

Tone: factual, evidence-cited, no editorialising. Suitable for inclusion
in an evidence bundle that may be reviewed by a SOX-style auditor.

Format:
  - Single Markdown document, ~300-500 words.
  - H2 sections: Summary, Forward Steps, Outcomes, Audit References.
  - Every factual claim about MQSC, state transitions, or timings must
    derive from the provided audit-log excerpt. Do not invent timings
    or AMQ codes that aren't in the input.
"""


async def narrate_completion(
    *,
    migration_summary: dict[str, Any],
    recent_audit_excerpt: list[dict[str, Any]],
    session_factory: async_sessionmaker[AsyncSession],
    correlation_id: str | None = None,
    actor: str | None = None,
) -> str:
    """Produce a Markdown narrative for a completed (or rolled-back)
    migration. Falls back to a templated narrative if the LLM is
    unavailable.
    """
    user_prompt = _render_narrator_user_prompt(
        migration_summary, recent_audit_excerpt
    )
    input_for_audit = {
        "migration_summary": migration_summary,
        "audit_excerpt_size": len(recent_audit_excerpt),
    }

    text, _invocation = await run_text_agent(
        agent_name=AgentName.MIGRATION_PLANNER,  # Reuse enum; narrator is
                                                  # a Planner sub-mode.
        trigger="POST /migrations narrate-completion",
        system_prompt=_NARRATOR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        input_for_audit=input_for_audit,
        session_factory=session_factory,
        correlation_id=correlation_id,
        actor=actor,
    )

    if text:
        return text
    return _deterministic_narrative(migration_summary, recent_audit_excerpt)


def _render_narrator_user_prompt(
    summary: dict[str, Any],
    excerpt: list[dict[str, Any]],
) -> str:
    summary_lines = "\n".join(f"  {k}: {v}" for k, v in summary.items())
    excerpt_lines = []
    for e in excerpt[:60]:  # cap at 60 entries to keep prompt size reasonable
        excerpt_lines.append(
            f"  LC={e.get('lamport_clock')} "
            f"{e.get('operation')} "
            f"success={e.get('success')} "
            f"actor={e.get('actor')}"
        )
    return f"""\
Migration summary:
{summary_lines}

Audit-log excerpt (Lamport-ordered, up to 60 entries):
{chr(10).join(excerpt_lines)}

Produce the compliance narrative in Markdown.
"""


def _deterministic_narrative(
    summary: dict[str, Any],
    excerpt: list[dict[str, Any]],
) -> str:
    """Templated narrative used when the LLM is unavailable.

    Produces a structurally identical markdown document so the
    evidence bundle has a narrative regardless of LLM availability.
    """
    op_counts: dict[str, int] = {}
    for e in excerpt:
        op = e.get("operation") or "UNKNOWN"
        op_counts[op] = op_counts.get(op, 0) + 1

    op_lines = "\n".join(
        f"- `{op}`: {n}" for op, n in sorted(op_counts.items(), key=lambda kv: -kv[1])
    )

    return f"""\
## Summary

Migration of application **{summary.get("app_id")}** from source QM
**{summary.get("source_qm")}** to dedicated target QM
**{summary.get("target_qm")}** entered the audit log starting at
Lamport clock **{summary.get("first_lamport")}** and reached terminal
state **{summary.get("final_state")}** at Lamport clock
**{summary.get("last_lamport")}**.

## Forward Steps

The choreography executed {summary.get("step_count", 0)} ordered steps
spanning source-side QREMOTE/XMITQ/SDR definition, target-side RCVR
definition, drain wait, and source-side QLOCAL teardown. Every step's
forward MQSC text and rollback MQSC text are persisted on the
corresponding MigrationStep row.

## Outcomes

Final migration state: **{summary.get("final_state")}**.
Duration: {summary.get("duration_seconds", "?")} seconds wall-clock.
Drain prediction (Little 1961): L_0={summary.get("drain_l0", "?")},
μ={summary.get("drain_mu", "?")} msg/s, T_drain_predicted=
{summary.get("drain_predicted_seconds", "?")} s.

## Audit References

Audit-log entries by operation type in this migration's
correlation_id scope:

{op_lines}

The full audit log is queryable via `GET /audit?correlation_id=
{summary.get("correlation_id")}`. Every state transition, every MQSC
command, and every agent invocation appears as one row.

> _Narrative generated by deterministic template (LLM unavailable).
> Replace with LLM-generated text by setting `BCL_LLM_PROVIDER=groq` or
> `tachyon` and re-running the migration's completion endpoint._
"""


__all__ = [
    "MigrationPlan",
    "MigrationRisk",
    "PlannerInput",
    "plan_migration",
    "deterministic_plan",
    "narrate_completion",
]
