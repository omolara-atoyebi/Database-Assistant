"""The LLM client's provider handling, offline (no API calls, no Ollama needed).

A scripted stand-in replaces the Anthropic SDK client, so these tests check
what the client sends and how it handles replies:
- tolerant JSON parsing, with one retry when a reply doesn't match the schema
- the tool-call limit, which Ollama's compat API doesn't enforce itself
- Ollama: JSON schema in the prompt, thinking off/on, the context checks
- Anthropic: structured outputs and effort, as before
"""

from types import SimpleNamespace

import pytest

from db_assistant import config
from db_assistant.llm import client as llm_client
from db_assistant.llm.client import (
    JSONReplyError,
    LLMClient,
    LLMContextTooSmall,
    LLMFormatError,
    parse_json_reply,
)
from db_assistant.pipeline.scope import SCHEMA as SCOPE_SCHEMA

IN_SCOPE = '{"decision": "in_scope", "reason": "About orders.", "message_to_user": "", "suggestions": []}'
TOOL = {"name": "describe_table", "description": "Describe a table.", "input_schema": {"type": "object"}}


# --- Scripted SDK responses ---------------------------------------------------------

def text_reply(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def tool_reply(table, block_id="call_1"):
    block = SimpleNamespace(type="tool_use", id=block_id, name="describe_table", input={"table_name": table})
    return SimpleNamespace(stop_reason="tool_use", content=[block])


class ScriptedMessages:
    """Stands in for anthropic.Anthropic().messages; records every request."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def create(self, **request):
        # Copy the list: the client appends to the same messages list later.
        self.requests.append({**request, "messages": list(request["messages"])})
        return self.replies.pop(0)


def make_client(provider, replies, monkeypatch, context=16384):
    if provider == config.OLLAMA:
        monkeypatch.setattr(
            llm_client, "fetch_ollama_model", lambda base_url, model: {"parameters": f"num_ctx    {context}"}
        )
    llm = LLMClient(provider=provider, base_url="http://localhost:11434", api_key="test", model="m", max_tokens=1000)
    llm.client = SimpleNamespace(messages=ScriptedMessages(replies))
    return llm


def requests(llm):
    return llm.client.messages.requests


# --- parse_json_reply ---------------------------------------------------------------

@pytest.mark.parametrize(
    "reply",
    [
        IN_SCOPE,
        f"```json\n{IN_SCOPE}\n```",
        f"Here is my classification:\n{IN_SCOPE}\nHope that helps!",
        f"<think>Is it {{about}} the data? Yes.</think>\n{IN_SCOPE}",
        f'Example: {{"decision": "maybe"}}. Answer: {IN_SCOPE}',  # first object is wrong, second is right
    ],
    ids=["bare", "fenced", "prose-around", "think-tags", "first-object-invalid"],
)
def test_parse_json_reply_tolerates_text_around_the_json(reply):
    assert parse_json_reply(reply, SCOPE_SCHEMA)["decision"] == "in_scope"


@pytest.mark.parametrize(
    "reply, message",
    [
        ("I think it's in scope.", "contains no JSON object"),
        ('{"decision": "in_scope"}', "missing the key(s) reason, message_to_user, suggestions"),
        ('{"decision": "maybe", "reason": "", "message_to_user": "", "suggestions": []}',
         "'decision' must be one of in_scope, out_of_scope, sensitive"),
        ('{"decision": "in_scope", "reason": "", "message_to_user": "", "suggestions": "none"}',
         "'suggestions' must be a JSON array"),
    ],
    ids=["no-json", "missing-keys", "bad-enum", "wrong-type"],
)
def test_parse_json_reply_names_what_is_wrong_and_what_is_allowed(reply, message):
    with pytest.raises(JSONReplyError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_json_reply(reply, SCOPE_SCHEMA)


def test_parse_json_reply_does_not_accept_true_as_a_number():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    with pytest.raises(JSONReplyError, match="'n' must be a JSON integer"):
        parse_json_reply('{"n": true}', schema)


# --- Ollama -------------------------------------------------------------------------

def test_ollama_json_call_puts_the_schema_in_the_prompt_and_turns_thinking_off(monkeypatch):
    llm = make_client(config.OLLAMA, [text_reply(IN_SCOPE)], monkeypatch)

    assert llm.complete_json("Classify.", "How many orders?", SCOPE_SCHEMA)["decision"] == "in_scope"

    request = requests(llm)[0]
    assert '"enum": ["in_scope", "out_of_scope", "sensitive"]' in request["system"]
    assert request["thinking"] == {"type": "disabled"}
    assert "output_config" not in request  # Ollama ignores it, so it isn't sent


def test_ollama_complete_thinks_unless_told_not_to(monkeypatch):
    llm = make_client(config.OLLAMA, [text_reply("SELECT 1"), text_reply("One.")], monkeypatch)
    llm.complete("Write SQL.", "q")
    llm.complete("Answer.", "q", think=False)
    assert "thinking" not in requests(llm)[0]
    assert requests(llm)[1]["thinking"] == {"type": "disabled"}


def test_a_reply_that_is_not_the_json_is_retried_once_with_the_problem(monkeypatch):
    llm = make_client(config.OLLAMA, [text_reply("It's in scope."), text_reply(IN_SCOPE)], monkeypatch)

    assert llm.complete_json("Classify.", "q", SCOPE_SCHEMA)["decision"] == "in_scope"

    retry = requests(llm)[1]["messages"]
    assert retry[1]["role"] == "assistant"
    assert "contains no JSON object" in retry[2]["content"]


def test_a_second_bad_reply_is_a_format_error(monkeypatch):
    llm = make_client(config.OLLAMA, [text_reply("No."), text_reply("Still no.")], monkeypatch)
    with pytest.raises(LLMFormatError, match="after 2 tries"):
        llm.complete_json("Classify.", "q", SCOPE_SCHEMA)


def test_a_reply_cut_off_at_max_tokens_is_a_format_error(monkeypatch):
    llm = make_client(config.OLLAMA, [text_reply('{"decision": "in_', stop_reason="max_tokens")], monkeypatch)
    with pytest.raises(LLMFormatError, match="cut off at max_tokens=1000"):
        llm.complete_json("Classify.", "q", SCOPE_SCHEMA)


def test_a_tool_call_past_the_limit_is_not_run_and_the_model_is_told_to_reply(monkeypatch):
    schema = {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    replies = [tool_reply("orders", "a"), tool_reply("orders", "b"), text_reply('{"sql": "SELECT 1"}')]
    llm = make_client(config.OLLAMA, replies, monkeypatch)
    ran = []

    def call_tool(name, arguments):
        ran.append(arguments["table_name"])
        return {"content": "{}", "is_error": False}

    reply, tool_calls = llm.complete_with_tools("Fix.", "q", [TOOL], call_tool, schema, max_tool_calls=1)

    assert reply == {"sql": "SELECT 1"}
    assert ran == ["orders"]  # the second call was not run
    assert len(tool_calls) == 1
    refused = requests(llm)[2]["messages"][-1]["content"][0]
    assert refused["is_error"] is True
    assert "Tool call limit reached (1)" in refused["content"]
    assert requests(llm)[1]["tool_choice"] == {"type": "none"}  # still sent, for Claude


def test_a_model_that_keeps_calling_tools_past_the_limit_is_a_format_error(monkeypatch):
    replies = [tool_reply("orders", "a"), tool_reply("orders", "b"), tool_reply("orders", "c")]
    llm = make_client(config.OLLAMA, replies, monkeypatch)
    call_tool = lambda name, arguments: {"content": "{}", "is_error": False}  # noqa: E731
    with pytest.raises(LLMFormatError, match="kept calling tools"):
        llm.complete_with_tools("Fix.", "q", [TOOL], call_tool, SCOPE_SCHEMA, max_tool_calls=1)


def test_startup_rejects_a_model_with_the_default_4096_context(monkeypatch):
    monkeypatch.setattr(llm_client, "fetch_ollama_model", lambda base_url, model: {"parameters": "top_k 20"})
    with pytest.raises(LLMContextTooSmall, match="context of 4096 tokens; at least 16384"):
        LLMClient(provider=config.OLLAMA, model="qwen3:14b")


def test_a_prompt_that_might_not_fit_the_context_is_refused_not_cut_off(monkeypatch):
    llm = make_client(config.OLLAMA, [], monkeypatch, context=16384)
    with pytest.raises(LLMContextTooSmall, match="might not fit"):
        llm.complete("x" * 50_000, "q")  # about 20,000 tokens
    assert requests(llm) == []  # nothing was sent


# --- Anthropic ----------------------------------------------------------------------

def test_anthropic_json_call_uses_structured_outputs_and_effort(monkeypatch):
    llm = make_client(config.ANTHROPIC, [text_reply(IN_SCOPE)], monkeypatch)

    llm.complete_json("Classify.", "q", SCOPE_SCHEMA)

    request = requests(llm)[0]
    assert request["output_config"] == {
        "effort": config.EFFORT,
        "format": {"type": "json_schema", "schema": SCOPE_SCHEMA},
    }
    assert request["system"] == "Classify."  # no schema text in the prompt
    assert "thinking" not in request  # left at the model default
