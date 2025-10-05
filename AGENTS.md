# CerbomoticzGx Contribution Notes

## Scope
These guidelines apply to the entire repository.

## Conventions
- Prefer descriptive documentation updates when altering behaviour or architecture diagrams.
- Keep README diagrams in Mermaid format for easy maintenance.
- When adding scripts or CLIs, provide usage examples in the `docs/` directory.
- Prioritise test execution via `export DEV=0 && pytest -q` before final commits.

## Documentation
- Ensure newly added sections include context for integration with Victron, Tesla, and home automation modules when relevant.
- Diagrams should focus on data flow between major subsystems (e.g., energy sources, controllers, automations).

## Code Style
- Follow existing Python formatting and avoid introducing additional linting tools without discussion.

