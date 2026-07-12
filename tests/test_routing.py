"""Initiator grant-selector and server-routing tests (offline)."""
import unittest

from app.agents import Initiator, _heuristic_class, _keyword_tags


class TestHeuristicClass(unittest.TestCase):
    def test_name_verb_beats_description_words(self):
        # "grade charts" in the description must not make an open_/render_
        # tool a write — the leading verb of the name wins.
        self.assertEqual(
            _heuristic_class("Canvas__open_course_explorer",
                             "browse assignments with grade charts"),
            "read",
        )
        self.assertEqual(
            _heuristic_class("Canvas__render_grade_distribution",
                             "histogram of graded scores"),
            "read",
        )

    def test_writes_stay_writes(self):
        for name in ("Canvas__grade_submission", "Canvas__create_quiz",
                     "Canvas__publish_quiz", "Canvas__delete_module",
                     "Canvas__upload_file", "Canvas__update_page"):
            self.assertEqual(_heuristic_class(name, ""), "modify", name)

    def test_reads_stay_reads(self):
        for name in ("Canvas__list_courses", "Canvas__get_page",
                     "Canvas__read_module_item", "demo__roll_dice"):
            self.assertEqual(_heuristic_class(name, ""), "read", name)

    def test_unknown_defaults_to_modify(self):
        self.assertEqual(_heuristic_class("srv__frobnicate", "does things"),
                         "modify")


def make_initiator() -> tuple[Initiator, set[str]]:
    """Two servers with a small classified inventory."""
    ini = Initiator()
    inventory = {
        "Canvas__list_courses": "read",
        "Canvas__list_assignments": "read",
        "Canvas__create_assignment": "modify",
        "Canvas__grade_submission": "modify",
        "Canvas__list_quizzes": "read",
        "Canvas__create_quiz": "modify",
        "demo__get_time": "read",
        "demo__roll_dice": "read",
    }
    ini.classes = dict(inventory)
    ini.category = {
        "Canvas__list_courses": "courses",
        "Canvas__list_assignments": "assignments",
        "Canvas__create_assignment": "assignments",
        "Canvas__grade_submission": "submissions",
        "Canvas__list_quizzes": "quizzes",
        "Canvas__create_quiz": "quizzes",
        "demo__get_time": "time",
        "demo__roll_dice": "dice",
    }
    ini.tags = {
        name: _keyword_tags(name) | {ini.category[name]} for name in inventory
    }
    return ini, set(inventory)


class TestSelectors(unittest.TestCase):
    def setUp(self):
        self.ini, self.available = make_initiator()

    def expand(self, selectors, servers=None):
        granted, unmatched = self.ini.expand(selectors, self.available, servers)
        return granted, unmatched

    def test_read_class(self):
        granted, _ = self.expand(["read"])
        self.assertIn("Canvas__list_courses", granted)
        self.assertNotIn("Canvas__create_quiz", granted)

    def test_category(self):
        granted, _ = self.expand(["quizzes"])
        self.assertEqual(
            granted, {"Canvas__list_quizzes", "Canvas__create_quiz"}
        )

    def test_access_narrowed_category(self):
        granted, _ = self.expand(["write:assignments"])
        self.assertEqual(granted, {"Canvas__create_assignment"})

    def test_exact_name(self):
        granted, _ = self.expand(["Canvas__grade_submission"])
        self.assertEqual(granted, {"Canvas__grade_submission"})

    def test_server_route_all(self):
        granted, _ = self.expand(["server:demo"])
        self.assertEqual(granted, {"demo__get_time", "demo__roll_dice"})

    def test_server_colon_selector(self):
        granted, _ = self.expand(["Canvas:read"])
        self.assertEqual(
            granted,
            {"Canvas__list_courses", "Canvas__list_assignments",
             "Canvas__list_quizzes"},
        )

    def test_skill_server_scoping_restricts_universe(self):
        # With the skill routed to Canvas, demo tools cannot be granted at all.
        granted, unmatched = self.expand(["all"], servers=["Canvas"])
        self.assertTrue(all(n.startswith("Canvas__") for n in granted))
        granted, unmatched = self.expand(["dice"], servers=["Canvas"])
        self.assertEqual(granted, set())
        self.assertEqual(unmatched, ["dice"])

    def test_unmatched_reported(self):
        granted, unmatched = self.expand(["nonexistent-thing"])
        self.assertEqual(granted, set())
        self.assertEqual(unmatched, ["nonexistent-thing"])


class TestAgentToolSurfaces(unittest.TestCase):
    def setUp(self):
        self.ini, _ = make_initiator()

    def test_read_only_agents_never_see_write_tools(self):
        for agent in ("explorer", "planner", "reviewer", "verifier"):
            allowed = self.ini.allowed_for(agent)
            self.assertIsNotNone(allowed, agent)
            for name in allowed:
                self.assertEqual(self.ini.classes[name], "read",
                                 f"{agent} was granted write tool {name}")

    def test_manager_delegates_only(self):
        self.assertEqual(self.ini.allowed_for("manager"), set())

    def test_assistant_unrestricted(self):
        self.assertIsNone(self.ini.allowed_for("assistant"))


if __name__ == "__main__":
    unittest.main()
