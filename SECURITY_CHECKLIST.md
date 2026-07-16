# Security & manual-verification checklist

Things that were deliberately **not** exercised automatically (the Canvas MCP
tools point at a **live** Canvas instance) plus the hardening steps that need a
human decision. Work through this before using the workflow on a real course.

## Do first, manually, in a sandbox course

- [ ] **Run the new Canvas tools read-only against a sandbox course** before
      anything else: `get_course_overview`, `get_assignment_scores`,
      `open_course_explorer`, `render_grade_distribution`,
      `render_assignment_averages`, `render_course_progress`. All are
      read-only by design — confirm none of them changed anything by
      re-checking the course in the Canvas web UI afterwards.
- [ ] **Verify the initiator classified every Canvas tool correctly**
      (Agents panel → `N tools → x read · y modify`). Open Settings → MCP
      Servers and spot-check: `grade_submission`, `publish_quiz`,
      `create_announcement`, `delete_module*`, `upload_file` must be
      *modify*; every `list_*` / `get_*` / `render_*` / `open_*` must be
      *read*. If the LLM classification looks wrong, fix the tool description
      or restart so the initiator re-runs — the explorer/planner/reviewer/
      verifier tool surfaces and the app bridge all depend on this table.
- [ ] **Walk one full workflow in the sandbox**: task → plan → approve plan →
      execution with approval prompts → reviewer/verifier PASS → done. Confirm
      each write really landed (Canvas UI) and that denying an approval really
      prevents the API call (check `logs/session-*.jsonl`: `approval_result`
      with `approved=false` and **no** following `tool_result` for that tool —
      and no matching entry in the server-side log
      `examples/Canvas_MCP/logs/canvas-mcp-*.jsonl`, the independent witness).
- [ ] **Test grading end-to-end on fake students only.** `grade_submission`
      posts real grades and comments instantly. With privacy mode ON, verify
      the comment that lands in Canvas contains the real student name, not
      `Student-N` (outward de-pseudonymization), and that `logs/` and the LLM
      history contain only pseudonyms.
- [ ] **Announcements/discussions notify every enrolled student.** Only test
      `create_announcement` in a course with no real students enrolled.
- [ ] Remember the MCP surface has **no delete for assignments/quizzes/pages**
      — anything created during tests must be removed by hand in the Canvas
      UI (`delete_module` / `delete_module_item` only detach/remove modules).

## Credentials & network

- [ ] `examples/Canvas_MCP/.env` holds a **real API token** — confirm it is a
      token for the sandbox account (least privilege), not your production
      admin token, and that `.env` stays out of git (it is gitignored; check
      `git status` after changes).
- [ ] Rotate the Canvas token if it was ever pasted into a chat, log, or
      shared screen. Canvas → Account → Settings → Approved Integrations.
- [ ] The voice client binds to `127.0.0.1` — keep it that way unless you add
      auth; the WebSocket accepts tool-affecting messages (approvals, MCP
      registration) from anyone who can reach it.
- [ ] If you run the Canvas MCP server over HTTP instead of stdio, bind it to
      localhost only (it has no auth on the MCP endpoint).
- [ ] `LLM_API_KEY` / `NVIDIA_API_KEY` in `.env`: same care — never commit.

## Governance settings (Settings ⚙ → General / Privacy)

- [ ] Approval mode: keep **high-risk only** (default) or raise to **every
      write** for real courses. Only use **off** in throwaway sandboxes.
- [ ] Privacy-preserving processing ON if your LLM endpoint is third-party
      hosted — student names/emails then never leave the machine unmasked.
      (IDs remain; treat logs as sensitive either way.)
- [ ] Injection guard ON. Canvas submission text is untrusted student input
      that flows into the LLM — the guard flags but cannot catch everything,
      so watch the Activity panel for `injection_flagged` events after
      grading runs and read the flagged submission yourself.
- [ ] Audit log ON; `logs/` is gitignored but lives in plaintext on disk —
      clean it out when a machine is shared. The same applies to the Canvas
      MCP server's own tool-call log in `examples/Canvas_MCP/logs/`
      (gitignored, PII-masked, still sensitive; `CANVAS_MCP_AUDIT=0` turns
      it off).

## Known limits to keep in mind

The full catalog — avoidable failures vs. those the system can only survive —
is `FAILURE_MODES.md`. Headline limits:

- Reviewer/verifier verdicts come from the same LLM: they are a strong
  cross-check, not a guarantee. The human approval checkpoints are the actual
  gate — do not approve writes you haven't read.
- The review/verify enforcement is per **plan step** (`set_todo_status`). A
  user talking directly to @assistant (outside the workflow) bypasses plan
  gating — approval checkpoints still apply, but review/verify do not.
- Barge-in: the echo fix relies on browser AEC plus a software gate; with an
  unusual speaker/mic setup validate it once (play a long answer through the
  speakers; the assistant must not interrupt itself, and your voice must
  still stop it within ~0.3 s). Use Manual mode if a setup misbehaves.
- The MCP-app iframes are sandboxed (`allow-scripts`, no same-origin) and the
  bridge refuses non-read tools, but apps do render live course data —
  don't screen-share the explorer with students present.
