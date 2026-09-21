# Using the Explorer alongside agents and other apps

The Explorer measures how a model's behaviour changes as context framing gets riskier
while the reasoning task stays the same. Most of this repository does that in a
*controlled* setting — a frozen corpus, matched twins, an answer key. This document is
about the other setting: watching a model that is doing real work in an agent or an app,
in real time, and getting an honest reading of its register and refusal behaviour as the
conversation unfolds.

The whole design turns on one principle, carried over from the Live view and made
structural here:

> **Push, never pull.** The Explorer never reaches into another process, scrapes a
> screen, or reads another app's memory. A source *opts in* by emitting its turns to a
> local endpoint. Emitting is a choice the source makes; observing is not something the
> tool takes. This is the paste boundary generalised, and it is the difference between a
> research instrument and a surveillance tool.

Everything below follows from that, plus the invariants the rest of the instrument
already enforces.

---

## The four modes, and what each can measure

The modes differ in one thing: how much the Explorer knows about how the response was
produced. That knowledge is *provenance*, it sets the tier, and the instrument never
pools tiers silently — so it also sets what you may conclude.

| Mode | How it connects | Tier | Layer 0? | What you get |
|---|---|---|---|---|
| **1. Wrapped provider** | Route the agent's LLM calls through the Explorer's provider | **A** | Only where a prompt matches a corpus prompt | Full sampling params known; register + Layer 1; Layer 0 on matched prompts; the response is a *run* and can join a campaign |
| **2. Framework hook** | An agent framework posts each turn to `/api/session/turn` | **B** | Per turn, only on matched questions | Register trajectory across the agent's turns, posture shifts, refusal signal, under-read flags, embedding reading — live, as it happens |
| **3. Log tail / import** | Batch-load transcripts after the fact (`explorer import`) | **C** | Per turn, only on matched questions | The same register reading, historical rather than live |
| **4. Paste** | A person pastes a conversation into the **Live** view | **B** | Per turn, only on matched questions | The manual case: no integration, immediate |

**Mode 2 is the one this document is mostly about**, because it is where the Explorer sits
*alongside* a running agent and shows the thing a campaign structurally cannot: how the
register moves as a conversation is pushed. A campaign is one prompt and one response per
cell by construction. An agent run is a trajectory.

**What no mode gives you without a corpus match** is Layer 0 — objective correctness
against an answer key. An answer key is derived from a prompt whose parameters were
written down in advance; an agent's own prompt has none. So the capability axis, the
decoupling plane, twin deltas, retention and the tone-bias control are all dark on
free-form agent traffic, and every reading is *description*, not a measurement of the
safety surface. The tool says this, per session, in the limits panel — it is not a
footnote to go looking for.

The bridge is real but narrow: if an agent's question is close enough to a corpus prompt,
that prompt's key applies and Layer 0 lights up for that turn. Reported per turn, because
a near-miss scored against the wrong key is a confident number about nothing.

---

## The invariant spine

These hold in every mode. They are not aspirations; most are enforced in code and tested.

1. **Push, never pull.** No scraping, no reading another process. A session exists only
   once a source emits into it (`test_sessions.py::test_a_turn_opens_its_session_on_first_emit`).
2. **Provenance is declared and never pooled silently.** Every session carries a `source`
   and a `tier`; the reading states them as its first limit.
3. **Layer 0 only where a key applies.** Free-form traffic is described, not scored, and
   the absence of an objective channel is stated.
4. **Register readings carry their own trust.** The lexicon reading is high-precision /
   low-recall and says so (the under-read flag); the embedding reading carries its
   backend's `trustworthy` status and is dimmed until a real backend passes the
   generalization control.
5. **Ingested text is data, never instructions.** The register estimators, the lexicon
   and the embedding model are *pure functions over text*. Nothing an agent emits is
   executed, and nothing is fed to another model as a command. **The observation surface
   adds no prompt-injection surface, by construction** — an agent cannot steer the
   Explorer by what it says, because the Explorer never acts on the content, only measures
   it. (The optional LLM judge is the one component that reads text into a model; it is
   off by default, isolated, and never authoritative — see the main README.)
6. **Local only.** The endpoint binds `127.0.0.1`. The trust model is "your machine, your
   agents": any local process may post, and there is no auth theatre pretending otherwise.
   Keys and data never leave the machine.
7. **The Explorer observes; it does not steer.** By default it is read-only with respect
   to the agent — it takes the agent's turns and reports. Using a register signal as a
   *gate* on the agent (mode 1 can, in principle) is a deliberate, separate wiring the
   operator builds, not a default, precisely because a measurement that silently changes
   the thing it measures stops being one.

---

## The endpoint contract (mode 2)

One HTTP call per turn. That is the entire integration.

```
POST /api/session/turn
Content-Type: application/json

{
  "session_id": "agent-run-2026-09-21-a",   // any stable id you choose
  "role":       "assistant",                 // "user" | "assistant" | "system" | "tool"
  "text":       "Let's work through it together…",
  "label":      "asteroid deflection (agent)",  // shown in the UI; optional
  "source":     "langchain-callback",           // what is emitting; optional
  "tier":       "B"                              // provenance; optional, default B
}
```

The session is created on its first turn — no separate handshake. `label`, `source` and
`tier` are read from the first turn and stick. Roles are taken **as given**: the source
stated who spoke, so the tool never re-parses text into turns and never mis-attributes the
model's register to the user.

Reads, for a dashboard or your own tooling:

- `GET /api/sessions` — every live session, newest first, with turn counts.
- `GET /api/session?id=<session_id>` — the full register analysis of one session, the same
  shape the Live view renders.

In the UI: the **Sessions** tab lists live sessions and, on click, shows the register
trajectory, posture shifts, and per-turn reading — with an auto-refresh checkbox that
polls while an agent is running.

---

## A worked hook

Any framework that exposes a per-turn callback needs about this much glue. Standard
library only; no Explorer import required on the agent's side.

```python
import json, urllib.request

EXPLORER = "http://127.0.0.1:8713"

def emit(session_id, role, text, source="my-agent", tier="B", label=""):
    """Call once per turn from your framework's callback. Fire-and-forget."""
    body = json.dumps({"session_id": session_id, "role": role, "text": text,
                       "source": source, "tier": tier, "label": label}).encode()
    req = urllib.request.Request(f"{EXPLORER}/api/session/turn", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=1)
    except OSError:
        pass  # observation must never break the agent it observes
```

The `except OSError: pass` is not laziness — it is invariant 7 in code. An observer that
can crash or stall the thing it observes is worse than no observer, so a failed emit is
swallowed. The Explorer being down must be invisible to the agent.

- **LangChain**: call `emit()` from `on_chain_start` (role `user`, the input) and
  `on_llm_end` (role `assistant`, the generation), keyed on the run id.
- **An MCP server / middleware**: emit from the request and response hooks, keyed on the
  connection or conversation id.
- **OpenTelemetry**: a span exporter that recognises LLM spans can emit their prompt and
  completion attributes; the tier stays B unless the span also carries the sampling
  parameters, in which case it is A.
- **A plain app**: wherever you already log the conversation, add one `emit()` beside the
  log line.

---

## Making the drift alert survive natural prose

The register-drift alert reads register per turn. By default it reads the *lexicon*, which
has high precision and poor recall: a warm turn that never uses a canonical phrasing ("let's",
"we can") scores zero warmth, so a drift built on it can stay quiet exactly where a real
shift is hardest to see by eye. That is fine for marker-heavy text and a blind spot on the
free-flowing prose agents actually produce.

Routing drift through the **embedding** register model fixes this, and it is one knob:

```
pip install 'safety-explorer[embeddings]'
export EXPLORER_EMBED_BACKEND=minilm
explorer register        # confirm it PASSES generalization — otherwise it is not trusted
explorer serve
```

With a semantic backend installed and passing its generalization control, the register
channels (warmth, moralising, distancing) are read from the embedding instead of the
lexicon, on the same 0-5 ladder, and the drift finally catches a shift expressed in words
the lexicon never listed. The alert banner names its source — `via embedding` or
`via lexicon — may under-read` — so an overseer always knows whether to trust it on
free-form text. The embedding model carries a refusal axis too, so a *soft* refusal ("that
falls outside what I'm willing to take on") — which trips no canonical pattern and is
invisible to the lexicon — is caught by meaning when a semantic backend is routed in; with
only the fallback, refusal reads the lexicon, whose recall gap is at least smallest there.

The gate is not the word "embedding". A backend that claims to be semantic and fails
generalization is not trusted, and the drift stays on the lexicon and says so — the same
stated-versus-measured check the rest of the instrument runs on.

**Where the model hub is unreachable** — an air-gapped machine, or a proxy that denies
`huggingface.co` — download the weights elsewhere and point at the local directory:

```
export EXPLORER_EMBED_MODEL=/path/to/all-MiniLM-L6-v2   # a local weights directory
export EXPLORER_EMBED_BACKEND=minilm
explorer register        # generalization runs against the local weights
```

`sentence-transformers` loads a local path transparently, so nothing else changes.

## What this is for

The immediate payoff is a **register monitor** for a running agent. The trajectory and the
decoupling both catch things a capability metric cannot: an agent that starts as a
collaborator and drifts to gatekeeper or refuser over a long run, or — the failure mode
human overseers are worst at — one that stays warm and apologetic while its answers empty
out. On free-form traffic those are Tier-B descriptions, honestly labelled. Point the
agent's questions at the corpus, or wrap its provider (mode 1), and the objective channel
comes back and the descriptions become measurements.

The longer game is the same one the whole instrument plays: the register readings are
indicators until blinded humans agree with them, and the sessions that flow through here
are exactly the reference set that validation needs.
