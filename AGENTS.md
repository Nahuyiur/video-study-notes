# Maintaining Video Study Notes

This repository is the source for the `video-study-notes` Codex skill. Current acquisition supports Bilibili and local videos; YouTube and scene/slide-change sampling remain planned.

- Keep script paths relative to the skill root. Preserve `SKILL.md`, `agents/openai.yaml`, workflow references and the actual CLI behavior together.
- Preserve budget and completion boundaries. Acquiring frames is not model reading; transcript coverage is not exhaustive visual coverage. Unknown counters/costs stay null.
- Use temporary or synthetic fixtures for unit tests. Do not commit user runs, downloaded audio/video, transcripts, signed media URLs, cookies, credentials, session logs or private filesystem paths.
- Keep HTML a generated view of `summary.json`, with run scope and usage imported from real artifacts. Preserve escaping and frame-read checks; use no paid model or network calls in the renderer.
- Bundled upstream files are pinned and licensed. Preserve notices and update `references/upstream.json` hashes when intentionally changing the vendor revision. ASR dependency changes require a real-audio check, not only unit tests.
- Prefer focused changes. Do not add source adapters, broad dependency updates or paid APIs while fixing presentation or a small extraction issue.
- Changes here do not automatically update an existing installed skill. Sync the installation only when the user requests it, then validate the installed copy.

Verification:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts
git diff --check
```

If available on the current Codex host, also run its skill-creator validator and skill-router audit. Verify source-specific access separately when changing acquisition. Explain what was actually run; do not present fixture/CI success as live-video success.
