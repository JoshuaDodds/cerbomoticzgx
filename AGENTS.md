# AGENTS

> Quick purpose: this file captures repository-wide rules, developer expectations, and design context so humans and machine agents make consistent, low-risk changes for a home energy management system controlling critical power infrastructure.

---

## For Automated AI/LLM Agents
IMPORTANT:
You have full ADMIN level privileges to the Jira project this task is linked to. For details on how to use this API refer to the documentation here:
    https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/#about
You will probably mostly be interacting with:
    https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-group-issues

Your environment may have Jira credentials pre-configured. If not, and you need to access Jira, the necessary credentials (`JIRA_EMAIL`, `JIRA_PAT`) are located in the `.env` file. Note that direct access to this file might be restricted by security policies. In that case, your human operator will need to provide the credentials to you.

Naming discipline when tickets are referenced:
- Detect Jira keys in the format `<PROJECT>-<int>` using regex `\b[A-Z][A-Z0-9]+-\d+\b` across instructions, attachments, or assets.
- If a key is present, immediately transition the Jira issue from **To Do** to **In Progress**, comment “Agent started work on this ticket.”, assign the ticket to yourself, and log setup time.
- If no key is present, create a new issue for this request using the API and include Title and description, and set the sprint field to the currently active sprint so it shows on the board. Set any other appropriate fields on the issue that enrich transparency.
- When a key exists, always create the working branch and PR title prefixed with that identifier (e.g., `CGX-1234-description`); recommended branch format: `CGX-<num>-<short-slug>`.
- When a key exists always use the Jira key in the PR title and your own Task name in the CODEX web ui task list.  
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
- Ensure the Git author email matches a Jira user for smart-commit linkage.

Ticket lifecycle expectations (Board: To Do → In Progress → In Review):
1. **Start/pickup** – transition to **In Progress** and assign yourself to this ticket using the Jira API and env credentials available to you, add the startup comment, start a timer so you can log total time spent working on this task, Update the ticket title and description to be a well written description a developer can understand, estimate the complexity and update the ticket story points, then continue with your implementation task.
2. **During work** – keep the ticket **In Progress**, post incremental commits with `<PROJECT>-<int>` keys and `#comment` tags, and perform Jira API updates to add time tracking information between commits and comments if applicable..
3. **Complete** – final commit transitions to **In Review** using the smart commit format. Post a Jira comment summarizing work, update the jira time tracking field with actually mins for your task, current status (**In Review**), and links back to this CODEX task run and PR/commit diff, and finally include testing criteria and testing steps that a human can do to verify functionalilty. Do **not** move to Done.
4. **Failures** – if transitions fail, comment the error, retry with backoff (5 attempts), and proceed with manual follow-up instructions.
5. **Fallback** – if Smart Commit automations are unavailable (permissions/workflow), explicitly post Jira comments, worklogs, and transitions using the Jira API.

Jira API usage requires `JIRA_EMAIL` and `JIRA_PAT` is available to you and they are preconfigured for codex agents in their work environments already; derive the base URL as `https://mfisbv.atlassian.net` each run instead of reading a `JIRA_BASE_URL` variable. Read tokens from the environment only, redact PAT values in logs, and scope credentials minimally (issue read/write, worklog, transitions). Resolve transition IDs dynamically by name (“In Progress”, “In Review”), and verify capabilities (`/myself`, read issue, list transitions, add comment/worklog) before first use. Remember that Jira ticket keys already embed the project prefix (`ABC-123` ⇒ project key `ABC`, `CGX-456` ⇒ project key `TR`). Perform a self-check at startup to confirm transitions map correctly and permissions allow commenting/worklogging. On closeout, ensure total time logged and final status are reported in Jira comments.
You are expected to strictly adhere to Jira API usage guidelines and not make any changes to the Jira UI. You are expected to send Jira API regularly to keep your work tracked.
The ENV vars mentioned above are already configured for you. If you do not have these vars available in your environment you might have them in a .env file at the project root so check there for it. 

### Time tracking discipline (enforced for all agents)
- Start a real timer (e.g., `time.perf_counter()` in a scratch Python shell or a physical stopwatch) **before** you run any repo command. Record the start timestamp in your scratchpad so you can recompute elapsed wall time at any moment.
- Every time you make a commit, compute the actual minutes spent since the previous commit (or task start) and copy that exact rounded value into the `#time` smart-commit tag. Never reuse values from earlier worklogs or guess; always recompute from the timer.
- Pause the timer and subtract any time you are idle or waiting on unrelated tasks. The value you log must reflect focused, hands-on work on this ticket only.
- Maintain a small running log in your scratchpad (e.g., `notes/time_tracking.txt` in your workspace or your terminal notes) that lists timestamp checkpoints and the cumulative elapsed minutes. Use it to double-check that Jira worklogs and `#time` values reconcile before you finish.
- Before your final commit, add Jira worklog entries that sum to the same total minutes as your scratch timer. If you discover a mismatch, update the worklog to the correct value and mention the adjustment in your closing Jira comment.
- Prior to calling `make_pr`, re-read the time tracking log and confirm the total duration you plan to report matches the wall-clock elapsed time for the session (rounded to the nearest minute). If there is a discrepancy, correct the worklog and amend the commit message before proceeding.

Before final commit with smart commit messages pushing:
1. Run tests (export DEV=1 && pytest -q). All tests must pass.
2. Empty Commit (Fallback)
If no files are changed and no doc is needed:
3. Push Workflow
At the end of the run, the orchestration system should reattach `origin` with credentials and push the `work` branch.  
    git commit --allow-empty -m "CGX-52 Finalization #comment trigger pipeline #time 2m #transition In Review"

Pull request hygiene:
- PR titles must begin with the Jira key (e.g., `CGX-123: Fix …`, `AUDIO-45: Update mixer`).
- Use the template sections: **What / Why**, **How (high-level)**, **Risk / Rollback**, **Human Testing Criteria**, **Links** (Jira issue, task run, preview URL).
- Keep commits small and logically grouped; document test coverage changes in `#comment`.

If you are reviewing another agent's PR:
- Always leave a short review comment summarizing what you did, test results, and risk assessment.
- If all tests pass and the PR is safe to merge, submit a formal GitHub PR review with “Approve” status (not just a 👍 reaction).
- If there are issues or risks, submit a “Comment” or “Request changes” review instead, explaining why.
- Reactions (👍) alone are not sufficient; every PR must have a visible comment and, when ready, a formal approval.

---

## Design & Implementation Rules (practical)
- Respect module boundaries. If you need to add functionality that crosses components, introduce small, explicit APIs rather than inlining cross-cutting logic.
- Avoid blocking the main capture loop. Offload CPU or IO heavy tasks to background threads/processes and communicate via bounded queues.
- Use idempotent operations for storage and external effects where possible; prefer write-then-rename semantics for recorded files.
- When adding subprocess usage (eg. `ffmpeg`), include robust respawn/cleanup logic and failure telemetry.
- Be conservative with memory and CPU: assume Pi Zero class constraints by default. Add performance or feature gates for heavier processing (e.g., RNNoise/noisereduce).

---

## Things to always think about when editing code
**CRITICAL SYSTEM WARNING:** This project controls a 16KW 3-phase electrical system. System uptime is critical, and software-induced power loss is unacceptable. All changes must be made with extreme care, prioritizing stability and reliability. 
---

## Python code style
- Module constants: `UPPER_SNAKE_CASE`. Functions/variables: `snake_case`.
- Type hints welcome when consistent; avoid mixing hinted/unhinted signatures without reason.
- Avoid broad `except:` blocks. Always log exceptions and scope `try/except` narrowly.
- Add docstrings where behavior is non-obvious (audio math, buffer semantics, filesystem guarantees).

---

## Shell scripts and systemd units
- Start scripts with `#!/usr/bin/env bash` and `set -euo pipefail`.
- Keep scripts POSIX-friendly; avoid GNU-only flags unless already used in the repo. Document exceptions.
- When editing units, keep matching `.service`/`.timer` pairs and document variables in `config.yaml`.

---

## Testing and validation
- Run `export DEV=1; pytest -q` before submitting changes. If making a targeted change, run the specific module tests as well as the full suite.
- If touching shell scripts/systemd, prefer a dry run on a Raspberry Pi or document why that isn’t feasible.
- When adding features, add tests in `tests/` and document their intent in `README.md` and `tests/README.md`.  The order in which the tests run will be enforced by a numbered naming structure to force 
a certain order why pytest iterates the files in the directory.
- New dependencies must be compatible with Ubuntu 24.04 LTS and Python 3.10+.

---

## Documentation
- Keep `README.md` and all .env and config files synchronized with code. Add release notes for behavior-changing edits.
- Document new configuration options inline when adding them and summarize them in `README.md`.
- When changing runtime behavior, update the architecture diagram and the “Operations” section in the README.

---

## Contribution workflow & PR guidance
- Small, focused PRs. Large architectural changes should be prefaced with an issue describing the proposed design, tradeoffs, and rollback plan.
- PRs must include:
  - a short description of the change,
  - a risk statement (how this impacts the recorder runtime),
  - test steps (.local dev + Raspberry Pi when necessary),
  - updated docs for configs or runtime behavior.
- For high-risk changes (capture loop, encoding pipeline, HLS stream lifecycle), require at least one approval from a core maintainer and a successful run of the end-to-end audio test (if available).

---

## Appendix: Suggested developer mindset
- Be conservative: prefer clarity over cleverness when working on concurrency or IO code.
- Test in a hardware-like environment before shipping (Pi Zero 2 W or similar). [If you are an AI/LLM powered agent, this does not apply to you!]
- When adding heuristics (adaptive thresholding, VAD tuning), provide sane defaults, expose tunables, and document expected behavior and failure modes.
- When in doubt, open an issue describing the problem and a proposed minimal fix or experiment before changing production code. [If you are an AI/LLM powered agent, pause work and consult with the human who set you to task.]

---

End of file.
