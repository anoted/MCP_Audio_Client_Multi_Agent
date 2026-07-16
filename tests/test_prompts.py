"""MCP prompt consumption tests (offline): the manager must list prompts
across connected servers and render GetPromptResult messages to plain text
that the client can send as ordinary user input."""
import asyncio
import unittest

from mcp import types

from app.mcp_manager import MCPManager


def _prompt_result(*texts: str) -> types.GetPromptResult:
    return types.GetPromptResult(
        messages=[
            types.PromptMessage(
                role="user", content=types.TextContent(type="text", text=t)
            )
            for t in texts
        ]
    )


class FakeSession:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def get_prompt(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.result


class FakeConn:
    def __init__(self, prompts=(), session=None, connected=True):
        self.prompts = list(prompts)
        self.session = session
        self.connected = connected
        self.error = None
        self.tools = []
        self.resources = []


GRADE = types.Prompt(
    name="grade_homework",
    description="Grade every ungraded submission against a rubric.",
    arguments=[
        types.PromptArgument(name="course_id", required=True),
        types.PromptArgument(name="rubric", description="criteria", required=False),
    ],
)


class TestPromptListing(unittest.TestCase):
    def setUp(self):
        self.mgr = MCPManager()

    def test_lists_prompts_with_argument_specs(self):
        self.mgr.connections["Canvas"] = FakeConn(prompts=[GRADE])
        listed = self.mgr.prompts()
        self.assertEqual(len(listed), 1)
        p = listed[0]
        self.assertEqual(p["server"], "Canvas")
        self.assertEqual(p["name"], "grade_homework")
        self.assertEqual(
            p["arguments"],
            [
                {"name": "course_id", "description": "", "required": True},
                {"name": "rubric", "description": "criteria", "required": False},
            ],
        )

    def test_disconnected_server_prompts_hidden(self):
        self.mgr.connections["Canvas"] = FakeConn(prompts=[GRADE], connected=False)
        self.assertEqual(self.mgr.prompts(), [])

    def test_promptless_server_contributes_nothing(self):
        self.mgr.connections["demo"] = FakeConn()
        self.assertEqual(self.mgr.prompts(), [])


class TestPromptRendering(unittest.TestCase):
    def setUp(self):
        self.mgr = MCPManager()

    def render(self, server="Canvas", name="grade_homework", args=None):
        return asyncio.run(self.mgr.get_prompt(server, name, args))

    def test_renders_message_text(self):
        session = FakeSession(result=_prompt_result("Grade course 71186."))
        self.mgr.connections["Canvas"] = FakeConn(prompts=[GRADE], session=session)
        self.assertEqual(self.render(args={"course_id": "71186"}),
                         "Grade course 71186.")
        self.assertEqual(session.calls,
                         [("grade_homework", {"course_id": "71186"})])

    def test_multiple_messages_joined(self):
        session = FakeSession(result=_prompt_result("part one", "part two"))
        self.mgr.connections["Canvas"] = FakeConn(prompts=[GRADE], session=session)
        self.assertEqual(self.render(), "part one\n\npart two")

    def test_unknown_server_returns_none(self):
        self.assertIsNone(self.render(server="nope"))

    def test_server_error_returns_none(self):
        session = FakeSession(error=RuntimeError("missing required argument"))
        self.mgr.connections["Canvas"] = FakeConn(prompts=[GRADE], session=session)
        self.assertIsNone(self.render())

    def test_empty_result_returns_none(self):
        session = FakeSession(result=_prompt_result())
        self.mgr.connections["Canvas"] = FakeConn(prompts=[GRADE], session=session)
        self.assertIsNone(self.render())


if __name__ == "__main__":
    unittest.main()
