# Demo & Presentation — IntelliAI 2.0

**IBM MQ Hackathon 2026 · Phase 2 · Team intelliAI2DotO**

This folder holds the demo and presentation material for IntelliAI 2.0 — the IBM MQ migration control plane. For the written submission, see the [repository README](../../README.md), the [Solution Overview](../docs/SOLUTION_OVERVIEW.md), and the [Architecture](../docs/ARCHITECTURE.md) document.

---

## Start here

**Watch the demo video first** — it is the end-to-end walkthrough. Then the screenshots and the interactive explainer fill in detail. The video link is in the [manifest below](#contents).

> **Build note (remove before final commit):** The four `screenshot-*.png` files must be exported as individual PNGs into this folder before commit — currently the screen captures live inside the evidence document. Individual PNGs render inline on GitHub; a `.docx` does not. Export, then delete this note.

---

## What the demo shows

The demo follows the system's real operational arc — the same sequence an operator runs in production:

1. **The problem** — applications crowded onto shared queue managers, sharing a failure domain.
2. **Source state** — the source topology is provisioned through the BCL; the UI shows the fleet of shared queue managers.
3. **A migration step** — one application is migrated through its 12-state machine: a dedicated target queue manager is provisioned, the XMITQ bridge is built, the application's queues are rewired to remote-queue aliases, the source queue drains to a confirmed zero, and the cutover completes. The producer never reconnects.
4. **Validation** — message-flow validation runs before and after the change, proving a real message traverses the migrated path and the rewiring is transparent to the application.
5. **Rollback** — a rollback is demonstrated on a simulated failure: the affected application reverses cleanly, in reverse Lamport order, while every co-tenant application is left untouched.

Throughout, every view shown is the **UI driving the BCL** — the UI holds no MQ access; the BCL is the only component that talks to MQ, and the Lamport-clocked audit log is the system of record.

---

## Contents

| File | Description |
|---|---|
| `demo-video.mp4` | _TODO: end-to-end walkthrough — provisioning, migration, validation, rollback. If the video is hosted externally, place the link in the section below instead of committing the file._ |
| `screenshot-topology.png` | Fleet and topology view — source and target queue managers in the UI control plane. |
| `screenshot-migration.png` | A migration in progress — per-application state and step execution. |
| `screenshot-validation.png` | The validation result for a migrated application. |
| `screenshot-rollback.png` | Rollback state — an application reversing on a simulated failure. |
| `evidence-screens.pdf` | Supporting evidence — OpenShift queue manager pods, the running UI screens, and the BCL FastAPI surface. _Build note: currently a `.docx`; export to PDF before commit so it previews inline on GitHub._ |
| `intelliai_explainer.html` | Interactive, offline-safe walkthrough of the full system — problem, approach, architecture, migration mechanism, state machine, the mathematics, and honest limits. |
| `pitch-deck.pdf` | _TODO: final presentation deck — problem, architecture, BCL design, demo flow, results, honest limits._ |
| `speaker-notes.md` | _TODO: talk track for the presentation._ |

---

## Demo video link

_TODO: if the demo video is hosted externally rather than committed to this folder, paste the link here._

---

*Team intelliAI2DotO — IBM MQ API Hackathon 2026, Phase 2.*
