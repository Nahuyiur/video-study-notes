# StudyNote schema v2

Native Codex supplies understanding after actually reading reserved packs; the
standalone API engine submits actual packs and generates the same note structure.
Saving validates identity, ranges, reading provenance and safe assets; valid
references do not prove semantic support. Check important explanations against the material.
No saving/export command fetches video, transcribes audio or calls a model.

## Save once, deliver many times

```text
python scripts/video_notes.py note --run RUN --input study-note.json
python scripts/video_notes.py export --run RUN --format html,md
python scripts/video_notes.py export --run RUN --format md --text-only
python scripts/video_notes.py export --run RUN --format html --revision 1 --out OUTPUT.html
```

The standalone engine validates its draft before finalizing and saves it through
this same contract; it does not accept a model-supplied snapshot or usage record.

Finalize actual reading and usage before saving. Input uses this content structure:

```json
{
  "schema_version": 2,
  "title": "Course note",
  "takeaway": "The important explanation from the materials.",
  "takeaway_evidence_refs": ["segment:s0001"],
  "sections": [{
    "id": "mechanism",
    "title": "How it works",
    "evidence_refs": ["segment:s0001"],
    "blocks": [{
      "type": "paragraph",
      "text": "A supported explanation.",
      "attribution": "speaker",
      "evidence_refs": ["segment:s0001", "frame:f0001"]
    }]
  }],
  "timeline": [{
    "start": 100, "end": 140,
    "title": "Mechanism", "text": "What happens in this interval.",
    "section_id": "mechanism",
    "evidence_refs": ["segment:s0001"]
  }],
  "figures": [{
    "frame_id": "f0001", "title": "Mechanism diagram",
    "caption": "What this actually read frame shows.",
    "evidence_refs": ["frame:f0001"]
  }],
  "caveats": ["Visuals are sampled, not exhaustive."],
  "sources": [{"label": "Original reference", "url": "https://example.org/paper"}]
}
```

Use the segment IDs shown in the actual pack, and frame IDs the agent declared
read. Old runs without segment IDs receive stable `s0001`, `s0002`, … in snapshot
order. A declared old overview text pack implies its selected transcript was read;
a detail pack without exact IDs does not imply all nearby transcript was read.
Unavailable/unread IDs and times outside the processed original interval are
rejected. Optional `subtitle` and `usage_note` contain plain text.

Every block has an explicit `evidence_refs` list; `[]` means no exact grounding is
asserted. Attribution is `speaker` (default), `agent`, or `uncertain`. It is not a
confidence score. Sections/timeline/takeaway may also cite evidence IDs.

| Block type | Content fields |
| --- | --- |
| `paragraph` | `text` |
| `list` | `items` as text strings, optional boolean `ordered` |
| `code` | `text`, optional plain `language` identifier |
| `formula` | `text` as equation source |
| `table` | `headers` as text strings, `rows` as equal-width text lists |
| `image` | `frame_id`, `title`, `caption`; its frame ref must appear explicitly |

HTML escapes all content and embeds JPEGs, CSS and readable formula source. It
needs no scripts, external equation service, fonts or network. Markdown uses
fenced code/math source and escaped ordinary content; rich exports include a
relative `note-vNNN-assets/` folder. Move that folder together with its Markdown.
Text-only Markdown omits pictures but preserves captions, timestamps and refs.

## Immutable evidence and revisions

`notes/note-v001.json` stores content plus `note_id`, `revision`, `content_hash`,
and `snapshot`. The snapshot freezes:

- `run`: requested/processed/remaining ranges, duration, status and source type.
- `source`: public platform/media/part identity, title and canonical URL.
- `usage`: sanitized observed counters/costs, unknowns still `null`.
- `segments`: IDs, original seconds, text and `claimed_read` flags.
- `frames`: declared read IDs/times/kinds and content hashes, with run-relative
  `notes/assets/<sha256>.jpg` copies.
- `reading`: pack IDs, exact frame/segment IDs, declared status and exposure counts.

API-accepted packs also preserve submission/response provenance and call identity,
distinct from native reading declarations. This records the material submitted,
not proof of visual comprehension or semantic correctness. The engine requires
grounded takeaway/body content and actual frame references; it does not add
references to unsupported generated explanations to make validation pass.

It excludes source media paths, signed stream URLs, cookies, session files and raw
provider responses. Images and snapshot/content hashes are checked on load.
Repeated identical input with the same frozen evidence reuses the current
revision. Changed content or run evidence appends a new revision without
rewriting old notes, source records or historical usage. Serial saving uses a
local lock. `load_note(run_dir, revision=None)` reads only notes and verified
snapshot assets, so later source/run changes cannot alter an old export.

Exports go to `exports/note-vNNN.html`, `.md` and a local receipt by default.
Their displayed counters cover the frozen acquisition/reading run, including
standalone API synthesis performed before finish. Rendering
makes zero model calls; subsequent agent messages or publishing work may consume
additional native tokens and are not folded into old video usage.

## Explicit historical import

```text
python scripts/video_notes.py note --run RUN --input RUN/summary.json --legacy-summary
```

This copies legacy paragraphs/bullets, timeline and actual-read figures into a new
StudyNote revision. It does not modify `summary.json`, `run.json` or `usage.json`.
Paragraphs have no precise citation mapping; timeline refs are inferred by overlap
with read segments and this limitation is recorded/displayed. Figure refs remain
exact declared-read frame references. Import is not new understanding or evidence
that every old explanation was checked.
