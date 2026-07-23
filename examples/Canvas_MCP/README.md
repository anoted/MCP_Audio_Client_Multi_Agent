# Canvas LMS MCP Server

An MCP server (Python SDK, **streamable HTTP with bearer-token auth**) that puts the
Canvas API behind tools, resources, and prompts so an LLM client (the bundled voice
client, Claude Code, Claude Desktop, etc.) can run the day-to-day of teaching a
course: set and grade homework, build quizzes, create pages, upload files, post
announcements, and manage modules.

## Setup

1. Copy `.env.example` to `.env` and fill in:
   - `CANVAS_BASE_URL` — e.g. `https://yourschool.instructure.com`
   - `CANVAS_API_TOKEN` — Canvas → Account → Settings → **+ New Access Token**
   - `CANVAS_MCP_AUTH_TOKEN` — the bearer token HTTP clients must present;
     generate one:

     ```
     conda run -n mcpagents python canvas_mcp_server.py --make-token
     ```

2. Start the server (uses the conda `mcpagents` environment; run it as its own
   process, separate from whatever client connects to it):

   ```
   conda run --no-capture-output -n mcpagents python canvas_mcp_server.py
   ```

   MCP endpoint: `http://127.0.0.1:8017/mcp` — requests without
   `Authorization: Bearer <CANVAS_MCP_AUTH_TOKEN>` get 401. The server refuses
   to start over HTTP if the token is unset.

3. Connect a client with the same token:
   - **Voice client** (this repo): `mcp_servers.json` already registers
     `http://127.0.0.1:8017/mcp` with header
     `Authorization: Bearer ${CANVAS_MCP_AUTH_TOKEN}` — just put the token in
     the repo root `.env` too.
   - **Claude Code**:

     ```
     claude mcp add --transport http canvas http://127.0.0.1:8017/mcp --header "Authorization: Bearer <token>"
     ```

> **Security note:** your Canvas token gives full account access, so the endpoint is
> guarded: bearer-token auth (constant-time compare, refuses to start without a
> token), Host/Origin allowlists against DNS rebinding, localhost bind by default,
> and rejected requests logged to `logs/`. Keep `MCP_HOST=127.0.0.1` unless you put
> the server behind an HTTPS reverse proxy. Never commit `.env`. The repo root's
> `MCP_security.md` documents the full model, the OAuth 2.1 upgrade path, and the
> MCP 2.0 outlook; `stdio_instruction.md` covers the auth-free stdio mode.

## Tools (31)

| Area | Tools |
|---|---|
| Courses & people | `list_courses`, `list_students` |
| Homework | `list_assignments`, `create_assignment` (description, deadline, points, unlock/lock window, optional `module_id`), `update_assignment`, `list_submissions`, `get_submission_text`, `grade_submission` |
| Quizzes | `list_quizzes`, `create_quiz` (optional `module_id`), `add_quiz_question`, `update_quiz`, `publish_quiz` |
| Modules | `list_modules`, `create_module`, `update_module`, `add_module_item`, `read_module_item`, `update_module_item`, `delete_module_item`, `delete_module` |
| Pages | `list_pages`, `get_page`, `create_page` (optional `module_id`), `update_page` |
| Files | `list_files`, `get_file_text`, `upload_file` (local file → course Files, optional `module_id`) |
| Communication | `create_announcement`, `list_announcements`, `create_discussion` |

### Reading content

- `get_submission_text` — a student's submission as assessable text: online text
  entries (HTML stripped) plus extracted text from attached **PDF** and **DOCX**
  files. The LLM client does the assessment, then posts results with `grade_submission`.
- `read_module_item` — the content behind any module item: page bodies, file text
  (**PDF / DOCX / PPTX / HTML / plain text**), assignment/quiz/discussion
  descriptions, or external URLs.
- `get_file_text` — any course file by id, same formats as above.

### Adding content to modules

`create_page`, `upload_file`, `create_assignment`, and `create_quiz` all accept an
optional `module_id` to attach the new content to a module in one call; anything
else (existing files, discussions, external links, subheaders) goes through
`add_module_item`. Everything is created **unpublished** by default so the
instructor can review before students see it — announcements are the exception
(live immediately unless `delayed_post_at` is set).

`upload_file` implements Canvas's three-step upload, so local PDFs, PowerPoint
decks, Word docs, images, etc. can be pushed straight into the course from disk.

## Resources

Read-only JSON views: `canvas://courses`, and per-course
`.../assignments`, `.../quizzes`, `.../modules`, `.../pages`, `.../files`.

## Prompts

- **grade_homework**(course_id, assignment_id, rubric) — fetch ungraded submissions,
  assess each against the rubric, show a grade table for approval, then post grades.
- **assess_single_submission**(course_id, assignment_id, user_id, rubric) — detailed
  assessment of one student, no grade posted.
- **build_quiz**(course_id, topic, num_questions, difficulty) — draft questions for
  review, then create the quiz + questions (left unpublished).
- **build_course_module**(course_id, module_name, materials) — outline then assemble
  a full module: overview page, uploaded files, assignment with deadline/points, and
  a quiz, all as unpublished drafts.
