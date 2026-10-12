"""Preserve actual provider reasoning when serializing tool-enabled conversations."""

from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek


class ProtocolChatDeepSeek(ChatDeepSeek):
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if payload.get("tools"):
            if len(messages) != len(payload["messages"]):
                raise ValueError("Provider message mapping changed")
            for original, wire in zip(messages, payload["messages"]):
                if isinstance(original, AIMessage):
                    reasoning = original.additional_kwargs.get("reasoning_content")
                    if not isinstance(reasoning, str):
                        raise TypeError("Missing recorded provider reasoning")
                    wire["reasoning_content"] = reasoning
        return payload
