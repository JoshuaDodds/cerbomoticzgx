# AGENTS

> Quick purpose: this file captures repository-wide rules, developer expectations, and design context so humans and machine agents make consistent, low-risk changes for a home energy management system controlling critical power infrastructure.

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
NEVER remove a secret from .secrets - This is imperative. You may add new credentials but may not remove anything there already.
---

## Python code style
- Module constants: `UPPER_SNAKE_CASE`. Functions/variables: `snake_case`.
- Type hints welcome when consistent; avoid mixing hinted/unhinted signatures without reason.
- Avoid broad `except:` blocks. Always log exceptions and scope `try/except` narrowly.
- Add docstrings where behavior is non-obvious (audio math, buffer semantics, filesystem guarantees).

---

## Testing and validation
- Run `export DEV=1; pytest -q` before submitting changes. If making a targeted change, run the specific module tests as well as the full suite.
- If touching shell scripts/systemd, prefer a dry run on a Raspberry Pi or document why that isn’t feasible.
- When adding features, add tests in `tests/`.  The order in which the tests run will be enforced by a numbered naming structure to force 
a certain order of how pytest iterates the files in the directory.
- New dependencies must be compatible with Ubuntu 24.04 LTS and Python 3.10+.

---

## Documentation
- Keep `README.md` and all .env and config files synchronized with code. Add release notes for behavior-changing edits.
- Document new configuration options inline when adding them and summarize them in `README.md`.

---

## Appendix: Suggested developer mindset
- Be conservative: prefer clarity over cleverness when working on concurrency or IO code.
- When in doubt, pause work and consult with the human who set you to task.
