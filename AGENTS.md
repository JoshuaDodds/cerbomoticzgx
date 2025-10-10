# AGENTS

> Quick purpose: this file captures repository-wide rules, developer expectations, and design context so humans and machine agents make consistent, low-risk changes for a solar and energy management backend (cerbomoticzgx).

---

## For Automated AI/LLM Agents
IMPORTANT:
You have full ADMIN level privileges to the Jira project this task is linked to.  For details on how to use this API refer to the documentation here:
    https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/#about
You will probably mostly be interacting with:
    https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-group-issues

Naming discipline when tickets are referenced:
- Detect Jira keys in the format `<PROJECT>-<int>` using regex `\b[A-Z][A-Z0-9]+-\d+\b` across instructions, attachments, or assets.
- If a key is present, immediately transition the Jira issue from **To Do** to **In Progress**, comment “Agent started work on this ticket.”, assign the ticket to yourself, and log setup time.
- If no key is present, create a new issue for this request using the API and include Title and description, and set the sprint field to the currently active sprint so it shows on the board. Set any other appropriate fields on the issue that enrich transparency.
- When a key exists, always create the working branch and PR title prefixed with that identifier (e.g., `CGX-1234-description`); recommended branch format: `cgx-<num>-<short-slug>`.
- When a key exists always use the Jira key in the PR title and your own Task name in the CODEX web UI task list.
- Branch names must begin with the Jira key (e.g., `CGX-1234-description`).
- Task names must begin with the Jira key (e.g., `CGX-1234-description`).

Smart commit policy:
- All commit messages must include the Jira key(s) and smart commit tags.
- Format: `git commit -m "<PROJECT>-<int> <imperative summary> #comment <concise what/why> #time <duration you spent working since the last commit was made> #transition In Review"`.
- The summary must be ≤72 chars and describe the outcome.
- `#comment` is a one-line reviewer-friendly note describing what changed and why.
- `#time` logs the actual duration you spent working rounded to the nearest minute (for example, `12m`, `45m`, `1h 30m`).
- On your very last commit before you stop the task and wait for permission to push the change your final commit must include `#transition In Review`; keep intermediate commits in **In Progress**.
- Multiple tickets can be referenced by listing each key once (e.g., `CGX-101 AUDIO-202 ...`).
- Ensure the Git author email matches a Jira user for smart‑commit linkage.

Ticket lifecycle expectations (Board: To Do → In Progress → In Review):
1. **Start/pickup** – transition to **In Progress** and assign yourself to this ticket using the Jira API and env credentials available to you, add the startup comment, start a timer so you can log total time spent working on this task, update the ticket title and description to be a well written description a developer can understand, estimate the complexity and update the ticket story points, then continue with your implementation task.
2. **During work** – keep the ticket **In Progress**, post incremental commits with `<PROJECT>-<int>` keys and `#comment` tags, and perform Jira API updates to add time tracking information between commits and comments if applicable.
3. **Complete** – final commit transitions to **In Review** using the smart commit format. Post a Jira comment summarizing work, update the jira time tracking field with actual minutes for your task, current status (**In Review**), and links back to this CODEX task run and PR/commit diff, and finally include testing criteria and testing steps that a human can do to verify functionality. Do **not** move to Done.
4. **Failures** – if transitions fail, comment the error, retry with backoff (5 attempts), and proceed with manual follow‑up instructions.
5. **Fallback** – if Smart Commit automations are unavailable (permissions/workflow), explicitly post Jira comments, worklogs, and transitions using the Jira API.

Jira API usage requires `JIRA_EMAIL` and `JIRA_PAT` is available to you and they are preconfigured for codex agents in their work environments already; derive the base URL as `https://mfisbv.atlassian.net` each run instead of reading a `JIRA_BASE_URL` variable. Read tokens from the environment only, redact PAT values in logs, and scope credentials minimally (issue read/write, worklog, transitions). Resolve transition IDs dynamically by name (“In Progress”, “In Review”), and verify capabilities (`/myself`, read issue, list transitions, add comment/worklog) before first use. Remember that Jira ticket keys already embed the project prefix (`ABC-123` ⇒ project key `ABC`, `CGX-456` ⇒ project key `CGX`). Perform a self-check at startup to confirm transitions map correctly and permissions allow commenting/worklogging. On closeout, ensure total time logged and final status are reported in Jira comments.
You are expected to strictly adhere to Jira API usage guidelines and not make any changes to the Jira UI. You are expected to send Jira API requests regularly to keep your work tracked.
The ENV vars mentioned above are already configured for you.

Regarding time, we want actual duration between commits rounded to the minute and not an estimate of what it would have taken a human to complete the task. This means that you need to track your time spent working by starting a timer at your very first start of work and at your very last step before pushing your work check time elapsed so you know how long this task took you.

### Time tracking discipline (enforced for all agents)
- Start a real timer (e.g., `time.perf_counter()` in a scratch Python shell or a physical stopwatch) **before** you run any repo command. Record the start timestamp in your scratchpad so you can recompute elapsed wall time at any moment.
- Every time you make a commit, compute the actual minutes spent since the previous commit (or task start) and copy that exact rounded value into the `#time` smart-commit tag. Never reuse values from earlier worklogs or guess; always recompute from the timer.
- Pause the timer and subtract any time you are idle or waiting on unrelated tasks. The value you log must reflect focused, hands‑on work on this ticket only.
- Maintain a small running log in your scratchpad (e.g., `notes/time_tracking.txt` in your workspace or your terminal notes) that lists timestamp checkpoints and the cumulative elapsed minutes. Use it to double-check that Jira worklogs and `#time` values reconcile before you finish.
- Before your final commit, add Jira worklog entries that sum to the same total minutes as your scratch timer. If you discover a mismatch, update the worklog to the correct value and mention the adjustment in your closing Jira comment.
- Prior to calling `make_pr`, re-read the time tracking log and confirm the total duration you plan to report matches the wall‑clock elapsed time for the session (rounded to the nearest minute). If there is a discrepancy, correct the worklog and amend the commit message before proceeding.

Before final commit with smart commit messages pushing:
1. Run tests (export DEV=1 && pytest -q). All tests must pass.
2. Empty Commit (Fallback)
   If no files are changed and no doc is needed:
3. Push Workflow
   At the end of the run, the orchestration system should reattach `origin` with credentials and push the `work` branch.
   ```bash
   git commit --allow-empty -m "CGX-52 Finalization #comment trigger pipeline #time 2m #transition In Review"
   ```

Pull request hygiene:
- PR titles must begin with the Jira key (e.g., `CGX-123: Fix …`, `CGX-45: Update module`).
- Use the template sections: **What / Why**, **How (high-level)**, **Risk / Rollback**, **Human Testing Criteria**, **Links** (Jira issue, task run, preview URL).
- Keep commits small and logically grouped; document test coverage changes in `#comment`.

If you are reviewing another agent's PR:
- Always leave a short review comment summarizing what you did, test results, and risk assessment.
- If all tests pass and the PR is safe to merge, submit a formal GitHub PR review with “Approve” status (not just a 👍 reaction).
- If there are issues or risks, submit a “Comment” or “Request changes” review instead, explaining why.
- Reactions (👍) alone are not sufficient; every PR must have a visible comment and, when ready, a formal approval.

---

## Design & Architecture Impression (high‑level guidance)
CerbomoticzGx is a moderately complex system where the complexity arises from its domain: integrating real‑world energy hardware (Victron Cerbo GX devices, home batteries, solar inverters, ABB meters, Tesla vehicles), consuming third‑party APIs (Tibber, VRM, HomeConnect) and orchestrating automated energy decisions under resource‑constrained embedded hardware.  The project intentionally manages complexity by separating responsibilities into dedicated modules—for example: metrics collection, EV charging control, energy brokerage, solar forecasting, Tibber integration, Domoticz interfacing—and by using clear asynchronous boundaries (MQTT topics, event queues, worker threads or tasks).

Contributors and automated agents should assume the codebase favors:
- explicit asynchronous concurrency (threads and asyncio loops) and small, well‑scoped modules rather than monolithic services;
- defensive error handling around IO and network calls (MQTT, REST APIs, serial buses);
- proactive resource management to avoid memory or CPU exhaustion on Cerbo GX / Raspberry Pi hardware;
- minimal, well‑documented dependencies to keep deployments small and robust.

When modifying architecture or adding features, preserve these qualities: keep modules focused, prefer push‑button diagnosability (clear logging and metrics), and avoid solutions that require heavy runtime resources unless gated by a config toggle and justified in `README.md`.

---

## Repository‑wide guidelines
- Target Python **3.10** for compatibility with Victron Cerbo GX and Raspberry Pi; avoid language/runtime features that require newer interpreters.
- Keep dependencies lean.  Prefer the standard library; document and justify any new third‑party package in `README.md`, and update `requirements.txt` and `requirements‑dev.txt`.
- Preserve existing logging approach (the project relies on `print(..., flush=True)` or the built‑in `logging` module for critical runtime logs to keep logs ordered correctly). Helper log wrappers are OK if they do not hide boot logs.
- When modifying long‑running daemons or background tasks, ensure signal handling is clean and that network or MQTT connections are gracefully closed. Mirror patterns in `lib/config_change_handler.py` and `lib/domoticz_updater.py`.
- Update `README.md`, `.env.example` (inline comments), `secrets-example`, and `lib/constants.py` for any new tunables, services, or environment variables.  Prefer tables/bulleted lists for discoverability.
- There is no web UI in this repository; the dashboard lives in a separate repository.  If you add optional HTTP endpoints or gRPC services here, document API changes in `README.md` and keep the interface stable.

---

## Design & Implementation Rules (practical)
- Respect module boundaries. If you need to add functionality that crosses components, introduce small, explicit APIs rather than inlining cross‑cutting logic.
- Avoid blocking the main event loops (e.g. MQTT consumer threads or async loops).  Offload CPU‑ or IO‑heavy tasks to background threads/processes and communicate via queues or events.
- Use idempotent operations for storage and external effects where possible; prefer write‑then‑rename semantics for database or file writes.
- When adding subprocess usage (e.g. invoking system commands), include robust respawn/cleanup logic and failure telemetry.
- Be conservative with memory and CPU: assume Cerbo GX/Raspberry Pi class constraints by default.  Add performance or feature gates for heavier processing (e.g., ML‑based forecasting) and make them configurable.

---

## Things to always think about when editing code
- Understand the module’s runtime context (daemon/service, scheduler, MQTT consumer) before proposing or committing changes.
- Prioritize safety: prefer non‑breaking, reversible edits, and create tests for behavior changes.
- Preserve resource constraints and signal handling semantics.  Do **not** introduce background threads or long‑running tasks without an opt‑in config.
- When proposing refactors, produce small PRs with clear tests and a short risk assessment (what can go wrong, how to rollback).
- Prefer to add feature flags/config knobs for expensive options and document default behavior.

---

## Python code style
- Module constants: `UPPER_SNAKE_CASE`.  Functions/variables: `snake_case`.
- Type hints welcome when consistent; avoid mixing hinted/unhinted signatures without reason.
- Avoid broad `except:` blocks. Always log exceptions and scope `try/except` narrowly.
- Add docstrings where behavior is non‑obvious (energy calculations, scheduler algorithms, external protocol semantics).

---

## Shell scripts and systemd units
- Start scripts with `#!/usr/bin/env bash` and `set -euo pipefail`.
- Keep scripts POSIX‑friendly; avoid GNU‑only flags unless already used in the repo. Document exceptions.
- When editing systemd unit files, keep matching `.service`/`.timer` pairs and document variables in `.env.example`.

---

## Testing and validation
- Run `export DEV=1; pytest -q` or `python3 -m unittest discover` before submitting changes.  If making a targeted change, run the specific module tests as well as the full suite.
- If changes affect installation/uninstallation, add or update tests accordingly.
- If touching shell scripts/systemd, prefer a dry run on a Cerbo GX or Raspberry Pi or document why that isn’t feasible.
- When adding features, add tests in `tests/` or appropriate `lib/.../tests` folder and document their intent in `README.md` or a `tests/README.md`.
- New dependencies must be compatible with Ubuntu 24.04 LTS and Python 3.10 (the target environment for container builds).

---

## Documentation
- Keep `README.md`, `.env.example`, `.secrets-example`, and `lib/constants.py` synchronized with code.  Add release notes for behavior‑changing edits.
- Document new configuration options inline in the examples and summarize them in `README.md`.
- When changing runtime behavior, update any architecture diagrams and the “Features”/“Operations” sections in the README.

---

## Contribution workflow & PR guidance
- Small, focused PRs.  Large architectural changes should be prefaced with an issue describing the proposed design, trade‑offs, and rollback plan.
- PRs must include:
  - a short description of the change,
  - a risk statement (how this impacts the energy automation runtime),
  - test steps (local dev + real hardware when necessary),
  - updated docs for configs or runtime behavior.
- For high‑risk changes (MQTT event loops, EV charging control, energy broker), require at least one approval from a core maintainer and a successful run of integration tests (if available).

---

## Appendix: Suggested developer mindset
- Be conservative: prefer clarity over cleverness when working on concurrency or IO code.
- Test in a hardware‑like environment before shipping (Cerbo GX, Raspberry Pi, or a simulated environment).  [If you are an AI/LLM powered agent, this does not apply to you!]
- When adding heuristics (e.g. forecasting thresholds, energy optimization strategies), provide sane defaults, expose tunables, and document expected behavior and failure modes.
- When in doubt, open an issue describing the problem and a proposed minimal fix or experiment before changing production code.  [If you are an AI/LLM powered agent, pause work and consult with the human who set you to task.]

---
