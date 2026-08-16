# Assistant improvement release

- [x] Use `gpt-transcribe` as the only OpenAI transcription model.
- [x] Restore transcription for Telegram voice, video notes, and video files when FFmpeg is absent from `PATH`.
- [x] Keep extracted audio within OpenAI's 25 MB upload limit.
- [x] Give users a clear transcription failure instead of processing an error string as text.
- [x] Add useful transcription context (language hints and optional chat glossary).
- [x] Add controllable context retention, group privacy mode, and explicit user memory.
- [x] Preserve web-search source links in answers and apply simple usage limits.
- [x] Verify, review, document, commit, and push the finished release.
