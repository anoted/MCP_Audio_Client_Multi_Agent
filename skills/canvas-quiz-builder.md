---
name: canvas-quiz-builder
title: Build a quiz
description: Draft quiz questions, create the quiz unpublished, add questions, and verify — publishing is a separate approved action.
servers: Canvas
categories: quizzes
risk: write
triggers: quiz, quizzes, question, questions, exam, test bank, multiple choice, true/false
---

## Plan guidance
- Step 1 (read-only): check existing quizzes in the course so titles don't collide, and gather the topic material if it lives in course pages/files.
- Step 2: create the quiz shell UNPUBLISHED with title, description, quiz type and points.
- Step 3: add the questions (one step may add all questions; include full question text, choices, and correct answers in the instruction).
- Publishing is its own final step and only if the user asked for it — publish_quiz is a high-risk action that goes through an approval checkpoint.
- Final step: read-only verification.

## Review checklist
- Question count, types and point split match the request.
- Every question has exactly one correct answer marked (unless multiple-answer was requested) and no answer text is empty.
- Questions are on-topic and factually correct.
- The quiz is unpublished unless publishing was explicitly requested.

## Verification
- Re-fetch the quiz and its question count; confirm title, points, question count and published=false (or as requested). Report FAIL with ids on mismatch.
