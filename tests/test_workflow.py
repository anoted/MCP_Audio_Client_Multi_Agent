"""Workflow state machine + approval policy tests (offline)."""
import unittest

from app.config import settings
from app.workflow import Workflow, approval_required, risk_of


class TestRisk(unittest.TestCase):
    def test_read_tools_are_read(self):
        self.assertEqual(risk_of("Canvas__list_courses", "read"), "read")

    def test_plain_writes_are_write(self):
        self.assertEqual(risk_of("Canvas__create_module", "modify"), "write")
        self.assertEqual(risk_of("Canvas__update_page", "modify"), "write")

    def test_dangerous_writes_are_high(self):
        for name in (
            "Canvas__grade_submission", "Canvas__publish_quiz",
            "Canvas__create_announcement", "Canvas__delete_module",
            "Canvas__upload_file",
        ):
            self.assertEqual(risk_of(name, "modify"), "high", name)

    def test_approval_matrix(self):
        self.assertFalse(approval_required("read", "all"))
        self.assertTrue(approval_required("write", "all"))
        self.assertTrue(approval_required("high", "all"))
        self.assertFalse(approval_required("write", "high"))
        self.assertTrue(approval_required("high", "high"))
        self.assertFalse(approval_required("high", "off"))


class TestWorkflow(unittest.TestCase):
    def setUp(self):
        self._mode = settings.approval_mode
        settings.approval_mode = "high"
        self.wf = Workflow()
        self.wf.begin("build week 3", "canvas-content-builder")

    def tearDown(self):
        settings.approval_mode = self._mode

    def test_plan_pauses_for_review(self):
        self.wf.set_plan(["step one", "step two"])
        self.assertEqual(self.wf.stage, "plan_review")
        self.assertTrue(self.wf.plan_pending)
        self.assertTrue(self.wf.approve_plan())
        self.assertEqual(self.wf.stage, "executing")

    def test_plan_skips_review_when_approvals_off(self):
        settings.approval_mode = "off"
        self.wf.set_plan(["step one"])
        self.assertEqual(self.wf.stage, "executing")

    def test_reject_plan_returns_to_planning(self):
        self.wf.set_plan(["step one"])
        self.wf.reject_plan()
        self.assertEqual(self.wf.stage, "planning")
        self.assertEqual(self.wf.todos, [])

    def test_write_step_blocked_until_review_and_verify(self):
        self.wf.set_plan(["create the module"])
        self.wf.approve_plan()
        self.wf.todos[0]["status"] = "in_progress"
        self.wf.mark_wrote(0)
        block = self.wf.completion_block(0)
        self.assertIsNotNone(block)
        self.assertIn("reviewer", block)
        self.wf.record_review(0, "pass", "review")
        block = self.wf.completion_block(0)
        self.assertIsNotNone(block)
        self.assertIn("verifier", block)
        self.wf.record_review(0, "pass", "verify")
        self.assertIsNone(self.wf.completion_block(0))

    def test_failed_review_still_blocks(self):
        self.wf.set_plan(["create the module"])
        self.wf.approve_plan()
        self.wf.mark_wrote(0)
        self.wf.record_review(0, "fail", "review")
        self.wf.record_review(0, "pass", "verify")
        self.assertIsNotNone(self.wf.completion_block(0))

    def test_read_only_step_closes_freely(self):
        self.wf.set_plan(["look things up"])
        self.wf.approve_plan()
        self.assertIsNone(self.wf.completion_block(0))

    def test_completion(self):
        self.wf.set_plan(["a", "b"])
        self.wf.approve_plan()
        for t in self.wf.todos:
            t["status"] = "done"
        self.assertTrue(self.wf.maybe_complete())
        self.assertEqual(self.wf.stage, "complete")

    def test_serialization_round_trip(self):
        self.wf.set_plan(["a"])
        self.wf.mark_wrote(0)
        self.wf.record_review(0, "pass", "review")
        data = self.wf.to_dict()
        other = Workflow()
        other.load(data)
        self.assertEqual(other.stage, self.wf.stage)
        self.assertEqual(other.todos[0]["review"], "pass")
        self.assertTrue(other.todos[0]["wrote"])

    def test_legacy_todos_load(self):
        other = Workflow()
        other.load({"stage": "executing",
                    "todos": [{"text": "old step", "status": "done"}]})
        self.assertEqual(other.todos[0]["text"], "old step")
        self.assertFalse(other.todos[0]["wrote"])


if __name__ == "__main__":
    unittest.main()
