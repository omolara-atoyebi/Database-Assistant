"""The only module that talks to the Anthropic API.

Pipeline code depends on the small LLMClient interface (complete), not on the
Anthropic SDK, so the model or provider can be swapped here alone. Tests use a
FakeLLM with the same interface (tests/conftest.py).

Check your API key works:  python -m db_assistant.llm.client
"""

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
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_config={"effort": self.effort},
        )
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMRefusal(f"The model declined the request (category: {category}).")
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
