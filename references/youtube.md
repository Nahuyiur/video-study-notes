# YouTube acquisition

The adapter uses anonymous yt-dlp extraction and the same downstream transcript,
ASR, sampling, reading and note pipeline as other sources. Its public functions
are `resolve(video, language=None)` and `media(source, kind, height=720)`.

## Optional dependencies

- Fallback pins: `yt-dlp==2026.8.19`, `yt-dlp-ejs==0.8.0`. The EJS version matches
  the yt-dlp release's declared dependency, verified on 2026-10-08.
- The adapter first accepts an existing yt-dlp release at least as recent as the
  fallback pin. Otherwise it uses `uv run --isolated --with-requirements` and the
  repository's `scripts/requirements-youtube.txt`. It does not install globally.
- A supported existing JavaScript runtime is required. This adapter checks Deno
  >= 2.3, then Node >= 22 by executing `--version`, and explicitly enables the
  detected runtime. It does not install a runtime or fetch remote solver code.
- Official standalone yt-dlp executables bundle EJS. An existing Python/package
  installation must include the matching `yt-dlp-ejs` dependency. Existing
  installations may differ from the fallback pins. The source record includes
  the selected yt-dlp version and detected runtime name/version separately from
  fallback pins. An existing standalone/package EJS version cannot be inferred
  from the CLI and remains unknown; the isolated matched pin is recorded when
  that environment successfully performs extraction.
- FFmpeg/ffprobe requirements remain owned by the shared materials stage.
  Bilibili and local-file paths do not load YouTube dependencies.

See [yt-dlp dependencies](https://github.com/yt-dlp/yt-dlp#dependencies),
[official EJS setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS), and the
[pinned release on PyPI](https://pypi.org/project/yt-dlp/2026.8.19/).

## Scope and captions

Watch, short-link, Shorts, embed and individual `/live/` URLs are normalized to
one canonical watch URL. Tracking parameters and playlist context are discarded.
Playlist-only URLs, channels, active/upcoming streams and recordings still being
processed are rejected; finished archived livestreams are accepted.

Metadata is normalized to `SourceRecord`. Provider JSON, signed subtitle/media
URLs, thumbnails and headers do not escape into persistent source metadata.
Caption preference is a useful human track, then an automatic track, with JSON3,
VTT and SRT supported. An explicit language restricts the candidates to that
language and regional variants; `auto` uses the video language followed by
English and Chinese. At most three distinct language/kind tracks are attempted.
Automatic rolling text is deduplicated only at overlapping/adjacent times; later
repeated speech remains present. Timestamps stay in original video seconds and
are clamped to its duration.

When captions are absent, denied, malformed or outside the requested language,
metadata is retained with empty segments and a missing reason. The shared ASR
stage can then process the selected interval within its existing budget. This
adapter does not trigger transcription or summarize content by itself.

## Media and recovery

Media URLs are refreshed immediately when audio or video is requested. Direct
HTTPS streams are preferred. Video selects a resolution within the requested
height when available; audio prefers an audio-only stream with the lowest
available bitrate. A missing requested resolution may yield the smallest
available larger stream. FFmpeg decodes the returned URL; extracting the URL
does not prove successful decoding.

Only HTTPS YouTube/googlevideo hosts are accepted for media. Caption redirects
must remain on HTTPS YouTube hosts. Only a sanitized user agent is forwarded to
FFmpeg; cookies, authorization and arbitrary provider headers are excluded.
yt-dlp user configuration, plugin directories, persistent cache, remote EJS
components and playlist expansion are disabled. Browser cookies, login profiles,
netrc and account secrets are not read. The host's existing HTTPS/ALL proxy route
is also passed explicitly to FFmpeg as an ephemeral HTTP tunnel option, respecting
NO_PROXY. This avoids Python extraction succeeding through a configured proxy
while FFmpeg times out on a direct CDN connection. No proxy configuration is
created or changed; SOCKS/HTTPS proxy types unsupported by this FFmpeg path cause
an actionable error. Proxy values are never persisted in source records.
See [FFmpeg HTTP protocol options](https://ffmpeg.org/ffmpeg-protocols.html#http).

Metadata has a 120-second process deadline, 15-second socket timeout and one
extractor/network retry. Each caption has a 15-second request timeout, at most
one retry, an 8 MiB response limit and a 20,000-segment limit. Metadata JSON is
limited to 20 MiB. The optional isolated dependency bootstrap shares the process
deadline; an initial slow package download may require one retry. Captions are
bounded as a full track before the shared stage selects the requested range.
Error messages are reduced to stable reasons and never echo raw provider output
or signed URLs.

Age, region, copyright, account and bot challenges may prevent anonymous access.
The adapter reports a limitation and accepts local materials; it does not discover
cookies or bypass account checks automatically. External site changes may require
an intentional pin update and new live verification.

## Verification performed

Fixture tests cover normalized metadata, language preference, manual-to-auto
fallback, missing/denied captions, JSON3/VTT/SRT timing, rolling deduplication,
playlists/live rejection, stream choice, dependency flags, retry limits, caption
size limits and error/header privacy. They require no network or optional tools.

On 2026-10-08, anonymous extraction of
[Me at the zoo](https://www.youtube.com/watch?v=jNQXAC9IVRw) returned 19 seconds of
metadata and six English manual caption segments in 3.57 seconds. Separate audio
and video stream extraction also succeeded using the pinned isolated packages
and an existing supported Node runtime. A repeated extraction verified source
provenance reporting `yt_dlp_version=2026.8.19`, `ejs_version=0.8.0`, and Node
`24.7.0` in the Python process environment.

Follow-up FFmpeg verification found direct CDN connections timing out while
Python used the host's existing HTTP proxy. Explicitly preserving the same proxy
route decoded one real video frame at 5 seconds (320 pixels wide, 13,257-byte
JPEG) and a two-second mono 16 kHz audio segment (64,078-byte WAV). These files
were temporary and not committed. This verifies media decoding for this public
video; it does not establish ASR accuracy, model reading or every YouTube access
condition.

The integrated CLI subsequently extracted frames at 3, 10 and 16 seconds,
created one bounded reading pack, and the current Codex actually read its
six subtitle segments and contact sheet. A frozen note exported to both HTML
and Markdown. A separate local fixture removed the otherwise available captions
to exercise the shared fallback: real YouTube audio was acquired and transcribed
with the pinned local `small` English ASR runtime over 0–19 seconds. That fixture
was finished as `extraction_only`; it is not evidence that this video lacks
captions. Native token totals and dollar cost were unavailable and stayed null.
