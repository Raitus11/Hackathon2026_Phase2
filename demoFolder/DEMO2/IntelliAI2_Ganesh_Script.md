# IntelliAI 2.0 — Ganesh Walkthrough Script (~7:30 total)

**Budget: ~4:50 on the interactive HTML (11 scenes) + ~2:30 demo + buffer.**
Voice: confident, precise, benefit-led. The AI is the brain of this system — never call it "just advisory." The framing that runs through everything: **the AI decides the what; the engine guarantees the how — and that pairing is what saves the money.** Lines in *(italics)* are stage cues; don't say them.

*(Open the HTML full-screen. Scene 1 is already animating — a live cutover, messages flowing.)*

---

## Scene 1 — Title (~25s)

Part 1 used AI to design the target topology. IntelliAI 2.0 is the system that executes that design — an AI-driven migration control plane that moves live applications onto their own dedicated queue managers, one at a time, with zero message loss.

What's animating behind me is the entire idea in a single picture: messages streaming from an application, and mid-flow, the route cutting over to a new queue manager without one message dropped. And this isn't a simulation — sixteen real IBM MQ queue managers on OpenShift, seven applications migrated, zero lost, and not a single application had to change.

Let me show you how it works — and what it saves.

## Scene 2 — The Problem (~30s)

*(advance — the four cards stage in)*

Designing the target topology took Part 1 thirty seconds. Executing it on a running estate is the genuinely hard part, because four guarantees have to hold true at the same time.

Not one message can be lost — none stranded, none duplicated. Not one application can change — producers and consumers must not even notice. The co-tenant applications still on that shared queue manager must feel nothing. And every change must be recorded, causally ordered, and reversible.

Any one of these is straightforward. Holding all four simultaneously, on live infrastructure — that is exactly what this control plane was built to do.

## Scene 3 — One Chokepoint (~35s)

*(advance — the stack builds, then the ✗ NO PATH marker appears)*

The architecture rests on a single rule: nothing touches MQ or the cluster except the Business Control Layer.

The UI is a pure client — it holds no credentials and issues no commands; it can only call the BCL's API. The BCL is the sole point of contact with MQ and OpenShift, and it's where everything important lives: the deterministic execution engines, the AI core, and the Lamport-clocked audit log. Beneath it, sixteen real queue manager pods.

Notice the marker on the right — there is structurally no path from the UI to MQ. That's by design. One path means one place to validate every change, one place to sequence it, and one place to record it. When an auditor asks what happened, there is exactly one place to look.

## Scene 4 — Watch the Cutover (~55s)

*(advance — the migration auto-runs; narrate over it)*

Here is one application migrating, live, in five steps.

Provision — the application's dedicated queue manager comes up as a pod, and its MQ objects are created. Bridge — a transmission queue and sender channel on the source, a matching receiver on the target. The path now exists.

Now the cutover, and this is the elegant part. Watch the queue label: the application's local queue is redefined as a remote-queue alias with the exact same name, pointing across the bridge. The application keeps putting to the same name, on the same connection — you can see its messages now flowing to the target — and it never reconnects. It never even notices.

Then the drain. With the producer rewired away, nothing new arrives — watch the source depth fall. And we don't declare victory on a single reading: the gate requires depth zero, no input handles, no output handles, held across a consecutive-poll window. There it is — three of three.

Validate the new path end to end, remove the empty source queues, and the application owns a dedicated queue manager. One deliberate choice worth noting: we used an alias rather than clustering the queue managers, because an alias is local and reversible with a single inverse command.

## Scene 5 — The 12-State Machine (~50s)

*(advance — the happy path auto-runs)*

Every migration runs as an explicit twelve-state machine — not a script. When a script fails, it leaves the system wherever it happened to stop, and someone reverse-engineers the wreckage at two in the morning. Here, every application is always in a named state, and every transition writes an audit row.

The engineering detail you'll appreciate: every state change passes through a single assert_transition chokepoint, and the transition rules live in a pure module with no I/O — unit-testable, and deliberately shaped as the executable mirror of a TLA+ next-state relation. We use Lamport's discipline today; running the model checker is on the roadmap, and we say so plainly.

*(click ⚡ INJECT FAILURE)*

Now watch a failure — because the failure path is half this machine, not an afterthought. The target queue manager drops mid-migration. The engine detects it at the transition, moves to ROLLING_BACK, reads this application's audit entries, and walks them in reverse Lamport order, applying the inverse of every completed step. Watch the reverse walk. It settles at ROLLED_BACK — source restored, application fully operational — and every other application, completed or not yet started, is untouched.

## Scene 6 — The AI Core (~50s)

*(advance — the lifecycle strip lights up: PLANS → RISK-GATES → PREDICTS → EXPLAINS → DIAGNOSES — then the reasoning stream starts)*

Now the heart of the system — the AI, running Gemini 2.5 Pro through our Tachyon gateway. And I want to be precise about its role, because this is not a chatbot bolted onto a migration tool. Watch the strip: the AI plans, risk-gates, predicts, explains, and diagnoses — it is woven through the entire lifecycle. The AI decides the *what*; the engine guarantees the *how*.

The Migration Planner and Risk Auditor write the plan the engine executes — the step sequence, the bridge and transmission-queue naming, the ordering rationale, and a risk score, all produced before the approval gate. That is days of a senior MQ engineer's work, generated in seconds.

The Operator Assistant turns operations into a conversation — you can see it computing a live drain estimate from real queue depth. And the RCA Assistant reads a failed migration's audit trail and names the exact MQ reason code — there's 2059, queue manager not available — with the checks to run next.

And the entire intelligence layer sits inside the audit perimeter: every AI call is recorded in its own table, with tool budgets and rate limits. Intelligent and bankable — at the same time.

## Scene 7 — The Mathematics (~20s)

*(advance)*

Briefly, the two models underneath — each used exactly where it applies. Little's Law predicts the drain, and it applies perfectly here because rewiring the producer makes the arrival rate zero by construction; the engine still proves the drain by polling to zero. And the state machine literally is an absorbing Markov chain, so the fundamental matrix gives expected steps and outcome probabilities — shown beside the audit-log measurements, never merged with them.

## Scene 8 — Results (~20s)

*(advance — counters run)*

Run end to end on a real fleet. Sixteen queue managers as real pods on OpenShift. Seven applications migrated, each through its own state machine. Zero messages lost — proven, not assumed. One hundred percent reversible, with the rollback verified live.

## Scene 9 — The Time Dividend (~40s)

*(advance — four compact cards, each with a manual bar stretching in red and an AI bar snapping in gold. Let each card's bars land before you speak to it.)*

You've now seen the architecture, the proofs, and the results. So let me close with what it's all worth — the business case, in two parts. First, the AI on the clock.

Designing the compliant target topology for the whole estate: weeks of a senior architect at a whiteboard. Part 1's AI does it in about thirty seconds — validated, with decision records.

Planning a single migration, sequenced and risk-scored: roughly three days of a senior MQ engineer, done by hand. About forty seconds with Gemini — and the engineer moves from writing the plan to judging it, which is a far better use of that seniority.

Diagnosing a failed migration: two to four hours of war-room triage, manually. The RCA agent names the reason code from the audit trail in seconds.

And the quiet one: risk records, ADRs, evidence — skipped under deadline pressure in every manual program we've ever seen. Here, generated on every single run. And the entire intelligence layer costs cents per migration in tokens. Engineer-days, replaced for pocket change.

## Scene 10 — The Program Dividend (~45s)

*(advance — the 438 counter climbs, then the four pillars stage in)*

Now compound it — because this is what you actually fund.

Across Part 1's real estate — four hundred and thirty-eight applications, designed by Part 1 and executed by Part 2 — the AI is the difference between a multi-year manual program and a scheduled quarter.

And the dividend lands on four fronts. Speed — modernization becomes a scheduled program, not a heroic one. Risk — every change is validated, reversible, and co-tenant-gated, so a failure isn't an outage; it's a rolled-back state with an instant root cause. Cost — design, planning, and diagnosis paid in tokens instead of engineer-years, and the estate itself gets cheaper: fifty-three orphan queue managers of licence and infrastructure waste, eliminated by the target design. And compliance — PCI isolation designed in, a decision record on every choice, an evidence bundle on every change. Audit-ready by construction.

And it compounds forever: Part 1's Day-2 mode keeps the estate optimised as it changes, and Part 2 executes every change with the same proofs. Modernise once — stay modern automatically.

## Scene 11 — Demo Handoff (~10s)

That's the system, and that's what it's worth. Now let's migrate one for real — and then break one on purpose.

*(switch to the dashboard)*

## Demo — compressed to ~2:30

*(Dashboard up; `oc get pods` on a side screen if available. Fallback if anything stalls: "in the interest of time, let me show you the recorded run.")*

**Topology (~15s)**

"Everything you've just seen as animation, you're now going to watch on real MQ. Here's the estate — sixteen queue managers, seven applications on shared sources — and on the side you can see the producer and consumer pods running and pushing live messages."

**Plan and approve (~30s)**

"I'll select the first application and click Plan Migration. Watch what comes back — in a few seconds, Gemini has produced the complete ordered plan: every MQSC operation, the bridge naming, and the risk assessment. This document is what a senior MQ engineer would spend the better part of a week producing, and it's the exact plan the engine will execute. Nothing runs until I approve it — so I approve."

**Execute — the main event (~35s)**

"Now watch the audit log. Every command from the AI's plan is hitting the queue manager pod live — each command and its response recorded in real time, with the Lamport clock incrementing on every row. On the topology graph, the dedicated queue manager has come up and the channels are wiring themselves in. And four functional tests run automatically — connectivity, message-count consistency, a put-get round trip, and the reconnect check. All four green."

**The wow moment (~25s)** — *(slow down here)*

"And here is the point of the entire system. Throughout that migration, the producer never changed. These are the producer pod's environment variables — same DNS name, same port, before and after. The application has no idea it just moved queue managers."

**Break it on purpose (~35s)**

"Now let me break something deliberately — I'll drop the sender channel. Validation fails, the state machine moves to rolling-back, and the engine walks the audit log backward, emitting the reverse commands live. The application settles back on its original queue manager, fully operational. And look at the RCA tab — the AI has already read the trail, named the reason code, and listed the checks to run. In production, that difference — seconds instead of hours — is your incident cost."

**Close (~15s)** — *(as you speak, switch back to the HTML and flip to the Program Dividend scene — the 438-apps screen — so it stays on the wall through Q&A)*

"So: no message lost, no application reconfigured, no co-tenant disturbed, every step on the record — and the intelligence that planned it, scheduled it, and diagnosed it cost cents. AI designed the estate in Part 1; AI planned and explained every move in Part 2; and deterministic engines guaranteed each one. That's how you modernise four hundred applications in a quarter instead of three years. Happy to go as deep as you'd like."

---

## Timing safety valves

- Scene 7 (math) compresses to one sentence: "Little's Law predicts the drain — the arrival rate is zero by construction — and the state machine is literally an absorbing Markov chain; model shown beside measurement, never merged."
- In the demo, if time is tight: drop the large multi-flow migration and the evidence-bundle download; keep plan-approve-execute, the environment-variables moment, and the chaos rollback with the RCA line.
- Never skip Scene 6 (the AI core) or Scenes 9–10 (the two dividend scenes) or the INJECT FAILURE animation — those carry the message he came for. In Scene 9, let each card's bars land before speaking to it; in Scene 10, pause a beat as the 438 counter finishes before delivering the closing line.

## If he asks "is this moving us into NGDC?" — the positioning

"In this build, source and target are both in the same OpenShift cluster — it's a topology migration, shared to dedicated. But the mechanism is location-agnostic: the bridge is standard MQ channels, and channels don't care whether the target queue manager is in the same namespace or in NGDC. An NGDC exit would use exactly this pattern — provision the targets in NGDC, bridge across, rewire, drain, and decommission the legacy source — with the same state machine, the same proofs, and the same evidence trail. Cross-cluster is on the roadmap slide for precisely that reason."
