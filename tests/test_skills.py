"""Skill registry tests: parsing, deterministic trigger scoring, and the
shipped playbooks (offline). The selection matrix mirrors
scenarios/triage_eval_cases.md — keep the two in sync."""
import tempfile
import unittest
from pathlib import Path

from app.skills import SkillRegistry, _parse_skill

SHIPPED = Path(__file__).resolve().parents[1] / "skills"

_ALPHA = """---
name: alpha
title: Alpha
description: test skill
servers: Canvas   # inline comment is stripped
categories: things, stuff
risk: high
triggers: deploy, grade, rubric
---

## Plan guidance
plan text

## Review checklist
check text

## Verification
verify text
"""

_BETA = """---
name: beta
title: Beta
triggers: roll out, ship
---

## Plan guidance
beta plan
"""


def _registry_with(*skill_texts: str) -> SkillRegistry:
    reg = SkillRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        for i, text in enumerate(skill_texts):
            Path(tmp, f"s{i}.md").write_text(text, encoding="utf-8")
        reg.load(tmp)
    return reg


class TestParsing(unittest.TestCase):
    def test_front_matter_and_sections(self):
        skill = _parse_skill(_ALPHA)
        self.assertEqual(skill.name, "alpha")
        self.assertEqual(skill.servers, ["Canvas"])
        self.assertEqual(skill.categories, ["things", "stuff"])
        self.assertEqual(skill.risk, "high")
        self.assertEqual(skill.triggers, ["deploy", "grade", "rubric"])
        self.assertEqual(skill.plan_guidance, "plan text")
        self.assertEqual(skill.review_checklist, "check text")
        self.assertEqual(skill.verification, "verify text")

    def test_no_front_matter_returns_none(self):
        self.assertIsNone(_parse_skill("just a markdown file"))

    def test_missing_name_returns_none(self):
        self.assertIsNone(_parse_skill("---\ntitle: X\n---\nbody"))

    def test_risk_defaults_to_write(self):
        self.assertEqual(_parse_skill(_BETA).risk, "write")


class TestSelection(unittest.TestCase):
    def setUp(self):
        self.reg = _registry_with(_ALPHA, _BETA)

    def test_single_trigger_selects(self):
        self.assertEqual(self.reg.select("please grade this pile").name, "alpha")

    def test_boundary_blocks_infix_matches(self):
        # 'grade' must not fire inside 'upgraded'
        self.assertIsNone(self.reg.select("the upgraded servers are fine"))

    def test_prefix_matches_plural(self):
        # left-boundary prefix: 'grade' matches 'grades'
        self.assertEqual(self.reg.select("check the grades").name, "alpha")

    def test_multiword_trigger_weighs_double(self):
        # alpha scores 1 ('deploy'), beta scores 2 ('roll out' is multi-word)
        self.assertEqual(self.reg.select("roll out the deploy now").name, "beta")

    def test_higher_score_wins(self):
        self.assertEqual(
            self.reg.select("grade with the rubric, then deploy").name, "alpha"
        )

    def test_no_match_returns_none(self):
        self.assertIsNone(self.reg.select("water the office plants"))
        self.assertIsNone(self.reg.select(""))

    def test_tie_prefers_first_loaded(self):
        # one single-word hit each ('deploy' vs 'ship') — s0/alpha wins the tie
        self.assertEqual(self.reg.select("ship the deploy").name, "alpha")

    def test_get_normalizes_name(self):
        self.assertEqual(self.reg.get("  ALPHA ").name, "alpha")
        self.assertIsNone(self.reg.get("nope"))


class TestShippedPlaybooks(unittest.TestCase):
    """The five skills/*.md playbooks stay complete and selectable."""

    @classmethod
    def setUpClass(cls):
        cls.reg = SkillRegistry()
        cls.reg.load(SHIPPED)

    def test_all_playbooks_complete(self):
        self.assertEqual(len(self.reg.skills), 5)
        for skill in self.reg.skills.values():
            self.assertEqual(skill.servers, ["Canvas"], skill.name)
            self.assertTrue(skill.triggers, skill.name)
            self.assertTrue(skill.plan_guidance, skill.name)
            self.assertTrue(skill.review_checklist, skill.name)
            self.assertTrue(skill.verification, skill.name)

    def test_risk_levels(self):
        expected = {
            "canvas-announcement": "high",
            "canvas-audit": "read",
            "canvas-content-builder": "write",
            "canvas-grading": "high",
            "canvas-quiz-builder": "write",
        }
        for name, risk in expected.items():
            self.assertEqual(self.reg.get(name).risk, risk, name)

    def test_selection_matrix(self):
        cases = {
            "Grade the ungraded submissions for assignment 42 in course "
            "71186 against the rubric": "canvas-grading",
            "Post an announcement reminding the class that the midterm "
            "is on Friday": "canvas-announcement",
            "Build a new week 3 module with an overview page and one "
            "assignment": "canvas-content-builder",
            "Create a 10-question quiz about NumPy basics": "canvas-quiz-builder",
            "How is course 71186 doing? Give me a status overview of the "
            "grading backlog": "canvas-audit",
        }
        for task, expected in cases.items():
            skill = self.reg.select(task)
            self.assertIsNotNone(skill, task)
            self.assertEqual(skill.name, expected, task)

    def test_untriggered_task_gets_generic_workflow(self):
        self.assertIsNone(self.reg.select("Enroll the new TA in section 2"))


if __name__ == "__main__":
    unittest.main()
