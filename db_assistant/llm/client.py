"""The only module that talks to the runtime LLM.

Pipeline code depends on the small LLMClient interface (complete, complete_json,
complete_with_tools), not on the Anthropic SDK, so the model or provider can
be swapped here alone. Tests use a FakeLLM with the same interface
(tests/conftest.py).

Two providers, chosen in .env (see config.py), both through the Anthropic SDK:
- anthropic: Claude via the Anthropic API.
- ollama: a local model via Ollama's Anthropic-compatible API, fully offline.
  Ollama accepts but ignores some features, so this module works around them:
  - output_config.format (structured outputs): the JSON schema goes into the
    system prompt instead, and the reply is parsed tolerantly and checked
    (parse_json_reply), with one retry.
  - tool_choice "none": a tool call past the limit is not run; the model gets
    an error tool result telling it to reply, and one more round to do so.
  - output_config.effort: thinking is switched on or off per call instead
    (the `think` argument). Qwen3 thinks by default, which is slow.
  - Context: Ollama cuts long prompts off silently, so the model's num_ctx is
    checked at startup and each prompt's size is checked before sending.

Failures are raised as this module's own exceptions (below), never as
Anthropic SDK exceptions, so callers don't depend on the SDK either.

Check the LLM works:  python -m db_assistant.llm.client
"""

import json
import re
import urllib.error
import urllib.request

import anthropic

from db_assistant import config


class LLMError(Exception):
    """Base class for LLM failures."""


class LLMNotConfigured(LLMError):
    """No API key is set, or the API rejected the key (anthropic)."""


class LLMUnavailable(LLMError):
    """The API could not be reached or returned an error."""


class LLMServerDown(LLMUnavailable):
    """The Ollama server isn't running at the configured URL."""


class LLMModelMissing(LLMError):
    """The configured model isn't installed in Ollama."""


class LLMContextTooSmall(LLMError):
    """The model's context window is too small for the prompt (ollama)."""


class LLMRefusal(LLMError):
    """The model declined the request (stop_reason "refusal")."""


class LLMFormatError(LLMError):
    """The reply couldn't be used: not the requested JSON, cut off, or tools misused."""


# A wrong reply gets this many retries, with the problem sent back to the model.
FORMAT_RETRIES = 1

# Rough size check for prompts sent to Ollama. qwen3 averages about 3.5
# characters per token on our prompts; 2.5 leaves a safety margin.
CHARS_PER_TOKEN = 2.5

# Ollama's num_ctx, as /api/show lists it in the model's parameters.
NUM_CTX = re.compile(r"^num_ctx\s+(\d+)", re.MULTILINE)


class JSONReplyError(ValueError):
    """A reply that isn't the requested JSON. The message says what is wrong."""


def parse_json_reply(text, schema):
    """Return the first JSON object in `text` that matches `schema`.

    Tolerant of extra text around the JSON: thinking tags, ```json fences,
    prose before or after. Raises JSONReplyError naming what is wrong.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    decoder = json.JSONDecoder()
    first_problem = None
    for start in [match.start() for match in re.finditer(r"\{", text)]:
        try:
            value, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        problem = schema_problem(value, schema)
        if problem is None:
            return value
        first_problem = first_problem or problem
    if first_problem:
        raise JSONReplyError(first_problem)
    raise JSONReplyError("The reply contains no JSON object. Reply with only a JSON object.")


def schema_problem(value, schema):
    """Check a dict against our simple schemas (required keys, types, enums).

    Returns None if it matches, else a message naming what failed and what is allowed.
    """
    types = {"string": str, "boolean": bool, "array": list, "object": dict, "integer": int, "number": (int, float)}
    properties = schema.get("properties", {})
    missing = [key for key in schema.get("required", []) if key not in value]
    if missing:
        return f"The JSON object is missing the key(s) {', '.join(missing)}. Required keys: {', '.join(schema['required'])}."
    for key, spec in properties.items():
        if key not in value:
            continue
        expected = types.get(spec.get("type"))
        # bool is a subclass of int in Python: don't accept true as a number.
        wrong_bool = isinstance(value[key], bool) and spec.get("type") in ("integer", "number")
        if expected and (not isinstance(value[key], expected) or wrong_bool):
            return f"'{key}' must be a JSON {spec['type']}, got {json.dumps(value[key])}."
        if "enum" in spec and value[key] not in spec["enum"]:
            return f"'{key}' must be one of {', '.join(spec['enum'])}, got {json.dumps(value[key])}."
    return None


def json_instruction(schema):
    return (
        "Your final reply (not a tool call) must be only a JSON object, with no "
        "other text, matching this JSON schema:\n" + json.dumps(schema)
    )


def fetch_ollama_model(base_url, model):
    """Ollama's /api/show for one model (a native Ollama endpoint, local)."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/show",
        data=json.dumps({"model": model}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def server_down_message(base_url):
    return (
        f"The Ollama server isn't running at {base_url}. "
        "Start the Ollama app (or run `ollama serve`), then try again."
    )


def model_missing_message(model):
    return (
        f"The model '{model}' isn't installed in Ollama. Install it with "
        "`ollama pull qwen3:14b` and `ollama create qwen3-14b-16k -f ollama/Modelfile` "
        "(or set LLM_MODEL in .env to a model you have; see `ollama list`)."
    )


class LLMClient:
    """provider, base_url, api_key, model, max_tokens default to config.py (so to .env)."""

    def __init__(self, provider=None, base_url=None, api_key=None, model=None,
                 effort=config.EFFORT, max_tokens=None):
        self.provider = provider or config.LLM_PROVIDER
        self.base_url = base_url or config.LLM_BASE_URL
        self.model = model or config.MODEL
        self.effort = effort
        self.max_tokens = max_tokens or config.MAX_TOKENS
        api_key = api_key if api_key is not None else config.LLM_API_KEY
        if self.provider == config.OLLAMA:
            # Fail now, with a clear message, rather than at the first question.
            self.context = self._check_ollama_model()
        elif not api_key:
            # Without a key the SDK only fails at the first request, with a bare TypeError.
            raise LLMNotConfigured(
                "ANTHROPIC_API_KEY is not set. Add it to the .env file in the "
                "project root (see .env.example)."
            )
        self.client = anthropic.Anthropic(api_key=api_key, base_url=self.base_url)

    @property
    def uses_ollama(self):
        return self.provider == config.OLLAMA

    def complete(self, system, user_message, think=True):
        """Send one system prompt + user message; return the reply text.

        think: on Ollama, False switches thinking off (faster). On Anthropic
        thinking stays at the model default (adaptive) and effort is set
        explicitly, see config.EFFORT.
        """
        response = self._create(system, [{"role": "user", "content": user_message}], think=think)
        return self._text(response)

    def complete_json(self, system, user_message, schema, think=False):
        """Like complete, but the reply is JSON matching `schema`; return it as a dict.

        Anthropic: structured outputs (output_config.format) guarantee the
        shape. Ollama: the schema goes in the prompt and the reply is checked.
        Either way, a reply that doesn't match gets FORMAT_RETRIES retries,
        then LLMFormatError. Thinking is off by default: these are quick judgments.
        """
        reply, _ = self.complete_with_tools(system, user_message, [], None, schema, 0, think=think)
        return reply

    def complete_with_tools(self, system, user_message, tools, call_tool, schema, max_tool_calls, think=True):
        """Like complete_json, but the model may call tools before it replies.

        tools: [{"name", "description", "input_schema"}].
        call_tool(name, input) runs one call and returns {"content": str, "is_error": bool}.
        After max_tool_calls calls, tools are switched off (tool_choice none);
        Ollama ignores that, so a call past the limit is answered with an
        error instead of being run.

        Returns (reply dict, tool_calls), where tool_calls lists every call as
        {"name", "input", "is_error"}.
        """
        messages = [{"role": "user", "content": user_message}]
        tool_calls = []
        rounds_past_limit = 0
        retries = 0
        while True:
            extra = {}
            if tools:
                extra = {
                    "tools": tools,
                    "tool_choice": {"type": "none" if len(tool_calls) >= max_tool_calls else "auto"},
                }
            response = self._create(system, messages, schema=schema, think=think, **extra)

            if response.stop_reason == "tool_use":
                # Send back the whole reply (it can include thinking blocks), then
                # one tool_result per tool_use block, all in a single user message.
                messages.append({"role": "assistant", "content": response.content})
                results, past_limit = [], False
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    if len(tool_calls) >= max_tool_calls:
                        past_limit = True
                        outcome = {
                            "content": (
                                f"Tool call limit reached ({max_tool_calls}): {block.name} was not run. "
                                "Do not call tools again; reply now with the JSON object."
                            ),
                            "is_error": True,
                        }
                    else:
                        outcome = call_tool(block.name, block.input)
                        tool_calls.append({"name": block.name, "input": block.input, "is_error": outcome["is_error"]})
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": outcome["content"],
                        "is_error": outcome["is_error"],
                    })
                messages.append({"role": "user", "content": results})
                rounds_past_limit += past_limit
                if rounds_past_limit > FORMAT_RETRIES:
                    raise LLMFormatError(
                        f"The model kept calling tools after the limit of {max_tool_calls} "
                        "instead of replying with JSON."
                    )
                continue

            text = self._text(response)
            try:
                return parse_json_reply(text, schema), tool_calls
            except JSONReplyError as error:
                if retries >= FORMAT_RETRIES:
                    raise LLMFormatError(
                        f"The model's reply wasn't the requested JSON after {retries + 1} tries: "
                        f"{error} Reply started: {text[:200]!r}"
                    ) from error
                retries += 1
                messages.append({"role": "assistant", "content": response.content})
                messages.append({
                    "role": "user",
                    "content": f"Your reply could not be used: {error} Reply again with only the JSON object.",
                })

    def _create(self, system, messages, schema=None, think=True, **extra):
        if self.uses_ollama:
            if schema is not None:
                system = f"{system}\n\n{json_instruction(schema)}"
            if not think:
                extra["thinking"] = {"type": "disabled"}
            self._check_fits(system, messages, extra.get("tools"))
        else:
            output_config = {"effort": self.effort}
            if schema is not None:
                output_config["format"] = {"type": "json_schema", "schema": schema}
            extra["output_config"] = output_config

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=messages,
                **extra,
            )
        except anthropic.AuthenticationError as error:
            raise LLMNotConfigured(
                "The API rejected the ANTHROPIC_API_KEY in .env. Check that the key is valid."
            ) from error
        except anthropic.APIConnectionError as error:
            if self.uses_ollama:
                raise LLMServerDown(server_down_message(self.base_url)) from error
            raise LLMUnavailable("Could not reach the Anthropic API. Check your network connection.") from error
        except anthropic.NotFoundError as error:
            if self.uses_ollama:
                raise LLMModelMissing(model_missing_message(self.model)) from error
            raise LLMUnavailable(f"The Anthropic API returned an error ({error.status_code}): {error.message}") from error
        except anthropic.APIStatusError as error:
            raise LLMUnavailable(f"The LLM API returned an error ({error.status_code}): {error.message}") from error

        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMRefusal(f"The model declined the request (category: {category}).")
        if response.stop_reason == "max_tokens":
            raise LLMFormatError(
                f"The model's reply was cut off at max_tokens={self.max_tokens} "
                "(MAX_TOKENS in config.py), usually by long thinking."
            )
        return response

    def _check_ollama_model(self):
        """Return the model's num_ctx. Raise if the server is down, the model
        isn't installed, or its context is below config.OLLAMA_MIN_CONTEXT."""
        try:
            details = fetch_ollama_model(self.base_url, self.model)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise LLMModelMissing(model_missing_message(self.model)) from error
            raise LLMUnavailable(f"Ollama returned an error ({error.code}) for model '{self.model}'.") from error
        except (urllib.error.URLError, OSError) as error:
            raise LLMServerDown(server_down_message(self.base_url)) from error

        match = NUM_CTX.search(details.get("parameters") or "")
        context = int(match.group(1)) if match else 4096  # Ollama's default
        if context < config.OLLAMA_MIN_CONTEXT:
            raise LLMContextTooSmall(
                f"The model '{self.model}' has a context of {context} tokens; at least "
                f"{config.OLLAMA_MIN_CONTEXT} is needed or long prompts are cut off silently. "
                "Build the model with `ollama create qwen3-14b-16k -f ollama/Modelfile` "
                "and set LLM_MODEL=qwen3-14b-16k."
            )
        return context

    def _check_fits(self, system, messages, tools):
        """Refuse a request that might not fit Ollama's context, instead of
        letting Ollama cut it off silently. A rough, cautious estimate."""
        size = len(system) + len(json.dumps(messages, default=str)) + len(json.dumps(tools or []))
        estimate = int(size / CHARS_PER_TOKEN)
        if estimate + self.max_tokens > self.context:
            raise LLMContextTooSmall(
                f"The prompt is about {estimate} tokens; with up to {self.max_tokens} for the reply "
                f"it might not fit the model's context of {self.context} tokens. Raise num_ctx "
                "in ollama/Modelfile (and OLLAMA_MIN_CONTEXT in config.py) and rebuild the model."
            )

    @staticmethod
    def _text(response):
        # The reply can include thinking blocks; keep only the text.
        return "".join(block.text for block in response.content if block.type == "text")


if __name__ == "__main__":
    llm = LLMClient()
    reply = llm.complete(
        system="You are a connectivity check.",
        user_message="Reply with exactly one word: hello",
        think=False,
    )
    print(f"provider: {llm.provider}")
    print(f"model:    {llm.model}")
    if llm.uses_ollama:
        print(f"context:  {llm.context}")
    print(f"reply:    {reply}")
