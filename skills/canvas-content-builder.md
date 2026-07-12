---
name: canvas-content-builder
title: Build course content (modules, pages, assignments)
description: Create or restructure modules, pages, files and assignments as unpublished drafts, verified before anything goes live.
servers: Canvas
categories: modules, pages, assignments, files
risk: write
triggers: module, modules, page, pages, assignment, assignments, week, unit, lesson, syllabus, upload, course content, build
---

## Plan guidance
- Start with one read-only step that captures the current course structure (modules, existing pages/assignments) so nothing is duplicated or overwritten.
- Create everything UNPUBLISHED unless the user explicitly asked to publish.
- One step per artifact kind (module shell → page(s) → assignment → attach items to the module) so each sub-agent has a narrow, checkable deliverable.
- Each step's instruction must repeat the ids produced by earlier steps (course id, module id, page url) — sub-agents share no memory.
- Final step: read-only verification of the whole structure.

## Review checklist
- Names, due dates and point values match what the user asked for exactly.
- New content is unpublished (unless the user explicitly requested publishing).
- Page/assignment bodies are complete (no placeholder text like TODO or lorem).
- Nothing pre-existing in the course was modified or deleted unless the task explicitly required it.

## Verification
- Re-list the module and its items; confirm each expected item exists, is attached to the right module, and its published flag is false (or as requested).
- Open the created page/assignment and confirm the body/points/due date; report FAIL with ids on any mismatch.
