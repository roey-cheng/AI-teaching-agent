import unittest

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from app.agent.budget_middleware import AgentToolUseNotAllowedError
from app.agent.output import VisibleChunk, extract_visible_chunk
from app.schemas import AgentProgressData, ReasoningDeltaData


class AgentOutputTest(unittest.TestCase):
    def test_provider_reasoning_and_answer_are_separate_and_whitespace_preserved(self):
        chunk = AIMessageChunk(content="  answer\n", additional_kwargs={"reasoning_content": "thought\n"},
                               response_metadata={"finish_reason": "stop"})
        self.assertEqual(extract_visible_chunk(chunk, {"langgraph_node": "model"}),
                         VisibleChunk("thought\n", "  answer\n", "stop"))

    def test_standard_blocks_do_not_duplicate_provider_reasoning_or_expose_unknown_blocks(self):
        chunk = AIMessageChunk(content=[{"type": "reasoning", "reasoning": "thought"},
                                        {"type": "text", "text": "answer"},
                                        {"type": "secret", "text": "must not show"}],
                               additional_kwargs={"reasoning_content": "thought"})
        self.assertEqual(extract_visible_chunk(chunk, {"langgraph_node": "model"}), VisibleChunk("thought", "answer"))

    def test_only_main_ai_chunks_are_visible(self):
        for chunk, node in ((AIMessageChunk(content="private"), "tools"),
                            (HumanMessage(content="private"), "model"),
                            (ToolMessage(content="private", tool_call_id="t"), "model")):
            self.assertEqual(extract_visible_chunk(chunk, {"langgraph_node": node}), VisibleChunk())

    def test_tool_call_is_rejected(self):
        chunk = AIMessageChunk(content="", tool_call_chunks=[{"name": "task", "args": "{}", "id": "t", "index": 0}])
        with self.assertRaises(AgentToolUseNotAllowedError):
            extract_visible_chunk(chunk, {"langgraph_node": "model"})

    def test_event_schemas_reject_invented_stage_and_empty_reasoning(self):
        from pydantic import ValidationError
        from uuid import uuid4
        with self.assertRaises(ValidationError):
            AgentProgressData(attempt_id=str(uuid4()), stage="searching_internet")
        with self.assertRaises(ValidationError):
            ReasoningDeltaData(attempt_id=str(uuid4()), text="")
