# Ganesh Q&A Prep — 38 Hard Questions, Veteran-CIO Grade

Written assuming a 30-year technical CIO who is a heavy AI practitioner: he knows LangGraph, LangChain, ADKs, system/user prompts, guardrails, and he has lived through bank model-governance. Answers are written the way you'd *say* them. **The bold first sentence is the answer** — the rest is depth if he pulls the thread. Never answer longer than he asked.

---

## A. Orchestration & agent architecture

**1. Why LangGraph and not plain function chaining, or CrewAI / AutoGen / LangChain agents?**
**Because we needed three things those don't give cleanly: a typed shared state, conditional routing, and a durable pause.** A single `IntelliAIState` TypedDict flows through every node, so every hand-off is a contract, not a string. The Tester→Architect retry is `add_conditional_edges`, and the human gate is a genuine halt with checkpointed state. Conversational frameworks like CrewAI add autonomy exactly where we want determinism — our agents are specialists on rails, not chatty collaborators.

**2. How does the human-in-the-loop pause actually work — is it LangGraph's interrupt?**
**The graph runs to the review node and terminates there; the full state is persisted in the session store, and the human decision is injected back through the review API.** Approve invokes the phase-2 agents directly on the stored state; Revise re-invokes a *second compiled graph* whose entry point is the Architect. Functionally equivalent to interrupt-plus-checkpointer, but we own the persistence — which we needed anyway for the review panel and the Ask-the-Architect chat.

**3. Why two compiled graphs for Revise instead of a loop edge back into one graph?**
**Because a revision must never re-run ingestion and analysis — the input hasn't changed — and a separate graph makes that guarantee structural, not a runtime flag.** The Architect receives the existing target graph plus the parsed feedback directives and applies targeted deltas. It's also cleaner state semantics and cheaper: revision costs one design pass, not a full pipeline.

**4. How do the agents communicate — messages or state?**
**Pure shared state; there is no agent-to-agent chatter.** Each node reads the keys it needs and writes its own section: Researcher writes the graph and analytics, Analyst writes metrics, Architect writes the target graph and ADRs. Every hand-off is inspectable and each agent is testable in isolation — that's why the Trace tab can show you everything.

**5. Is this really "10 agents", or 10 functions with a fashionable name?**
**Each node is a specialist with its own contract, tools, and — where AI-backed — its own scoped system prompt, temperature and token budget; the word "agent" earns its keep where the model genuinely decides among alternatives.** The Cluster Architect weighing bridge-app placement, the Feedback Interpreter converting human English into directives, the review chat defending decisions — those are agents in any honest definition. What we deliberately avoided is open-ended autonomy: in regulated infrastructure an agent that can "do anything" is a liability, not a feature.

**6. Do the agents have memory? Does the system learn across runs?**
**Deliberately stateless per run — and in a bank, run-to-run isolation is a feature, not a gap.** No contamination between estates, and every run is fully explainable from its own inputs. Cross-run learning belongs offline: the ADR and acceptance logs are exactly the labelled data you'd mine to improve prompts through a controlled release — not silent online adaptation that no auditor can reconstruct.

**7. Why not a tool-using agent that queries the graph itself — MCP-style, iterative tool calls?**
**Iterative tool-calling means many round trips, unbounded token spend, and a larger failure surface — for answers our one-shot summarised prompt already gets.** That pattern earns its cost when the data can't be summarised; ours can, by construction. Where it *would* fit is Day-2 lifecycle ops — single-app changes against a live estate — and the Ask-the-Architect chat is already a controlled step in that direction.

**8. (If he's seen Part 2) Part 1 uses LangGraph; Part 2 built its own 12-state machine. Why the inconsistency?**
**It's not inconsistency — it's matching the orchestrator to what's being orchestrated.** Part 1 orchestrates *reasoning*: LLM nodes, shared state, conditional routing — LangGraph's exact sweet spot. Part 2 orchestrates *execution* of live MQ migrations, where you want a hand-rolled, formally specified state machine with per-app rollback in reverse order — you do not want an LLM framework anywhere in the execution hot path. In Part 2 the AI agents sit *beside* the state machine as advisors, never inside it. Same philosophy both times: AI reasons, deterministic machinery executes.

---

## B. LLM usage & prompt engineering

**9. Which model, and why? What's your portability story?**
**Gemini through the Tachyon internal gateway in production, with a dev fallback to Llama 3.3 70B on Groq — and that dual stack is itself the portability proof.** The client is a thin wrapper: system prompt, user prompt, `json_object` mode, temperature 0.1, timeout, retries. Nothing is Gemini-specific; we validated the same prompts on two very different model families, and swapping models is configuration.

**10. Walk me through the prompt design.**
**Twelve role-scoped prompt pairs, each narrow, each with a fixed job.** The cluster prompt literally tells the model: "the engine has already done the assignments — your job is NOT to redo them," then gives it four bounded tasks: cluster review, bridge-app decisions, ADRs citing real entity names, and modernization insights. The immutable contract lives in the system prompt; the user turn carries only summarised topology facts; output is a fixed JSON schema. No open-ended "do anything" agent exists in the system.

**11. Temperature 0.1 — why not zero? Is output reproducible?**
**0.1 keeps design calls near-deterministic while avoiding the degenerate repetition you get at exactly zero on long structured outputs.** But reproducibility comes from the architecture, not the sampler: whatever the model returns is machine-verified, so two runs may differ in which *valid* improvements get applied — never in compliance. The invariants are identical every run.

**12. Why JSON mode rather than native function calling / structured outputs?**
**Deliberate portability — `json_object` plus our own schema validation behaves identically across Gemini via Tachyon and Llama via Groq, while function-calling APIs differ per provider.** Our output-side validation gives us everything function calling would, plus a stronger guarantee it can't: the rules re-check. If we standardised on one provider, native structured outputs would be a drop-in tightening — the architecture doesn't change.

**13. How do you evaluate LLM output quality — where are your evals?**
**The Tester is a built-in eval: eight hard constraints, zero-critical to pass, executed on every design including every AI contribution — plus phase-C scoring of every individual proposal as applied or rejected, with reasons.** That gives us an acceptance-rate metric per run. Honest gap for a hackathon: no offline golden-set benchmark yet. The ADR history is generating exactly the labelled data to build one — that's a named roadmap item, not an afterthought.

**14. Tachyon upgrades Gemini underneath you. What breaks silently?**
**Nothing breaks *silently* — that's the point of running schema validation plus the eight-constraint Tester on every single output.** A model regression manifests as rejected proposals or failed validation, both logged, and the pipeline degrades to the deterministic path rather than shipping a worse design. The rigorous next step is a golden-set regression suite triggered on model-version change — and the ADR log gives us the test cases for free.

**15. Why not fine-tune a model on MQ topology?**
**Wrong tool for this shape of problem.** The correctness-critical logic is symbolic and already perfect in the engine — fine-tuning would spend money approximating what we get exactly, for free. The model's value here is general reasoning over compliance semantics and human language, which frontier models already do well. And fine-tuning freezes you to one model and buys you a model-governance burden that a versioned prompt simply doesn't carry.

**16. Why not RAG over MQ documentation and standards?**
**There's nothing to retrieve — everything the model needs is either in the input data, summarised into the prompt, or in enterprise constraints, encoded in the engine.** Our grounding is actually *stricter* than RAG: the model may only cite entities present in the payload. The moment we extend to org-specific standards documents, RAG becomes the right addition — that's a natural Day-2 layer, not a redesign.

---

## C. Scale, data & the context window — *he will ask this*

**17. You summarise 13K rows to 3K tokens. What happens at 130K rows? A million?**
**The summary is scale-invariant by construction — it's aggregates, not samples, so prompt size grows with the number of clusters and decisions, not the number of rows.** Ten times the rows is still roughly the same count of communities, bridge apps, and violation categories; the prompt grows sub-linearly and is hard-capped with truncation rules, so it physically cannot blow the window. What does grow is the analytics side: betweenness centrality is roughly O(V·E), so at a million edges we switch to sampled approximation — a standard, citable technique. And if a single design pass ever got too large, the Louvain communities give us a natural map-reduce for free: design per community in parallel, then reconcile the bridge apps. The architecture already contains its own scaling strategy.

**18. Gemini has a million-token context. Why not just paste the raw data in?**
**Because stuff-the-window loses to summarise-then-reason on every axis that matters here: cost, latency, accuracy, and attack surface.** A million-token call costs orders of magnitude more per design pass and is dramatically slower. Accuracy degrades — needle-in-a-haystack retrieval over raw rows is a documented weakness of long context, and we need the model reasoning about *structure*, not hunting through rows. And every raw row is potential injection surface; a computed summary is not. Long context is a wonderful capability — this just isn't the problem it's for.

**19. How do you know the summary doesn't drop something the model needed?**
**Because the summary isn't a lossy re-reading of the data — it's generated from the same computed structures the engine itself acts on.** The violations, communities, centrality scores, and business metadata in the prompt are the outputs of the deterministic analysis, so the model sees exactly the feature set the decisions require. And the safety net is layered: anything the model gets wrong from a summarisation gap still has to pass validation, and anything subtle lands in front of the human with ADRs attached.

**20. Is 30 seconds real? What dominates runtime, and what's the SLA story at scale?**
**Yes — the LLM calls dominate; everything else is sub-second.** Rules on 438 apps run in milliseconds; NetworkX analytics on this graph in low seconds. Because the prompt is size-capped, LLM latency is flat regardless of estate size — so scaling pressure lands on the analytics, which parallelise per community. For an interactive design tool, sub-minute is the bar and we're well inside it.

---

## D. Guardrails, security & failure engineering

**21. Prompt injection through the data — walk me through the kill chain.**
**Four layers, and it only needs one.** One: data-instruction separation — the contract is in the system prompt, data sits in the user turn. Two: `json_object` schema — no free-text channel out. Three: grounding — the model may only reference entities present in the payload, and the engine discards any invented name, so "QM_HACK" dies here. Four: output-side enforcement — even a fully hijacked reply must pass schema plus the eight deterministic constraints before it can touch the target state. An injection has no landing zone.

**22. Explain the circuit breaker. Why 60 seconds? What happens to a run in flight?**
**It's a time-based trip: the first 429 sets a rate-limited-until timestamp sixty seconds out, and every LLM call inside that window short-circuits immediately to the deterministic path — no waiting, no queuing, no stalled run.** Mid-flight, that means one throttle degrades *that call*, not the pipeline; the state records which method produced the design, so the reviewer sees it. Why not exponential backoff? Backoff makes sense when you have no alternative but to wait — we have a free, instant, valid alternative, so waiting on a throttled API is strictly worse than degrading. Sixty seconds matches the provider's rate-window granularity and it's a config value, not a constant of nature. And the half-open behaviour is implicit: the first call after expiry probes the service, and success resumes normal operation.

**23. The Ask-the-Architect chat is free-text. Can it be jailbroken?**
**It's the most exposed surface in the system, and it's read-only by design — that's not an accident.** The chat can *explain* the design; it's grounded in this run's state and ADRs. But it has no write path: nothing it says can mutate the target graph. Changes flow only through Revise, where the Feedback Interpreter's output is structured directives that then pass the Architect's full validation. So a jailbroken chat can, at absolute worst, say something embarrassing. It cannot *do* anything.

**24. Enumerate the failure modes.**
**Every LLM failure degrades to the deterministic path; no failure blocks a run.** Rate limit → circuit breaker, mid-run, seamless. Malformed JSON → the exact parse error is fed back for the model to repair its own output, capped at two retries, then discard. Timeout, credential failure, oversized payload → immediate graceful degradation. The state records `architect_method` either way — the reviewer always knows which path produced what's in front of them. The one failure we *want* loud is data-quality failure, and the Sanitiser reports that before anything downstream runs.

**25. What data leaves the bank when you call the model?**
**Nothing leaves — Tachyon is the internal gateway, so calls stay inside the approved perimeter under a registered USE_CASE_ID.** And the payload itself is a structural summary: topology aggregates and entity identifiers. No message content, no credentials, no customer data — none of that even enters the pipeline.

**26. The model returns something *valid* but architecturally *bad*. What catches it?**
**Rules catch invalid; humans catch unwise — that division is deliberate.** And the reviewer isn't reviewing blind: every AI decision arrives with an ADR stating context, rationale, and consequences; the metrics quantify the design; and the chat lets them interrogate any specific choice. Nothing provisions without a human signature. There's also a Design Critic role whose entire job is adversarial review of the proposed topology before a human ever sees it.

---

## E. The math — *and he will ask why it's there at all*

**27. You have a frontier model. Why bother with Louvain, MST, centrality — why not let Gemini reason over the graph?**
**Because an LLM is a pattern-matcher, not a graph processor — it will *approximate* betweenness centrality, and an approximated single-point-of-failure is a wrong single-point-of-failure.** A 259-node, twelve-and-a-half-thousand-edge graph is beyond faithful in-context computation for any model today. NetworkX computes the exact answer in seconds, for zero tokens, using algorithms with fifty years of literature behind them that a regulator can verify. This is the neuro-symbolic pattern done properly: symbolic computation establishes the *facts*, the model reasons over the facts for *judgment*, and the engine enforces the *invariants*. Asking the LLM to also be the calculator would make the system slower, costlier, and unauditable — in one move. The inverse is equally true, by the way: the algorithms can't decide that a payment gateway deserves its own blast-radius zone. Each layer does the one thing it's provably best at.

**28. Who chose the six-factor weights, and how would you defend 25/25/20/15/10/5?**
**Expert-informed priors: coupling and channel count are the dominant drivers of migration cost and blast radius, so together they carry half the weight.** Each factor is normalised 0–100 against baseline, so the headline story — 100 down to 45.4 — is robust to any reasonable reweighting; we sanity-checked that. Calibrating the weights against *realised* migration effort across estates is a genuine improvement we've scoped for Day-2 — and honestly, the per-metric numbers matter more than the composite: coupling to exactly 1.0, orphans and violations to zero. Those are weight-independent.

---

## F. Governance, production & the business — the CIO layer

**29. How does this get past model risk management? (SR 11-7 energy)**
**The design is MRM-friendly by construction: the model never makes a unilateral decision — every output is machine-verified, human-approved, and ADR-documented, which puts it in decision-support territory, not autonomous decisioning.** Tachyon usage is registered per USE_CASE_ID. Prompts are versioned artifacts in the repo — a prompt change is a code change with review. The deterministic path is a documented degraded mode. And the audit story is complete: for any change to the estate we can produce the input, the prompt, the raw response, the validation verdict, the ADR, and the human approval, end to end.

**30. Two runs produce different designs. Which is the system of record?**
**The approved one — the human signature plus its ADR set is the record, exactly as it would be with two senior architects producing two valid designs.** Both runs are guaranteed compliant; they differ only in which valid optimizations were applied. And because we log the full prompt-response pair, any historical design is exactly explainable even if not bit-identically regenerable. Auditability doesn't require determinism — it requires traceability, and we have that per decision.

**31. Who's allowed to click Approve? Where's the maker-checker?**
**The gate *is* the maker-checker point: the AI is the maker, the human is the checker — and productionising it means binding the review API to entitlements.** The platform owner approves; a requester can't self-approve; above a blast-radius threshold you require a second approver. The state already records the decision and its context — wiring identity and entitlements onto that is straightforward plumbing, and it was designed as a first-class API endpoint precisely so that's possible.

**32. What's your observability story for the AI components?**
**Every agent step streams to the state's message log — that's literally the Trace tab you watched — and every LLM call logs role, attempt, latency, and outcome.** Production hardening adds span-level tracing and token accounting per use case — OpenTelemetry or LangSmith-style. But the part that's hard to retrofit already exists: per-decision traceability from input, through prompt, through validation, to ADR.

**33. What does a run cost, and what's the cost curve at enterprise scale?**
**Cents — one design pass is a handful of model calls totalling roughly fifteen to twenty-five thousand tokens across the roles.** The economics come from the architecture: the 438 routine assignments cost zero because the engine does them, and the prompt is size-capped, so cost scales with the number of *decisions*, not the size of the estate. The expensive resource this replaces isn't compute — it's weeks of a senior architect's time.

**34. Garbage in — the CSV is wrong or incomplete. Then what?**
**That's why the Sanitiser is a first-class agent, not a preprocessing script: dedupe, normalisation, referential integrity checks, and a data-quality report with issue counts before anything downstream runs.** The Anomaly Detective then flags suspicious patterns in what survived. What no system can fix is missing truth — a flow absent from the export can't be designed for — which is precisely why the human gate and per-step rollback exist. We degrade to caution, never to confidence.

**35. When would you trust it enough to remove the human gate?**
**That's a risk-appetite question, not a model-quality question — and the gate is cheap, so the bar should be high.** The path is tiered autonomy backed by longitudinal evidence: after enough approved runs with zero post-approval corrections, you auto-approve the low-blast-radius classes — orphan cleanup, dead-channel removal — and keep the human on compliance-zone moves and payment-critical changes. Full autonomy over payment infrastructure isn't something I'd propose to you; graduated autonomy with evidence is.

**36. What did the model actually get wrong while you were building this? Be honest.**
*(He's testing candour — have real examples ready and own them.)* **Every guardrail on that slide exists because we hit the failure it prevents.** Early on the model wrapped JSON in prose — that's why json_object mode plus retry-with-the-exact-error exists. It invented queue manager names that weren't in the data — that's where grounding came from. Given vague feedback it over-consolidated — that's why the Feedback Interpreter produces structured directives with a protect-list. The guardrails aren't theoretical hygiene; they're scar tissue, and I can trace each one to the incident that caused it.

**37. Prove the LLM adds value over the deterministic engine alone.**
**Run both modes and diff the output — the delta is itemised.** The engine alone gets you compliant. The AI adds the ten to twelve judgment calls: the PCI isolations, the payment-critical zoning, the bridge-app placements — each one with an ADR a rules engine could never write, because they're decisions over *business* metadata, not graph structure. Compliant versus compliant-and-well-architected, and the diff is the proof.

**38. Biggest weakness. One answer, no hedging.**
**No offline eval benchmark for the AI contributions yet.** The Tester and the human gate cover safety, but systematic quality regression — especially across model versions — needs a golden set, and we haven't built it. The honest silver lining: the pipeline is generating its own labelled data for exactly that, every run, in the ADR and acceptance logs. It's our first post-hackathon item.

---

## The three sentences to land, whatever he asks

1. "Twelve specialist AI roles — compliance, risk, architecture, human intent — every decision verified, recorded in an ADR, and signed off by a human."
2. "Classical algorithms compute the facts, Gemini reasons over them for judgment, the engine enforces the invariants — each layer does the one thing it's provably best at."
3. "Every AI decision in this system is auditable, defensible, and reversible — that's what makes it bankable."

## If he asks something you don't know

Never bluff a 30-year CIO — he's calibrated to detect it. Use: **"Honestly, we haven't tested that boundary — but here's how the architecture would handle it, and here's how I'd verify."** Reasoning from your own architecture under pressure impresses him more than a memorised answer. And if he corrects you on something, take it: "That's a fair point — that's exactly the kind of input we built the gate for."
