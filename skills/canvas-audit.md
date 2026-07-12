---
name: canvas-audit
title: Course status audit (read-only)
description: Inspect a course — structure, grading backlog, upcoming deadlines, drafts — without changing anything.
servers: Canvas
categories: assignments, submissions, modules, quizzes, pages
risk: read
triggers: audit, status, overview, backlog, summary, summarize, report, how is, what's in, review the course, inspect
---

## Plan guidance
- Every step is read-only; no write tool should ever be granted for this workflow.
- Typical steps: list structure (modules/items) → assignments + due dates → ungraded submission counts per assignment → drafts/unpublished content → one consolidated report.
- The final report should be short enough to speak: lead with the two or three things that most need attention.

## Review checklist
- Numbers in the report (counts, dates) are consistent with each other.
- The report distinguishes facts from recommendations.
- No write tool was called anywhere in the workflow.

## Verification
- Spot-check two claims from the report with fresh read-only calls; FAIL if either doesn't reproduce.
