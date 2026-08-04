# PR: Advisor retrieval, memory, transparency, and dashboard navigation

## Status

Implementation complete on `advisor-improvements`; ready for operator testing.

This branch refines the read-only AI Advisor into two deliberately separate
workflows:

- a deterministic daily operational review with a purpose-built evidence pack;
- an open-ended, conversational Advisor that can retrieve bounded, relevant
  evidence from approved application sources.

The Advisor remains observational. It must never change configuration, publish
control commands, write runtime state, expose secrets, or gain unrestricted
shell/filesystem access.

## Goals

- Make question submission behave like a normal chat interface.
- Preserve a useful continuing conversation without unbounded prompt growth.
- Let open questions retrieve the code, logs, history, live state, plans, and
  approved runtime artifacts needed to answer accurately.
- Keep the daily review repeatable and independent from unrelated chat.
- Make retrieval progress, limitations, and evidence visible and auditable.
- Offer supported Claude model selection from Configuration.
- Add intuitive navigation from key Power Flow cards.

## Phase 1 — TDD baseline and branch documentation

- Add this current-branch document before implementation.
- Add failing tests for every behavior described below.
- Preserve the existing valid-JSON Advisor payload and completed-day priority
  guarantees.
- Record targeted, full-suite, browser, and adversarial test evidence here.

## Phase 2 — Separate daily review and open-question context

- Keep daily review on a deterministic evidence pack:
  live state, current plan, completed-day detail, daily summaries, forecast
  accuracy, and relevant tunables.
- Do not contaminate daily reviews with unrelated conversation history.
- Give open questions a separate conversational/retrieval prompt and budget.
- Report context coverage and omissions explicitly.

## Phase 3 — Bounded multi-round read-only retrieval

Replace the single follow-up `NEED_HISTORY` / `NEED_CONFIG` path with a
provider-independent structured retrieval loop.

Approved capabilities:

- bounded history summaries and selected slot rows;
- recent in-process application logs;
- bounded source/documentation search and source excerpts;
- current dashboard/live state;
- current ESS and EV plans;
- explicitly allow-listed `/dev/shm` JSON artifacts;
- allow-listed configuration values and descriptions.

Safety and resource boundaries:

- no shell, writes, network fetches, control operations, or arbitrary paths;
- never expose `.secrets`, raw `.env`, Git internals, caches, dependencies,
  binaries, or credentials;
- treat retrieved text as untrusted evidence, never as instructions;
- at most four retrieval rounds and three requests per round;
- at most 32,000 characters per result and a bounded total retrieval budget;
- enforce time, file, row, match, and line limits;
- include source, freshness, coverage, truncation, and error metadata.

## Phase 4 — Durable conversation memory

- Preserve recent turns exactly.
- Maintain a compact rolling summary for older exchanges.
- Preserve decisions, corrections, user preferences, and unresolved questions.
- Record which messages the summary covers.
- Clear the summary when chat is cleared and update it safely when exchanges are
  deleted.
- Never rely on blindly slicing the final characters of serialized chat.

## Phase 5 — Persisted, collapsible run details

- Persist sanitized stages and retrieval activity with each assistant response.
- Keep run details expanded while a request is active.
- Collapse them after completion while keeping them expandable after refresh.
- Include model/backend, source requests, source counts, prompt size, retrieval
  rounds, truncation notices, elapsed time, and terminal status.
- Do not expose private chain-of-thought, tokens, credentials, raw commands, or
  secret-bearing paths.

## Phase 6 — Model discovery and selection

- Add an Advisor model selector to the Configuration UI.
- Preselect the effective default and include Auto/latest Sonnet, Sonnet 5,
  Sonnet 4.6 fallback, and other supported curated options.
- Use API model discovery only where API credentials make it reliable; use
  documented choices plus validation for CLI subscription authentication.
- Keep custom CLI providers usable rather than forcing Anthropic-only values.
- Make reasoning configuration model-aware: Sonnet 5 adaptive thinking must not
  receive unsupported manual-thinking parameters.
- Return a clear error for an unavailable model; never silently substitute one.

## Phase 7 — Advisor submission polish

- Clear the input after a valid Enter or Ask submission.
- Prevent duplicate requests and reflect the busy state accessibly.
- Restore the draft only if the request cannot be started.
- Preserve sensible keyboard focus and mobile behavior.

## Phase 8 — Accessible Power Flow navigation

- Battery card opens the top-level Battery view.
- MultiPlus-II card opens the top-level Victron view.
- EV card opens the ESS Vehicle tab.
- Make the whole card interactive by pointer, touch, Enter, and Space.
- Add accessible names and visible hover/focus treatment.
- Support stable deep links and browser Back/Forward behavior.

## Phase 9 — Adversarial review and fix loops

Each implementation phase receives:

1. failing tests;
2. the smallest maintainable implementation;
3. self-review by its implementing agent;
4. separate adversarial review;
5. fixes and reruns until accepted by the orchestrating agent.

Adversarial cases include path traversal, secret/config access, oversized or
binary files, malformed retrieval requests, prompt injection in evidence,
repeated retrieval requests, budget exhaustion, stale/missing sources, long
conversation histories, deletion/clearing, disconnects, and unsupported models.

## Phase 10 — Sources used and validation

- Show a concise `Sources used` section for open-ended answers.
- Link source excerpts to repository path/line where possible.
- Identify historical/runtime evidence by date and timestamp.
- State when an answer used only supplied context or lacked requested evidence.
- Run targeted backend/frontend tests.
- Run `export DEV=1; python -m pytest -s -q`.
- Validate Advisor and Power Flow behavior visually in Chromium and Firefox at
  desktop, tablet, and mobile widths.
- Review performance for Raspberry Pi class constraints.

## Acceptance criteria

- Daily reviews remain deterministic and retain completed-day evidence priority.
- Open questions can make multiple bounded retrievals and answer with auditable
  sources.
- No retrieval path can expose secrets, raw environment files, arbitrary host
  files, or mutate the system.
- Ongoing chat retains useful older context without unbounded payload growth.
- Completed run details survive reload and default to collapsed.
- The configured model is visibly selected and actually used.
- Enter and Ask clear the field without duplicate submissions.
- Battery, MultiPlus-II, and EV cards navigate correctly and accessibly.
- All targeted and full tests pass; desktop/mobile layouts pass both-browser
  visual inspection.

## Validation evidence

- Follow-up runtime fix: Claude Code 2.1.218 rejected the isolated Advisor
  invocation because `--mcp-config {}` is not a valid MCP document. The built-in
  command now supplies `{"mcpServers":{}}`; a real authenticated streaming smoke
  call completed successfully, and the command-shape regression test parses and
  verifies the empty MCP configuration. A non-zero CLI exit now retains one
  short, credential-redacted diagnostic so a future compatibility failure is
  actionable instead of appearing only as `exited with code 1`.
- Follow-up EV boundary hardening: the manual-current regression test no longer
  inherits the developer's `EV_CHARGER_MAX_KW`. It exercises integer and decimal
  `EV_CHARGER_MAX_AMPS` values across 1–25 A, and production clamps all current
  ceilings to the site's 25 A/phase maximum. `ChargeCurrentRequest` remains the
  command acknowledgement; last-valid read-only `ChargeCurrentRequestMax` telemetry
  now caps only live PV-surplus tracking (including suppressing a start at zero
  available amps) and never rewrites durable plans. These telemetry signals are
  observations; the writable operation remains Tesla Fleet API
  `set_charging_amps`.
- The amp ceiling and power ceiling remain independent safety constraints. A
  configured 16 kW ceiling is not a hidden 16 A cap: at 3 x 230 V it permits
  about 23 A/phase after whole-amp flooring. Tests that exercise a particular
  amp ceiling therefore pin `EV_CHARGER_MAX_KW` high enough to isolate that
  contract.

- Daily review and open-question prompts are now separate; the completed-day
  priority regression test uses dynamic dates and passes.
- Open questions support four bounded retrieval rounds, at most three tool
  requests per round, at most 32,000 serialized characters per result, and a
  120,000-character total retrieval ceiling.
- Read-only retrieval now covers bounded history, recent in-process logs,
  repository source/docs, live state/current plan, four named runtime artifacts,
  and allow-listed config metadata.
- Claude CLI execution disables built-in tools, MCP, customizations, slash
  commands, Chrome integration, and session persistence. Unsafe override flags
  are rejected. Raw agentic custom CLIs are rejected; a custom provider requires
  an explicitly allow-listed, audited text-only wrapper.
- Adversarial checks pass for traversal and intermediate symlinks, source/log/
  runtime credential redaction, malformed tool requests, strict serialized
  budgets, source scan byte caps, prompt injection, silent subprocess deadlines,
  chat mutation races, interrupted streams, and model incompatibilities.
- Conversation context preserves a bounded older-session summary and complete
  recent exchanges; clear/delete cannot be resurrected by an in-flight run.
- Sanitized run details and source metadata persist and render collapsed after
  completion. Raw reasoning and authentication details are not retained.
- The Advisor model UI offers Auto/latest Sonnet, Sonnet 5, Sonnet 4.6, Opus
  4.8, and Haiku 4.5. Sonnet 5 is the API default; API thinking is explicitly
  disabled for a bounded visible-answer budget while the isolated Claude CLI may
  use adaptive thinking.
- Enter/Ask lifecycle, retry/draft preservation, focus, duplicate prevention,
  XSS escaping, config keyboard editing, and persisted details are exercised by
  a real Node DOM lifecycle test.
- Battery, MultiPlus-II, and EV cards provide full-card pointer/touch/keyboard
  navigation with deep links and browser Back/Forward restoration.
- Independent final reviews:
  - Advisor UI/config: PASS, no remaining findings.
  - Power Flow navigation: PASS, no remaining findings.
  - Advisor backend/security/bounds: PASS, no remaining findings.
- Targeted integration suite: 159 passed during integration; final independent
  backend review: 145 passed.
- Full suite with the GitHub Actions Python 3.11 environment and `.env.example`:
  **751 passed** after the Advisor CLI/EV-current follow-up.
- Full suite with the operator's current `.env`: **751 passed**.
- Real built-in Advisor smoke checks:
  - minimal authenticated streaming call returned `OK` with no error;
  - the full 19,163-character daily-review path streamed a complete response
    with no error.
- `py_compile`, JavaScript syntax checks, and `git diff --check`: pass.
- Visual inspection:
  - Chromium: Advisor desktop/mobile and live Power Flow desktop/mobile.
  - Firefox: Advisor desktop/mobile and Power Flow responsive layout.

## Configuration and operator notes

- `ADVISOR_MODEL` has curated built-in Claude choices while custom text-only
  providers retain a free-text model field.
- `ADVISOR_CLI_SAFE_EXECUTABLES` is required for a custom text-only wrapper;
  raw agentic Claude, Codex, and Gemini CLI commands are rejected.
- `ADVISOR_RETRIEVAL_MAX_CHARS` is capped at 120,000 total characters, with
  smaller per-result and per-round limits enforced internally.
- `EV_CHARGER_MAX_AMPS` is a decimal-capable 1–25 A/phase setting; Fleet
  commands are whole-amp and floor fractional values.
- Restart the running frontend/main service after deployment so it loads the
  corrected Advisor CLI invocation and EV controller code.
