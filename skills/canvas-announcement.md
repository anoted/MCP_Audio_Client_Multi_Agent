---
name: canvas-announcement
title: Post an announcement or discussion
description: Draft the message, review tone and audience, then post — announcements notify every enrolled student, so posting always needs approval.
servers: Canvas
categories: announcements, discussions
risk: high
triggers: announce, announcement, notify students, discussion, post to the class, remind the class, reminder
---

## Plan guidance
- Step 1: draft the full announcement/discussion text in the step report — do NOT post yet.
- Step 2: post the approved draft with create_announcement / create_discussion. This notifies every enrolled student and passes an approval checkpoint.
- Keep it to exactly these two steps plus a read-only verification step.

## Review checklist
- The draft says exactly what the user asked to communicate — dates, times, links and course names are correct.
- Tone is appropriate for students; no internal notes, grades or PII of individual students leak into the text.
- It targets the right course id.

## Verification
- List announcements/discussions for the course and confirm the new item exists with the approved title and body.
