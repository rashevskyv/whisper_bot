# Assistant improvement release plan

## Phase 1 — reliable media transcription (Completed)

1. [x] Make `gpt-transcribe` the fixed OpenAI transcription model; remove the user-facing transcription-model selector.
2. [x] Pass its `languages` hint correctly and allow a short transcription prompt plus optional keywords from chat settings.
3. [x] Resolve FFmpeg from the installed bundled binary when it is not on `PATH`; convert video notes and video files to mono, speech-appropriate audio.
4. [x] Check the generated audio size against OpenAI's 25 MB limit and retry once with a lower bitrate; report a concise user-facing failure if still oversized.
5. [x] Raise and handle transcription failures so they never enter the text-beautification path.

## Phase 2 — useful, bounded context (Completed)

1. [x] Add a group context mode (shared or per-user) and a retention period with cleanup.
2. [x] Add explicit `/remember`, `/forget`, and `/memories` behavior backed by the existing SQLite database.
3. [x] Put saved facts into the system context with a small cap; do not add a vector database.

## Phase 3 — answer quality and operational limits (Completed)

1. [x] Keep web-search links as sources in the final answer.
2. [x] Add simple per-user daily media/transcription limits using existing SQLite state.
3. [x] Document settings and run the relevant checks.

## Verification and delivery

1. [x] Inspect every changed caller and review the diff.
2. [x] Run the smallest relevant Python checks and available test suite.
3. There is no existing application-version convention, so do not invent one.
4. [x] Update this plan and task checklist, commit focused changes, push, and confirm a clean tree.
