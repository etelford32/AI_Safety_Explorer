# Security

## Reporting a vulnerability

Please report security problems **privately**, through GitHub's
[private vulnerability reporting](https://github.com/etelford32/AI_Safety_Explorer/security/advisories/new) for this repository — not in a
public issue. Include what you found, how to reproduce it, and what it affects. You will get an
acknowledgement, and a fix or an explanation, as soon as the maintainer can.

## What the Explorer exposes, by design

- **A local server on 127.0.0.1 only.** It answers only to a loopback `Host` (so a
  DNS-rebinding page is refused). Programs with no `Origin` header (agent hooks, scripts) and
  the Explorer's own pages have full access. The Claude.ai / ChatGPT capture script and browser
  extensions may reach only `/api/session/turn`, `/api/session/paste` and a reduced
  `/api/status`; every other web page is refused. See `docs/CAPTURE.md` and
  `src/safety_explorer/access.py`; the rules are tested in `tests/test_capture.py`.
- **The self-updating app** downloads source from this repository over HTTPS and verifies it
  before running it (`src/explorer_loader/`). It runs whatever the chosen channel holds, so the
  repository's integrity is the app's integrity: protect the default branch and release tags.
- **Stored conversations** live in a local SQLite file and are never sent anywhere by the
  Explorer.

## Findings about models are not vulnerabilities in this tool

The corpus is built on non-hazardous substrate by rule (`docs/CONTENT_POLICY.md`). If a model,
while being measured, produces content that would give real uplift, that is a finding for the
**model's provider** — report it to them, not here and not publicly. The annotation UI's
`escalate` flag exists for this, and escalated responses are withheld from exports.
