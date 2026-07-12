---
name: canvas-grading
title: Grade submissions against a rubric
description: Fetch ungraded submissions, assess each against the rubric, post grades and feedback only after human approval.
servers: Canvas
categories: submissions, assignments, students
risk: high
triggers: grade, grading, graded, rubric, feedback, score, scores, assess, marking, marks, regrade
---

## Plan guidance
- First step: read-only — fetch the assignment details (points possible, rubric/description) and the list of ungraded submissions.
- One step per batch of submissions to assess (read submission text, draft grade + short constructive feedback), followed by ONE separate step that posts the drafted grades with grade_submission.
- Never combine "assess" and "post" in the same step: assessment is read-only, posting changes real student grades and passes an approval checkpoint.
- Final step: read-only verification that every posted grade matches the draft and no submission was missed.

## Review checklist
- Every proposed grade is within 0..points_possible for the assignment.
- Feedback is specific to the submission content, constructive, and free of PII beyond the student pseudonym.
- The number of assessed submissions matches the number of ungraded submissions found in step 1.
- No grade was posted for a student whose submission was empty/missing without that being noted.

## Verification
- Re-fetch the submissions with only_ungraded=false and confirm each posted score and comment matches the approved draft.
- Report any mismatch or still-ungraded submission as FAIL with the exact ids.
