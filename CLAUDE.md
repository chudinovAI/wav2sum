# wav2sum

A local Krisp + Wispr Flow for macOS: call recording with a transcript and a summary, and voice dictation into any app, in Russian and English. The full goal is in SPEC.md.

## Commands

- Test: `uv run pytest --cov`
- Lint: `uv run ruff check && uv run ruff format --check`
- Typecheck Swift (macOS only): `swiftc -typecheck wav2sum/native/app/*.swift && swiftc -typecheck wav2sum/native/capture.swift && swiftc -typecheck wav2sum/native/app/Logo.swift wav2sum/native/icon/main.swift`
- Run: `uv run wav2sum app` (menu bar app and daemon), `uv run wav2sum tui`, `uv run wav2sum meeting.mp3`
- Dictation latency from the history (read-only): `uv run python -m wav2sum.latency`
- Dictation quality from the history (read-only): `uv run python -m wav2sum.quality`
- Numbers and names in a fresh summary of the last 3 calls that their transcripts don't have (read-only): `uv run python -m wav2sum.factcheck [call folder ...]`; run it after a change to the summary prompt or model
- Dictation WER on the recorded phrase set (read-only): `uv run python -m wav2sum.evalset`; record it with `uv run python -m wav2sum.evalset record quiet` (also `airpods`, `noise`, `whisper`)
- Convert the GigaAM encoder to Core ML (macOS only): `uv run --group coreml python -m wav2sum.coreml`

CI (`.github/workflows/ci.yml`) runs ruff on Linux and the tests (with coverage, at least 80%) and the Swift typecheck on `macos-15`. The tests also pass on Linux.

## Project memory

- SPEC.md has the goal, the criteria for "done", the list of things that we do not do and the current architecture.
- PLAN.md has the steps for the current task. Each step has a check.
- Tasks live in Linear: project [wav2sum](https://linear.app/wasted-potentials/project/wav2sum-fa518c5db805), team "chudinov projects" (`CHU`). Name branches and PRs after the issue, for example `CHU-104`.
- When you open a PR, move the Linear issue to In Review and attach the PR link. After the merge, move it to Done.
- At the start of a session, read this file, SPEC.md and PLAN.md.
- Do not keep project knowledge only in the chat. If the next session must know a fact, tell me and write a proposed line for this file.

## Rules

1. Think before you write code.
   - Write your assumptions before you start.
   - If the task has more than one meaning, show all meanings. Do not select one without a question.
   - If a simpler solution is possible, tell me.
   - If a part of the task is not clear, stop and ask.
2. Keep the code simple.
   - Write the minimum code that does the task.
   - Do not add features that I did not ask for.
   - Do not make an abstraction for one call.
   - Do not add settings that I did not ask for.
   - Do not add error handling for errors that cannot occur.
   - If fewer lines can do the task, write the code again.
3. Change only the necessary lines.
   - Do not change code, comments or formatting that are not related to the task.
   - Do not refactor code that operates correctly.
   - Use the current style of the project.
   - If you find dead code, tell me. Do not remove it.
   - Remove only the code that your changes made unused.
   - Make sure that each changed line is necessary for the task.
4. Work to a goal that you can test.
   - Before you write code, write a test for the result.
   - For a task with many steps, write a plan. For each step, write how to test it.
   - Do the steps again until all tests pass.

## Work on a step

1. Do one step from PLAN.md. Do not start the next step.
2. Write the test for the step first. Then write the code.
3. Run the tests and the linter. If they fail, change the code and run them again.
4. Do not change a test to make it pass. If a test is incorrect, tell me why and stop.
5. Do not commit or push. I read the diff and commit.
6. At the end, write a short report:
   - what you changed and why;
   - what you did not do or did not test;
   - each changed line that is not necessary for the step (the list must be empty).

## Do not

- Do not delete data or deploy. I do these operations.
- Do not add a dependency. If you think that a dependency is necessary, ask me.
- Do not touch secrets or `.env` files.
- Do not change how recordings, transcripts, history and `vocabulary.json` are stored or deleted without asking (`history.py`, `calls.py`, `vocabulary.py`). These files are my real calls and dictations, and a mistake loses them for good.
- Do not change the app's bundle id, signing or `Info.plist` (`build.py`, `wav2sum/native/Info.plist`) without asking. macOS ties Microphone, Accessibility, Input Monitoring and System Audio permissions to them, and a change makes me grant all of them again.
- Do not change the daemon socket protocol (`daemon.py`, `client.py`, `native/app/Daemon.swift`) on one side only. The Python daemon and the Swift app must change together.
- Do not send audio or text outside the machine. The project is local by design: models run locally, the LLM runs in-process on MLX (mlx-lm).

## Project rules

<!-- When the agent makes the same mistake two times, add a rule here.
     Write the prohibition first, then the reason. Example:
     - Do not edit migrations/. These files are already applied on production. -->

- Do not add "Generated with Claude Code" or similar lines to PR descriptions, commit messages or GitHub comments, and do not add Claude as a co-author (`Co-Authored-By`, `Claude-Session` trailers). I am the only author of the commits.
- My shell is fish: give me commands in fish syntax (`set VAR value`, `env VAR=value cmd`), not bash.
