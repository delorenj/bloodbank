# BMAD Output Policy — Bloodbank

BMAD artifacts remain the project source of truth.

Authoritative artifacts live in Markdown/workflow/log files such as:

- `_bmad-output/stories/bloodbank-story-map.md`
- `_bmad-output/autopilot/logs/*.md`
- BMAD workflow outputs created by future planning, implementation, review, and verification runs
- Source docs and code committed in the repo

`_bmad-output/workspace.html` is a generated, human-facing rendered view. It exists so Jarad can read status quickly without opening scattered Markdown files. Do not treat embedded HTML/JSON as authoritative planning state. Regenerate or refresh it from the canonical BMAD artifacts.

Rule of thumb: **edit BMAD artifacts first, render the cockpit second.**
