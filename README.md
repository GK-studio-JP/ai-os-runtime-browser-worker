# ai-os-runtime-browser-worker

`ai-os-runtime-browser-worker` is the experimental Browser Chat Worker path for the GitHub-native AI OS.

It does **not** replace the API/subprocess runtime. The preserved provider-neutral runtime remains:

`GK-studio-JP/ai-os-runtime`

This fork explores a different execution model: give a Chat Worker a small bootstrap, a run-scoped identity, the Browser Agent operating instructions, and the canonical bulletin-board URL. The Worker then pulls the live task from GitHub, performs the work through Browser Agent and other allowed tools, and reports coordination events back to the canonical Issue.

```text
Scheduler / Kernel routing
          |
          v
small Chat bootstrap + agent_id
          |
          v
Browser Chat Worker
   |             \
   |              +--> Browser Agent --> GitHub / Web systems
   |
   +--> canonical bulletin board
        read task -> CLAIM -> PROGRESS -> RESULT
```

## Canonical sources

Canonical coordination journal:

`https://github.com/GK-studio-JP/ai-bulletin-board/issues`

Canonical board protocol:

`https://github.com/GK-studio-JP/ai-bulletin-board/blob/main/protocol/GITHUB_PROTOCOL.md`

Browser Agent:

`https://github.com/GK-studio-JP/browser-agent`

Browser Agent operating instructions:

`https://github.com/GK-studio-JP/browser-agent/blob/main/BROWSER_AGENT_INSTRUCTIONS.md`

Chat history, Browser Agent relay state, generated projections, and model memory are working context only. They are not canonical coordination state.

## Worker contract

The active browser-worker contract is split into three small files:

- `WORKER.md`: full Browser Chat Worker protocol.
- `CHAT_BOOTSTRAP.md`: minimal startup instruction for a Chat Worker.
- `process.json`: process identity and boundary metadata for `PROC-RUNTIME-BROWSER-WORKER`.

The task body itself stays on the bulletin board. A launcher may provide an Issue number as a pointer, but the Worker reads the current Issue body and comments from GitHub before acting.

## Execution loop

A normal run is:

1. Start the Chat Worker with one run-scoped `agent_id`.
2. Read `WORKER.md` and the current Browser Agent instructions.
3. Open the canonical bulletin board with Browser Agent.
4. Resolve only the task explicitly routed to this Worker/process.
5. Replay the canonical Issue history and fail closed on `history_unsafe`.
6. Append CLAIM.
7. Re-fetch/replay and begin work only if this `agent_id` is the live winning owner.
8. Use Browser Agent to inspect and operate the target system.
9. Append PROGRESS checkpoints for long work.
10. Before completion, re-fetch/replay ownership and verify immutable artifacts.
11. Append RESULT.
12. End the browser session when no continued session is required.

The Scheduler still owns task ordering. The Worker must not choose among ambiguous tasks or invent priority.

## Authority boundary

Browser access is not authority by itself.

The Worker does not gain capabilities merely because:

- it has an `agent_id`;
- a Chat model says an action is allowed;
- Browser Agent can see or click a control;
- a generated projection suggests a task is runnable.

Platform permissions, user authorization, Kernel policy, and the canonical board protocol remain separate boundaries.

Never publish credentials, tokens, cookies, passwords, private keys, authentication headers, or sensitive Browser Agent observations to the bulletin board.

## Preserved API runtime baseline

This repository was cloned from `GK-studio-JP/ai-os-runtime`. The following inherited files remain for comparison and migration work:

- `runtime.py`
- `driver_runner.py`
- `test_runtime.py`
- the existing runtime workflows

Those files represent the API/subprocess provider-neutral execution path. New API-runtime development should continue in `GK-studio-JP/ai-os-runtime`; browser-worker-specific development belongs here.

The inherited Runtime pipeline is:

```text
Worker Boot Bundle
  -> fresh preflight
  -> ai-os-worker-invocation:v1
  -> replaceable compute driver
  -> ai-os-worker-result:v1
  -> normalize
  -> fresh postflight gate
```

Keeping that baseline in the clone makes it possible to compare the two execution approaches without removing the original implementation.

### Runtime provenance and drift policy

`runtime-source.json` is the fail-closed provenance lock for inherited Runtime contract files. It pins the canonical repository, an immutable 40-hex Runtime commit, and the exact Git blob object ID expected on both the canonical and Browser Worker sides.

Files marked `identical` must match byte-for-byte. A file may be marked `forked` only when the manifest pins both distinct blob IDs and records a non-empty rationale. The current intentional fork is `driver_runner.py`, where the Browser Worker tolerates `BrokenPipeError` if a child exits before consuming stdin while preserving authoritative return-code handling and suppressing child stderr.

CI checks out the canonical Runtime at the commit declared by the manifest, never a floating branch or tag, then runs:

```sh
python scripts/check_runtime_drift.py --canonical-root .runtime-canonical --local-root .
```

The checker validates the manifest schema and file set, rejects unsafe paths and unknown fields/modes, recomputes Git blob IDs from file bytes, rejects symlinked contract files, and fails when either side changes without an explicit lock update.

## Integration status

Implemented and wired into the control plane:

- Browser Chat Worker protocol and minimal Chat bootstrap;
- dedicated `PROC-RUNTIME-BROWSER-WORKER` process metadata;
- Kernel registration for `PROC-RUNTIME-BROWSER-WORKER`;
- dedicated Scheduler routing via `.github/workflows/browser-worker-plan.yml`;
- canonical replay ownership checks for CLAIM / HEARTBEAT / RESULT;
- automatic lease renewal with verified canonical HEARTBEAT events;
- Browser Agent lifecycle and privacy rules;
- launcher preflight and production Browser Worker execution through `GK-studio-JP/browser-agent`;
- end-to-end runs that verify routed task ownership, browser evidence, and canonical RESULT completion.

Current hardening work is focused on reproducible provenance and dependency pinning across the control plane, plus exercising HEARTBEAT renewal in a deliberately long-running production task.
