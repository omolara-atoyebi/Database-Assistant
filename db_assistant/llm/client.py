"""The only module that talks to the Anthropic API.

Pipeline code depends on the small LLMClient interface (complete, complete_json,
complete_with_tools), not on the
Anthropic SDK, so the model or provider can be swapped here alone. Tests use a
FakeLLM with the same interface (tests/conftest.py).

Check your API key works:  python -m db_assistant.llm.client
"""

import json

import anthropic
from dotenv import load_dotenv

from db_assistant import config


class LLMRefusal(Exception):
    """The model declined the request (stop_reason "refusal")."""


class LLMClient:
    def __init__(self, model=config.MODEL, effort=config.EFFORT, max_tokens=config.MAX_TOKENS):
        load_dotenv(config.PROJECT_ROOT / ".env")
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def complete(self, system, user_message):
        """Send one system prompt + user message; return the reply text.

        Thinking is left at the model default (adaptive). Effort is set
        explicitly, see config.EFFORT.
        """
        return self._send(system, user_message, {"effort": self.effort})

    def complete_json(self, system, user_message, schema):
        """Like complete, but the reply is JSON matching `schema`; return it as a dict.

        Uses structured outputs (output_config.format), so the API guarantees
        the reply parses and has the required fields.
        """
        output_config = {
            "effort": self.effort,
            "format": {"type": "json_schema", "schema": schema},
        }
        return json.loads(self._send(system, user_message, output_config))

    def complete_with_tools(self, system, user_message, tools, call_tool, schema, max_tool_calls):
        """Like complete_json, but the model may call tools before it replies.

        tools: [{"name", "description", "input_schema"}].
        call_tool(name, input) runs one call and returns {"content": str, "is_error": bool}.
        After max_tool_calls calls, tools are switched off so the model must reply.

        Returns (reply dict, tool_calls), where tool_calls lists every call as
        {"name", "input", "is_error"}.
        """
        messages = [{"role": "user", "content": user_message}]
        output_config = {
            "effort": self.effort,
            "format": {"type": "json_schema", "schema": schema},
        }
        tool_calls = []
        while True:
            tool_choice = {"type": "none" if len(tool_calls) >= max_tool_calls else "auto"}
            response = self._create(system, messages, output_config, tools=tools, tool_choice=tool_choice)
            if response.stop_reason != "tool_use":
                return json.loads(self._text(response)), tool_calls

            # Send back the whole reply (it can include thinking blocks), then
            # one tool_result per tool_use block, all in a single user message.
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                outcome = call_tool(block.name, block.input)
                tool_calls.append({"name": block.name, "input": block.input, "is_error": outcome["is_error"]})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": outcome["content"],
                    "is_error": outcome["is_error"],
                })
            messages.append({"role": "user", "content": results})

    def _send(self, system, user_message, output_config):
        response = self._create(system, [{"role": "user", "content": user_message}], output_config)
        return self._text(response)

    def _create(self, system, messages, output_config, **extra):
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            output_config=output_config,
            **extra,
        )
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMRefusal(f"The model declined the request (category: {category}).")
        return response

    @staticmethod
    def _text(response):
        # The reply can include thinking blocks; keep only the text.
        return "".join(block.text for block in response.content if block.type == "text")


if __name__ == "__main__":
    llm = LLMClient()
    reply = llm.complete(
        system="You are a connectivity check.",
        user_message="Reply with exactly one word: hello",
    )
    print(f"model:  {llm.model}")
    print(f"effort: {llm.effort}")
    print(f"reply:  {reply}")
