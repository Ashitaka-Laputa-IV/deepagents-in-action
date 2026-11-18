# Chapter demos

Each script is a standalone, runnable extraction of one notebook chapter. The
scripts call the OpenCode Go gateway with the `deepseek-v4.1-flash` model and
print the observations that the notebook asserts.

## Quick start

```bat
copy .env.example .env
rem Edit .env and set OPENCODE_GO_API_KEY, then:
python -m pip install -r demo\requirements.txt
python demo\ch01.py
```

The demo uses this endpoint by default:

| Setting | Default |
| --- | --- |
| Base URL | `https://opencode.ai/inference/go/v1` |
| Model | `deepseek-v4.1-flash` |
| API key | required: `OPENCODE_GO_API_KEY` from the repo-root `.env` or environment |
| Required header | `x-opencode-session` (generated per process) |

Override any of them without editing code:

```bat
set OPENCODE_GO_API_KEY=...
set OPENCODE_GO_BASE_URL=https://opencode.ai/inference/go/v1
set OPENCODE_GO_MODEL=deepseek-v4.1-flash
```

## Chapter map

| Demo | Notebook | What it proves |
| --- | --- | --- |
| `ch01.py` | `notebooks/ch01/01-agent-harness.ipynb` | The deep-agent harness adds filesystem, execution, planning, and task tools on top of a plain agent. |
| `ch02.py` | `notebooks/ch02/01-quickstart.ipynb`, `02-research-assistant.ipynb` | Tool schemas, chained tool calls, tool errors, planning, search, and state files. |
| `ch04.py` | `notebooks/ch04/01-task-planning.ipynb` | `TodoListMiddleware`, full-list replacement semantics, and streaming a plan to completion. |
| `ch05.py` | `notebooks/ch05/01-subagent-delegation.ipynb` | Parent/child context isolation with a shared filesystem and an explicit `read_file` follow-up. |
| `ch06.py` | `notebooks/ch06/01-async-subagent-lifecycle.ipynb` | A real local LangGraph server, async subagent tasks, polling, update, and cancel. |
| `ch07.py` | `notebooks/ch07/01-skills-progressive-disclosure.ipynb` | Skill catalog first, then `SKILL.md`, then a referenced file on demand; missing references fail loudly. |
| `ch08.py` | `notebooks/ch08/01-long-term-memory.ipynb` | `CompositeBackend` routes `/memories/` to a `Store`, so memory survives threads and is isolated per user. |
| `ch09.py` | `notebooks/ch09/01-tool-approval-and-resume.ipynb` | `interrupt_on`, approve/edit/reject/respond decisions, checkpoints, and batch decision counts. |
| `ch10.py` | `notebooks/ch10/01-sandbox-execution-and-files.ipynb` | Upload, execute, read, and download inside a sandbox; non-zero exit codes survive. |
| `ch11.py` | `notebooks/ch11/01-filesystem-permissions.ipynb` | Ordered first-match-wins permissions for allow, deny, and interrupt. |
| `ch13.py` | `notebooks/ch13/01-rubric-revision.ipynb` | Rubric grading with an evidence tool, revision feedback, and `max_iterations` as a budget. |
| `ch14.py` | `notebooks/ch14/01-streaming-projections.ipynb` | Typed projections, raw `seq`/`namespace`, custom tool progress, and late-subscription loss. |

Chapters `ch03`, `ch12`, `ch15`, `ch16`, and `ch17` have no runnable notebook
in this repository, so they are covered by `summary/` only.

## Environment notes

- `ch06.py` starts `langgraph dev` in a temporary directory. It needs the
  server dependencies in `demo/requirements.txt` and takes about a minute.
- `ch10.py` uses Docker when the daemon is available. If Docker is down it
  falls back to `LocalShellBackend` in a temporary directory and prints why.
  The Docker branch pins `network=none`, a read-only rootfs, and no host mounts.
- `ch13.py` uses beta rubric middleware and suppresses only the matching beta
  warning. `ch11.py` uses filesystem permissions, which need no warning filter.
- `ScriptedChatModel` is used only where failure or ordering must be exact:
  `ch02.py` (schema error / `KeyError`), `ch04.py` (todo replacement / fake
  source), `ch09.py` (batch decision count), `ch10.py` (fixed upload/execute
  mechanics), and `ch11.py` (permission matrix and interrupt decisions).
  `ch01.py`, `ch05.py`, `ch07.py`, `ch08.py`, `ch13.py`, and `ch14.py` are live
  end to end; `ch02.py`, `ch04.py`, `ch09.py`, and `ch11.py` also run their main
  scenario live. `ch06.py` uses a live supervisor for the async lifecycle, and
  `ch10.py` adds a live model that chooses `execute`/`read_file`.

## Expected output

Every script ends with either `All chapter-XX checks passed.` or a raised
assertion. The checks are real `assert`-style guards, not print-only
observations, so a non-zero exit means the chapter's contract was violated.
