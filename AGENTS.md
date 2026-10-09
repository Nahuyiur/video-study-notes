# Maintaining Video Study Notes

This repository is the source for the `video-study-notes` Codex skill and its standalone Python/CLI engine. Acquisition supports Bilibili, individual YouTube/RedNote videos and local files. Shared understanding produces frozen StudyNote revisions for HTML, Markdown and optional Feishu delivery. See `references/architecture.md` for module ownership.

- Keep script paths relative to the skill root. Preserve `SKILL.md`, `agents/openai.yaml`, workflow references and the actual CLI behavior together.
- Preserve budget and completion boundaries. Acquiring frames is not model reading; transcript coverage is not exhaustive visual coverage. Unknown counters/costs stay null.
- Native Codex reading remains the default skill mode. Standalone API execution needs an explicitly selected endpoint/model and credential reference; never auto-switch a native request to a paid provider. Reserve all attempts before dispatch, persist responses before parsing, and block unknown sends from automatic retries. Include API synthesis before freezing usage.
- Use temporary or synthetic fixtures for unit tests. Do not commit user runs, downloaded audio/video, transcripts, signed media URLs, cookies, credentials, session logs or private filesystem paths.
- Keep all delivery views generated from validated StudyNote snapshots, with run scope and usage imported from real artifacts. Preserve escaping and frame-read checks; use no paid model or acquisition calls in local renderers. Feishu writes need authorized intent, durable receipts and online content/media verification; never blindly repeat an ambiguous mutation.
- Bundled upstream files are pinned and licensed. Preserve notices and update `references/upstream.json` hashes when intentionally changing the vendor revision. ASR dependency changes require a real-audio check, not only unit tests.
- Prefer focused changes. Do not add source adapters, broad dependency updates or paid APIs while fixing presentation or a small extraction issue.
- Changes here do not automatically update an existing installed skill. Sync the installation only when the user requests it, then validate the installed copy.

Verification:

```bash
uv run --python 3.12 --with pillow python -m unittest discover -s tests -v
python3 -m compileall -q scripts video_notes
git diff --check
```

If available on the current Codex host, also run its skill-creator validator and skill-router audit. Verify source-specific access separately when changing acquisition. Explain what was actually run; do not present fixture/CI success as live-video success.

- RedNote access URLs and signed media remain in process-scoped memory. Keep source/run/note URLs canonical without query strings. External skill use is operator-selected and read-only; stop on login/captcha, never migrate profiles or ship account state. A description/cover is never a transcript or video-frame read.
