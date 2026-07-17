"""Response-pipeline regressions added with the July 2026 UI/workflow rework.

- exactly ONE system message, at position 0 (strict local OpenAI-compatible
  endpoints reject anything else: "System message must be at the beginning")
- duplicate tool calls are suppressed: identical calls in one round run once,
  identical reads later in the turn return the cached result until a write
- run_planner is blocked while a plan is already awaiting approval
- intermediate text before tool calls is recorded as 'thought' segments
- reviewer/verifier verdict reasons land on the todo (review_note/verify_note)
  and in the subagent_done event
- streamed tool-call deltas assemble even when the provider omits `index`
"""
import json
import types
import unittest
from unittest.mock import patch

from app import llm
from app.agents import _keyword_tags, initiator
from app.config import settings
from app.llm import ReasoningFilter, _slot_index, strip_reasoning
from app.session import TurnDeduper, VoiceSession, _parse_verdict

from test_adversarial import (
    INVENTORY,
    FakeMCP,
    FakeWS,
    scripted_llm,
    text_turn,
    tool_turn,
)


def multi_tool_turn(calls):
    """One LLM turn ending with several tool calls."""
    return [{
        "type": "end", "finish_reason": "tool_calls",
        "tool_calls": [
            {"id": f"c{i}", "name": name, "arguments": json.dumps(args)}
            for i, (name, args) in enumerate(calls)
        ],
    }]


def thought_turn(text, name, arguments):
    """One LLM turn that streams text and then calls a tool."""
    return [
        {"type": "delta", "text": text},
        {"type": "end", "finish_reason": "tool_calls",
         "tool_calls": [{"id": "c1", "name": name,
                         "arguments": json.dumps(arguments)}]},
    ]


class ResponseFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = {
            "nvidia_api_key": settings.nvidia_api_key,
            "audit_enabled": settings.audit_enabled,
            "approval_mode": settings.approval_mode,
            "privacy_enabled": settings.privacy_enabled,
            "injection_guard_enabled": settings.injection_guard_enabled,
            "classes": dict(initiator.classes),
            "category": dict(initiator.category),
            "tags": dict(initiator.tags),
        }
        settings.nvidia_api_key = ""
        settings.audit_enabled = False
        settings.privacy_enabled = False
        settings.injection_guard_enabled = False
        settings.approval_mode = "off"
        initiator.classes = {n: a for n, (a, _) in INVENTORY.items()}
        initiator.category = {n: c for n, (_, c) in INVENTORY.items()}
        initiator.tags = {
            n: _keyword_tags(n) | {c} for n, (_, c) in INVENTORY.items()
        }

    async def asyncTearDown(self):
        for key in ("nvidia_api_key", "audit_enabled", "approval_mode",
                    "privacy_enabled", "injection_guard_enabled"):
            setattr(settings, key, self._saved[key])
        initiator.classes = self._saved["classes"]
        initiator.category = self._saved["category"]
        initiator.tags = self._saved["tags"]

    def make_session(self, mcp) -> tuple[VoiceSession, list[dict]]:
        session = VoiceSession(FakeWS(), mcp)
        events: list[dict] = []
        session.send_json = events.append
        return session, events

    # -- message building --------------------------------------------------------

    async def test_single_system_message_at_position_zero(self):
        mcp = FakeMCP({n: "ok" for n in INVENTORY})
        session, _ = self.make_session(mcp)
        session._thread("manager").extend([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        for agent in ("manager", "planner", "reviewer", "assistant"):
            messages = session._build_messages(agent)
            self.assertEqual(messages[0]["role"], "system")
            self.assertTrue(
                all(m["role"] != "system" for m in messages[1:]),
                f"extra system message in {agent}'s request",
            )

    # -- duplicate-call suppression ----------------------------------------------

    async def test_identical_calls_in_one_round_run_once(self):
        mcp = FakeMCP({"Canvas__list_quizzes": "[1, 2]"})
        session, _ = self.make_session(mcp)
        session.agent = "assistant"
        turns = [
            multi_tool_turn([("Canvas__list_quizzes", {}),
                             ("Canvas__list_quizzes", {})]),
            text_turn("ok"),
        ]
        with patch.object(llm, "chat_stream", scripted_llm(turns)):
            session._start_response("list quizzes")
            await session.response_task
        self.assertEqual(len(mcp.calls), 1)
        tool_msgs = [m for m in session.histories["assistant"]
                     if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 2)  # every tool_call_id answered
        self.assertIn("Duplicate call skipped", tool_msgs[1]["content"])
        self.assertIn("[1, 2]", tool_msgs[1]["content"])

    async def test_repeated_read_cached_until_a_write(self):
        mcp = FakeMCP({
            "Canvas__list_quizzes": "[1, 2]",
            "Canvas__create_quiz": "created",
        })
        session, _ = self.make_session(mcp)
        session.agent = "assistant"
        turns = [
            tool_turn("Canvas__list_quizzes", {}),
            tool_turn("Canvas__list_quizzes", {}),   # cached — not re-run
            tool_turn("Canvas__create_quiz", {"title": "T"}),
            tool_turn("Canvas__list_quizzes", {}),   # write invalidated cache
            text_turn("done"),
        ]
        with patch.object(llm, "chat_stream", scripted_llm(turns)):
            session._start_response("go")
            await session.response_task
        called = [name for name, _ in mcp.calls]
        self.assertEqual(called, ["Canvas__list_quizzes", "Canvas__create_quiz",
                                  "Canvas__list_quizzes"])
        tool_msgs = [m for m in session.histories["assistant"]
                     if m.get("role") == "tool"]
        self.assertIn("Duplicate call skipped", tool_msgs[1]["content"])
        self.assertNotIn("Duplicate call skipped", tool_msgs[3]["content"])

    def test_deduper_failed_round_duplicate_notes_failure(self):
        dedupe = TurnDeduper()
        key = TurnDeduper.key("t", {"a": 1})
        dedupe.record(key, "boom", access="read", ok=False)
        note = dedupe.cached(key)
        self.assertIn("failed", note)
        dedupe.new_round()
        self.assertIsNone(dedupe.cached(key))  # failures are never turn-cached

    async def test_planner_blocked_while_plan_pending(self):
        settings.approval_mode = "high"
        mcp = FakeMCP({n: "ok" for n in INVENTORY})
        session, _ = self.make_session(mcp)
        session.workflow.begin("task", None)
        session.workflow.set_plan(["step 1"])
        self.assertTrue(session.workflow.plan_pending)
        result = await session._run_subagent({"task": "plan again"},
                                             gen=0, role="planner")
        self.assertIn("already awaiting", result)

    # -- thought segments ----------------------------------------------------------

    async def test_intermediate_text_recorded_as_thought(self):
        mcp = FakeMCP({"Canvas__list_quizzes": "[1]"})
        session, events = self.make_session(mcp)
        session.agent = "assistant"
        turns = [
            thought_turn("Let me look that up.", "Canvas__list_quizzes", {}),
            text_turn("There is one quiz."),
        ]
        with patch.object(llm, "chat_stream", scripted_llm(turns)):
            session._start_response("how many quizzes?")
            await session.response_task
        kinds = [(e["kind"], e.get("thought", False)) for e in session.transcript]
        self.assertEqual(kinds, [("user", False), ("assistant", True),
                                 ("tool", False), ("assistant", False)])
        self.assertTrue(any(e.get("type") == "assistant_thought" for e in events))

    async def test_subagent_thoughts_interleaved_in_events(self):
        mcp = FakeMCP({"Canvas__list_quizzes": "[1]"})
        session, _ = self.make_session(mcp)
        turns = [
            thought_turn("Checking the quizzes.", "Canvas__list_quizzes", {}),
            text_turn("Found one quiz."),
        ]
        with patch.object(llm, "chat_stream", scripted_llm(turns)):
            await session._run_subagent(
                {"name": "lookup", "instruction": "count quizzes",
                 "tools": ["read"]},
                gen=0, role="worker",
            )
        entry = next(e for e in session.transcript if e["kind"] == "subagent")
        self.assertEqual(
            [ev["kind"] for ev in entry["events"]], ["thought", "tool"]
        )
        self.assertEqual(entry["events"][0]["text"], "Checking the quizzes.")
        self.assertEqual(entry["text"], "")  # answer lives in result only
        self.assertEqual(entry["result"], "Found one quiz.")

    # -- reviewer / verifier detail ---------------------------------------------------

    def test_parse_verdict(self):
        self.assertEqual(_parse_verdict("PASS — all fields match.\nMore."),
                         ("pass", "all fields match."))
        self.assertEqual(_parse_verdict("FAIL: points are wrong"),
                         ("fail", "points are wrong"))
        verdict, reason = _parse_verdict("The work looks fine to me.")
        self.assertIsNone(verdict)
        self.assertIn("no clear PASS/FAIL", reason)

    async def test_verdict_reason_lands_on_todo_and_event(self):
        mcp = FakeMCP({n: "ok" for n in INVENTORY})
        session, events = self.make_session(mcp)
        session.workflow.begin("task", None)
        session.workflow.set_plan(["build it"])
        session.workflow.mark_wrote(0)
        with patch.object(llm, "chat_stream",
                          scripted_llm([text_turn("FAIL: points are wrong")])):
            await session._run_subagent(
                {"step_index": 1, "summary": "worker report"},
                gen=0, role="reviewer",
            )
        todo = session.workflow.todos[0]
        self.assertEqual(todo["review"], "fail")
        self.assertEqual(todo["review_note"], "points are wrong")
        done = next(e for e in events if e.get("type") == "subagent_done")
        self.assertEqual(done["verdict"], "fail")
        self.assertEqual(done["reason"], "points are wrong")

    # -- model-native reasoning ---------------------------------------------------------

    async def test_reasoning_recorded_as_thought_not_history(self):
        mcp = FakeMCP({})
        session, events = self.make_session(mcp)
        session.agent = "assistant"
        turns = [[
            {"type": "reasoning", "text": "pondering the request"},
            {"type": "delta", "text": "Here is the answer."},
            {"type": "end", "finish_reason": "stop", "tool_calls": []},
        ]]
        with patch.object(llm, "chat_stream", scripted_llm(turns)):
            session._start_response("hi")
            await session.response_task
        self.assertEqual(session.histories["assistant"][-1]["content"],
                         "Here is the answer.")
        self.assertNotIn("pondering",
                         json.dumps(session.histories["assistant"]))
        kinds = [(e["kind"], e.get("thought", False)) for e in session.transcript]
        self.assertEqual(kinds, [("user", False), ("assistant", True),
                                 ("assistant", False)])
        self.assertTrue(any(e.get("type") == "assistant_reasoning"
                            for e in events))

    async def test_orphan_close_reclassifies_streamed_text(self):
        # R1-style: the reply starts inside an unopened think block.
        mcp = FakeMCP({})
        session, _ = self.make_session(mcp)
        session.agent = "assistant"
        turns = [[
            {"type": "delta", "text": "hidden reasoning"},
            {"type": "reasoning_retro"},
            {"type": "delta", "text": "Visible answer."},
            {"type": "end", "finish_reason": "stop", "tool_calls": []},
        ]]
        with patch.object(llm, "chat_stream", scripted_llm(turns)):
            session._start_response("hi")
            await session.response_task
        self.assertEqual(session.histories["assistant"][-1]["content"],
                         "Visible answer.")
        thoughts = [e for e in session.transcript if e.get("thought")]
        self.assertEqual(thoughts[0]["text"], "hidden reasoning")
        answers = [e for e in session.transcript
                   if e["kind"] == "assistant" and not e.get("thought")]
        self.assertEqual(answers[0]["text"], "Visible answer.")

    async def test_thoughts_and_reasoning_are_silent_answer_spoken(self):
        spoken: list[str] = []

        async def fake_tts(self, q, gen, stop):
            while True:
                sentence = await q.get()
                if sentence is None:
                    return
                spoken.append(sentence)

        mcp = FakeMCP({"Canvas__list_quizzes": "[1]"})
        session, _ = self.make_session(mcp)
        session.agent = "assistant"
        turns = [
            [{"type": "reasoning", "text": "silent thinking tokens"}]
            + thought_turn("Let me check the quiz list first.",
                           "Canvas__list_quizzes", {}),
            text_turn("There is one quiz in the course."),
        ]
        with patch.object(llm, "chat_stream", scripted_llm(turns)), \
             patch.object(VoiceSession, "_tts_worker", fake_tts):
            session._start_response("how many quizzes?")
            await session.response_task
        self.assertEqual(spoken, ["There is one quiz in the course."])

    async def test_subagent_reasoning_precedes_visible_thought(self):
        mcp = FakeMCP({"Canvas__list_quizzes": "[1]"})
        session, events = self.make_session(mcp)
        turns = [
            [
                {"type": "reasoning", "text": "figuring out the tool"},
                {"type": "delta", "text": "Checking quizzes."},
                {"type": "end", "finish_reason": "tool_calls",
                 "tool_calls": [{"id": "c1", "name": "Canvas__list_quizzes",
                                 "arguments": "{}"}]},
            ],
            text_turn("One quiz found."),
        ]
        with patch.object(llm, "chat_stream", scripted_llm(turns)):
            await session._run_subagent(
                {"name": "lookup", "instruction": "count quizzes",
                 "tools": ["read"]},
                gen=0, role="worker",
            )
        entry = next(e for e in session.transcript if e["kind"] == "subagent")
        self.assertEqual([ev["kind"] for ev in entry["events"]],
                         ["thought", "thought", "tool"])
        self.assertEqual(entry["events"][0]["text"], "figuring out the tool")
        self.assertEqual(entry["events"][1]["text"], "Checking quizzes.")
        self.assertTrue(any(e.get("type") == "subagent_reasoning"
                            for e in events))

    # -- streamed tool-call assembly ---------------------------------------------------

    def test_slot_index_without_provider_index(self):
        def tc(index=None, id=None, name=None, args=None):
            fn = types.SimpleNamespace(name=name, arguments=args)
            return types.SimpleNamespace(index=index, id=id, function=fn)

        pending: dict[int, dict] = {}
        # first call announces id+name -> new slot 0
        i = _slot_index(tc(id="a", name="list_quizzes"), pending)
        self.assertEqual(i, 0)
        pending[0] = {"id": "a", "name": "list_quizzes", "arguments": ""}
        # bare argument fragment continues the last slot
        self.assertEqual(_slot_index(tc(args="{}"), pending), 0)
        # a delta with a fresh id starts a new slot
        self.assertEqual(_slot_index(tc(id="b", name="get_page"), pending), 1)
        pending[1] = {"id": "b", "name": "get_page", "arguments": ""}
        # a delta re-sending a known id lands in that slot
        self.assertEqual(_slot_index(tc(id="a", args="{}"), pending), 0)
        # explicit provider index always wins
        self.assertEqual(_slot_index(tc(index=5), pending), 5)


class ReasoningFilterTests(unittest.TestCase):
    """Inline thinking-block extraction for popular open-model formats."""

    def collect(self, chunks):
        f = ReasoningFilter()
        events = []
        for chunk in chunks:
            events.extend(f.feed(chunk))
        events.extend(f.flush())
        return events

    def joined(self, events, kind):
        return "".join(e["text"] for e in events if e["type"] == kind)

    def test_think_block_single_chunk(self):
        ev = self.collect(["<think>abc</think>Hello"])
        self.assertEqual(self.joined(ev, "reasoning"), "abc")
        self.assertEqual(self.joined(ev, "delta"), "Hello")

    def test_tags_split_across_chunks(self):
        ev = self.collect(["<th", "ink>ab", "c</thi", "nk>Hi"])
        self.assertEqual(self.joined(ev, "reasoning"), "abc")
        self.assertEqual(self.joined(ev, "delta"), "Hi")

    def test_magistral_think_block(self):
        ev = self.collect(["[THINK]plan[/THINK]Answer"])
        self.assertEqual(self.joined(ev, "reasoning"), "plan")
        self.assertEqual(self.joined(ev, "delta"), "Answer")

    def test_unclosed_block_stays_reasoning(self):
        ev = self.collect(["<think>never closed, tool call next"])
        self.assertEqual(self.joined(ev, "reasoning"),
                         "never closed, tool call next")
        self.assertEqual(self.joined(ev, "delta"), "")

    def test_orphan_close_emits_retro(self):
        ev = self.collect(["already thinking</think>Answer"])
        self.assertIn("reasoning_retro", [e["type"] for e in ev])
        retro_at = [e["type"] for e in ev].index("reasoning_retro")
        self.assertEqual(self.joined(ev[:retro_at], "delta"),
                         "already thinking")
        self.assertEqual(self.joined(ev[retro_at:], "delta"), "Answer")

    def test_plain_text_with_angle_brackets_passes_through(self):
        ev = self.collect(["a < b and ", "c > d"])
        self.assertEqual(self.joined(ev, "delta"), "a < b and c > d")
        self.assertEqual(self.joined(ev, "reasoning"), "")

    def test_strip_reasoning_harmony_channels(self):
        raw = ("<|channel|>analysis<|message|>plan the steps<|end|>"
               "<|start|>assistant<|channel|>final<|message|>Hi there")
        self.assertEqual(strip_reasoning(raw), "Hi there")

    def test_strip_reasoning_orphan_close(self):
        self.assertEqual(strip_reasoning("secret</think>Answer"), "Answer")


if __name__ == "__main__":
    unittest.main()
