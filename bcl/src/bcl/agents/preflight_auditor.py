"""Pre-Flight Risk Auditor agent.

Runs exactly once per migration, in the window between the Migration
Planner finishing and the engine parking the migration in
AWAITING_APPROVAL. Its single job: hand the operator at the approval
gate a structured risk brief — the non-obvious hazards worth weighing
before clicking Approve.

Why this agent earns its place (the "why not a script" question)
---------------------------------------------------------------
The deterministic `analysis.blast_radius` module already computes the
*facts*: which QMs are shared, which apps are co-tenants, whether the
source QM is mainframe-fronted. A script can list those. What it
cannot do is reason about the *consequence* of a fact in context — that
a co-tenant sharing a reply-to queue with the migrating app turns a
routine cutover into a cross-app dependency, or that a mainframe
neighbourhood means the rollback window is constrained by a batch
schedule. The auditor reasons over the facts the blast-radius module
produced and the plan the planner produced, and names the hazards a
senior operator would raise in a change-review meeting.

It is advisory only. It has no write tools. It cannot start, stop,
revise, or alter a migration. Its output feeds two things: the brief
the human reads, and the go/no-go decision score
(`analysis.decision.evaluate_gate`), which folds the brief's severities
into an expected-cost comparison.

Like every agent in this system it has a deterministic fallback: when
the LLM gateway is unreachable the brief is still produced, derived
straight from the blast-radius facts. AI-elevated, not AI-dependent.

Citations
---------
Anthropic, "Building effective agents" (Schluntz & Zhang, 2024-12) —
the discipline of one well-scoped agent per distinct job.

US DoD, "Procedures for Performing a Failure Mode, Effects and
Criticality Analysis" (MIL-STD-1629A, 1980) — severity-ranked hazard
enumeration is the FMEA pattern; the brief is a lightweight FMEA over
one migration.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bcl.agents.base import run_structured_agent
from bcl.analysis.blast_radius import BlastRadius, analyse_blast_radius
from bcl.models.orm import AgentName

logger = logging.getLogger("bcl.agents.preflight_auditor")


# ─────────────────────────────────────────────────────────────────────────
# Output schema — Pydantic-validated. The auditor returns this.
# ─────────────────────────────────────────────────────────────────────────


class PreflightFinding(BaseModel):
    """One hazard surfaced by the pre-flight audit."""

    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    category: Literal[
        "CO_TENANCY", "SHARED_QM", "MAINFRAME", "ISOLATION",
        "DRAIN_TIME", "TARGET_READINESS", "DEPENDENCY", "OTHER",
    ]
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(min_length=1, max_length=1200)
    """What the hazard is and *why it matters* for this migration."""
    recommendation: str = Field(min_length=1, max_length=800)
    """What the operator should do or check before approving."""


class PreflightRiskBrief(BaseModel):
    """The auditor's structured output. Persisted on Migration.plan
    under the `risk_brief` key and consumed by the go/no-go score."""

    findings: list[PreflightFinding] = Field(default_factory=list)
    overall_assessment: Literal[
        "CLEAR", "PROCEED_WITH_CARE", "REVIEW_BEFORE_APPROVING"
    ]
    summary: str = Field(min_length=1, max_length=2000)
    """2-4 sentence plain-English brief an operator reads at the gate."""

    def severities(self) -> list[str]:
        """Severity strings for the go/no-go decision score."""
        return [f.severity for f in self.findings]


# ─────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────


_AUDITOR_SYSTEM_PROMPT = """\
You are the IntelliAI 2.0 Pre-Flight Risk Auditor. You run once, after
a migration plan is produced and before a human approves it.

You are given two things:
  1. The deterministic blast-radius analysis for the app being
     migrated — co-tenant apps on its source QM, shared-QM exposure,
     a formal isolation check, mainframe-fronting flags.
  2. The Migration Planner's plan — narrative, queues to redirect,
     predicted duration, the planner's own risks.

Your job: produce a structured risk brief naming the non-obvious
hazards a senior MQ operator would raise in a change-review meeting.
Reason about CONSEQUENCES, not just facts. A shared QM is a fact; the
consequence — that a co-tenant with a reply-to queue turns this into a
cross-app dependency — is what you surface.

You are advisory only. You cannot start, stop, revise or alter a
migration. Do not claim you can.

Rules:
  - Severity is FMEA-style: CRITICAL = could lose messages or strand
    a rollback; HIGH = likely incident if unaddressed; MEDIUM = worth
    a check; LOW = note for the record.
  - If the blast-radius isolation check shows no disturbance and there are no
    co-tenants, it is legitimate to return an empty findings list and
    overall_assessment=CLEAR. Do not invent hazards.
  - overall_assessment: CLEAR (no findings above LOW),
    PROCEED_WITH_CARE (MEDIUM/HIGH present, manageable),
    REVIEW_BEFORE_APPROVING (any CRITICAL).
  - Be specific. Name the QMs, the apps, the queues.
"""


def _render_auditor_user_prompt(
    blast: BlastRadius, plan_dict: dict[str, Any]
) -> str:
    cotenant_lines = "\n".join(
        f"  - {c.app_id} (shares QM {c.shared_qm})"
        for c in blast.cotenants
    ) or "  (none — single-tenant source QM)"

    shared_lines = "\n".join(
        f"  - QM {e.qm} shared with {len(e.cotenant_apps)} other app(s)"
        for e in blast.shared_qm_exposure
    ) or "  (none)"

    planner_risks = plan_dict.get("risks", []) or []
    planner_risk_lines = "\n".join(
        f"  - [{r.get('severity')}] {r.get('category')}: "
        f"{r.get('description', '')[:200]}"
        for r in planner_risks
    ) or "  (planner flagged no risks)"

    return f"""\
Audit the following migration before the operator approves it.

── APP ──
app_id: {blast.app_id}
source_qm: {blast.source_qm}
target_qm: {blast.target_qm}
mainframe_fronted: {blast.is_mainframe_fronted}
neighbourhoods: {", ".join(blast.neighbourhoods) or "(none)"}

── BLAST RADIUS (deterministic) ──
isolation_disturbed: {blast.isolation.disturbed}
disturbance_count: {blast.isolation.commands_touching_cotenant_exclusive}

co-tenant apps on the source QM:
{cotenant_lines}

shared-QM exposure:
{shared_lines}

blast-radius summary: {blast.summary}

── MIGRATION PLAN ──
predicted_duration_seconds: {plan_dict.get("predicted_duration_seconds")}
queues_to_redirect: {len(plan_dict.get("queues_to_redirect", []))}
planner narrative: {plan_dict.get("narrative", "")[:600]}

planner-flagged risks:
{planner_risk_lines}

Produce the risk brief JSON.
"""


# ─────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────


async def audit_migration(
    *,
    app_id: str,
    source_topology_spec: dict[str, Any],
    target_topology_spec: dict[str, Any],
    plan_dict: dict[str, Any],
    target_qm_namespace: str,
    target_qm_listener_port: int,
    session_factory: async_sessionmaker[AsyncSession],
    correlation_id: str | None = None,
    actor: str | None = None,
) -> tuple[PreflightRiskBrief, dict[str, Any]]:
    """Produce a pre-flight risk brief for one migration.

    Returns (brief, audit_metadata). The brief is always valid — if the
    LLM is unreachable the deterministic fallback supplies it from the
    blast-radius facts alone. audit_metadata records which path ran.
    """
    # Step 1 — deterministic blast-radius facts. Always runs.
    blast = analyse_blast_radius(
        app_id=app_id,
        source_topology_spec=source_topology_spec,
        target_topology_spec=target_topology_spec,
        target_qm_namespace=target_qm_namespace,
        target_qm_listener_port=target_qm_listener_port,
    )

    # Step 2 — LLM elevation: reason about consequences.
    parsed, invocation = await run_structured_agent(
        agent_name=AgentName.PREFLIGHT_AUDITOR,
        trigger="migration plan ready (pre-approval-gate)",
        system_prompt=_AUDITOR_SYSTEM_PROMPT,
        user_prompt=_render_auditor_user_prompt(blast, plan_dict),
        input_for_audit={
            "app_id": app_id,
            "source_qm": blast.source_qm,
            "target_qm": blast.target_qm,
            "cotenant_count": len(blast.cotenants),
            "isolation_disturbed": blast.isolation.disturbed,
        },
        output_schema=PreflightRiskBrief,
        session_factory=session_factory,
        correlation_id=correlation_id,
        actor=actor,
    )

    if parsed is not None:
        return parsed, {
            "auditor_source": "llm",
            "agent_invocation_id": invocation.id,
            "model": invocation.model,
            "duration_ms": invocation.duration_ms,
        }

    # Step 3 — deterministic fallback from blast-radius facts.
    brief = _deterministic_brief(blast, plan_dict)
    from bcl.config import get_settings
    is_stub = get_settings().llm_provider == "stub"
    return brief, {
        "auditor_source": "stub_fallback" if is_stub else "fallback",
        "agent_invocation_id": invocation.id,
        "model": invocation.model,
        "duration_ms": invocation.duration_ms,
        "fallback_reason": (
            "BCL_LLM_PROVIDER=stub; deterministic brief by design"
            if is_stub
            else (invocation.error_message or "no LLM output")
        ),
    }


def _deterministic_brief(
    blast: BlastRadius, plan_dict: dict[str, Any]
) -> PreflightRiskBrief:
    """Deterministic fallback. Always succeeds. Derives the brief from
    the blast-radius facts without an LLM — coarser prose, same shape.
    """
    findings: list[PreflightFinding] = []

    if blast.isolation.disturbed:
        findings.append(PreflightFinding(
            severity="CRITICAL",
            category="ISOLATION",
            title="Migration would disturb a co-tenant's exclusive queue",
            detail=(
                f"The isolation check for {blast.app_id} found "
                f"{blast.isolation.commands_touching_cotenant_exclusive} "
                "MQSC command(s) targeting queues owned exclusively by a "
                "co-tenant: "
                f"{', '.join(blast.isolation.cotenant_exclusive_queues_in_blast_radius) or '(unnamed)'}. "
                "Per-queue isolation requires this count to be zero."
            ),
            recommendation=(
                "Do not approve. Re-inspect the source/target topology "
                "mapping — the rewire plan is touching queues it should "
                "not."
            ),
        ))

    if blast.cotenants:
        names = ", ".join(c.app_id for c in blast.cotenants)
        findings.append(PreflightFinding(
            severity="HIGH" if len(blast.cotenants) > 1 else "MEDIUM",
            category="CO_TENANCY",
            title=f"{len(blast.cotenants)} co-tenant app(s) on the source QM",
            detail=(
                f"App {blast.app_id} shares its source QM "
                f"{blast.source_qm} with: {names}. Rewiring this app's "
                "queues touches a QM other apps still depend on; a "
                "mistake here has a blast radius beyond this migration."
            ),
            recommendation=(
                "Confirm none of the co-tenant apps share a reply-to "
                "queue with this app. Migrate single-tenant apps first."
            ),
        ))

    if blast.is_mainframe_fronted:
        findings.append(PreflightFinding(
            severity="MEDIUM",
            category="MAINFRAME",
            title="Source QM is mainframe-fronted",
            detail=(
                f"The source QM {blast.source_qm} carries a Mainframe "
                "neighbourhood tag. The rollback window may be "
                "constrained by an upstream batch schedule."
            ),
            recommendation=(
                "Confirm the cutover does not overlap a mainframe "
                "batch window before approving."
            ),
        ))

    if not plan_dict.get("queues_to_redirect"):
        findings.append(PreflightFinding(
            severity="MEDIUM",
            category="DEPENDENCY",
            title="Plan redirects no source-side queues",
            detail=(
                "The plan lists zero queues to redirect. Either the "
                "app is a pure producer (expected) or the topology "
                "mapping is incomplete (a problem)."
            ),
            recommendation=(
                "Confirm the app's role. If it consumes messages, the "
                "empty redirect list is a mapping bug — do not approve."
            ),
        ))

    top = max(
        (f.severity for f in findings),
        key=lambda s: ["LOW", "MEDIUM", "HIGH", "CRITICAL"].index(s),
        default="LOW",
    )
    if top == "CRITICAL":
        assessment = "REVIEW_BEFORE_APPROVING"
    elif top in ("HIGH", "MEDIUM"):
        assessment = "PROCEED_WITH_CARE"
    else:
        assessment = "CLEAR"

    summary = (
        f"Pre-flight audit of {blast.app_id}: {len(findings)} finding(s), "
        f"highest severity {top}. {blast.summary} "
        "(Deterministic brief — LLM elevation unavailable.)"
    )

    return PreflightRiskBrief(
        findings=findings,
        overall_assessment=assessment,
        summary=summary,
    )


__all__ = [
    "PreflightFinding",
    "PreflightRiskBrief",
    "audit_migration",
]