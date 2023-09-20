from __future__ import annotations

import logging
import re
from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING, Any, Literal, Optional

if TYPE_CHECKING:
    from autogpt.config import AIConfig, Config
    from autogpt.llm.base import ChatModelInfo, ChatModelResponse
    from autogpt.models.command_registry import CommandRegistry

from autogpt.agents.utils.exceptions import InvalidAgentResponseError
from autogpt.config.ai_directives import AIDirectives
from autogpt.llm.base import ChatSequence, Message
from autogpt.llm.providers.openai import OPEN_AI_CHAT_MODELS, get_openai_command_specs
from autogpt.llm.utils import count_message_tokens, create_chat_completion
from autogpt.models.action_history import EpisodicActionHistory, ActionResult
from autogpt.prompts.generator import PromptGenerator
from autogpt.prompts.prompt import DEFAULT_TRIGGERING_PROMPT

logger = logging.getLogger(__name__)

CommandName = str
CommandArgs = dict[str, str]
AgentThoughts = dict[str, Any]


class BaseAgent(metaclass=ABCMeta):
    """Base class for all Auto-GPT agents."""

    ThoughtProcessID = Literal["one-shot"]
    ThoughtProcessOutput = tuple[CommandName, CommandArgs, AgentThoughts]

    def __init__(
        self,
        ai_config: AIConfig,
        command_registry: CommandRegistry,
        config: Config,
        big_brain: bool = True,
        default_cycle_instruction: str = DEFAULT_TRIGGERING_PROMPT,
        cycle_budget: Optional[int] = 1,
        send_token_limit: Optional[int] = None,
        summary_max_tlength: Optional[int] = None,
    ):
        self.ai_config = ai_config
        """The AIConfig or "personality" object associated with this agent."""

        self.command_registry = command_registry
        """The registry containing all commands available to the agent."""

        self.prompt_generator = PromptGenerator(
            ai_config=ai_config,
            ai_directives=AIDirectives.from_file(config.prompt_settings_file),
            command_registry=command_registry,
        )
        """The prompt generator used for generating the system prompt."""

        self.config = config
        """The applicable application configuration."""

        self.big_brain = big_brain
        """
        Whether this agent uses the configured smart LLM (default) to think,
        as opposed to the configured fast LLM.
        """

        self.default_cycle_instruction = default_cycle_instruction
        """The default instruction passed to the AI for a thinking cycle."""

        self.cycle_budget = cycle_budget
        """
        The number of cycles that the agent is allowed to run unsupervised.

        `None` for unlimited continuous execution,
        `1` to require user approval for every step,
        `0` to stop the agent.
        """

        self.cycles_remaining = cycle_budget
        """The number of cycles remaining within the `cycle_budget`."""

        self.cycle_count = 0
        """The number of cycles that the agent has run since its initialization."""

        self.send_token_limit = send_token_limit or self.llm.max_tokens * 3 // 4
        """
        The token limit for prompt construction. Should leave room for the completion;
        defaults to 75% of `llm.max_tokens`.
        """

        self.event_history = EpisodicActionHistory()

        # Support multi-inheritance and mixins for subclasses
        super(BaseAgent, self).__init__()

    @property
    def system_prompt(self) -> str:
        """
        The system prompt sets up the AI's personality and explains its goals,
        available resources, and restrictions.
        """
        return self.prompt_generator.construct_system_prompt(self)

    @property
    def llm(self) -> ChatModelInfo:
        """The LLM that the agent uses to think."""
        llm_name = self.config.smart_llm if self.big_brain else self.config.fast_llm
        return OPEN_AI_CHAT_MODELS[llm_name]

    def think(
        self,
        instruction: Optional[str] = None,
        thought_process_id: ThoughtProcessID = "one-shot",
    ) -> ThoughtProcessOutput:
        """Runs the agent for one cycle.

        Params:
            instruction: The instruction to put at the end of the prompt.

        Returns:
            The command name and arguments, if any, and the agent's thoughts.
        """

        instruction = instruction or self.default_cycle_instruction

        prompt: ChatSequence = self.construct_prompt(instruction, thought_process_id)
        prompt = self.on_before_think(prompt, thought_process_id, instruction)
        raw_response = create_chat_completion(
            prompt,
            self.config,
            functions=get_openai_command_specs(self.command_registry)
            if self.config.openai_functions
            else None,
        )
        self.cycle_count += 1

        return self.on_response(raw_response, thought_process_id, prompt, instruction)

    @abstractmethod
    def execute(
        self,
        command_name: str,
        command_args: dict[str, str] = {},
        user_input: str = "",
    ) -> ActionResult:
        """Executes the given command, if any, and returns the agent's response.

        Params:
            command_name: The name of the command to execute, if any.
            command_args: The arguments to pass to the command, if any.
            user_input: The user's input, if any.

        Returns:
            The results of the command.
        """
        ...

    def construct_base_prompt(
        self,
        thought_process_id: ThoughtProcessID,
        prepend_messages: list[Message] = [],
        append_messages: list[Message] = [],
        reserve_tokens: int = 0,
    ) -> ChatSequence:
        """Constructs and returns a prompt with the following structure:
        1. System prompt
        2. `prepend_messages`
        3. Message history of the agent, truncated & prepended with running summary as needed
        4. `append_messages`

        Params:
            prepend_messages: Messages to insert between the system prompt and message history
            append_messages: Messages to insert after the message history
            reserve_tokens: Number of tokens to reserve for content that is added later
        """

        if self.event_history:
            prepend_messages.insert(
                0,
                Message(
                    "system",
                    "## Progress\n\n" f"{self.event_history.fmt_paragraph()}",
                ),
            )

        prompt = ChatSequence.for_model(
            self.llm.name,
            [Message("system", self.system_prompt)] + prepend_messages,
        )

        if append_messages:
            prompt.extend(append_messages)

        return prompt

    def construct_prompt(
        self,
        cycle_instruction: str,
        thought_process_id: ThoughtProcessID,
    ) -> ChatSequence:
        """Constructs and returns a prompt with the following structure:
        1. System prompt
        2. Message history of the agent, truncated & prepended with running summary as needed
        3. `cycle_instruction`

        Params:
            cycle_instruction: The final instruction for a thinking cycle
        """

        if not cycle_instruction:
            raise ValueError("No instruction given")

        cycle_instruction_msg = Message("user", cycle_instruction)
        cycle_instruction_tlength = count_message_tokens(
            cycle_instruction_msg, self.llm.name
        )

        append_messages: list[Message] = []

        response_format_instr = self.response_format_instruction(thought_process_id)
        if response_format_instr:
            append_messages.append(Message("system", response_format_instr))

        prompt = self.construct_base_prompt(
            thought_process_id,
            append_messages=append_messages,
            reserve_tokens=cycle_instruction_tlength,
        )

        # ADD user input message ("triggering prompt")
        prompt.append(cycle_instruction_msg)

        return prompt

    # This can be expanded to support multiple types of (inter)actions within an agent
    def response_format_instruction(self, thought_process_id: ThoughtProcessID) -> str:
        if thought_process_id != "one-shot":
            raise NotImplementedError(f"Unknown thought process '{thought_process_id}'")

        RESPONSE_FORMAT_WITH_COMMAND = """```ts
        interface Response {
            thoughts: {
                // Thoughts
                text: string;
                reasoning: string;
                // Short markdown-style bullet list that conveys the long-term plan
                plan: string;
                // Constructive self-criticism
                criticism: string;
                // Summary of thoughts to say to the user
                speak: string;
            };
            command: {
                name: string;
                args: Record<string, any>;
            };
        }
        ```"""

        RESPONSE_FORMAT_WITHOUT_COMMAND = """```ts
        interface Response {
            thoughts: {
                // Thoughts
                text: string;
                reasoning: string;
                // Short markdown-style bullet list that conveys the long-term plan
                plan: string;
                // Constructive self-criticism
                criticism: string;
                // Summary of thoughts to say to the user
                speak: string;
            };
        }
        ```"""

        response_format = re.sub(
            r"\n\s+",
            "\n",
            RESPONSE_FORMAT_WITHOUT_COMMAND
            if self.config.openai_functions
            else RESPONSE_FORMAT_WITH_COMMAND,
        )

        use_functions = self.config.openai_functions and self.command_registry.commands
        return (
            f"Respond strictly with JSON{', and also specify a command to use through a function_call' if use_functions else ''}. "
            "The JSON should be compatible with the TypeScript type `Response` from the following:\n"
            f"{response_format}"
        )

    def on_before_think(
        self,
        prompt: ChatSequence,
        thought_process_id: ThoughtProcessID,
        instruction: str,
    ) -> ChatSequence:
        """Called after constructing the prompt but before executing it.

        Calls the `on_planning` hook of any enabled and capable plugins, adding their
        output to the prompt.

        Params:
            instruction: The instruction for the current cycle, also used in constructing the prompt

        Returns:
            The prompt to execute
        """
        current_tokens_used = prompt.token_length
        plugin_count = len(self.config.plugins)
        for i, plugin in enumerate(self.config.plugins):
            if not plugin.can_handle_on_planning():
                continue
            plugin_response = plugin.on_planning(self.prompt_generator, prompt.raw())
            if not plugin_response or plugin_response == "":
                continue
            message_to_add = Message("system", plugin_response)
            tokens_to_add = count_message_tokens(message_to_add, self.llm.name)
            if current_tokens_used + tokens_to_add > self.send_token_limit:
                logger.debug(f"Plugin response too long, skipping: {plugin_response}")
                logger.debug(f"Plugins remaining at stop: {plugin_count - i}")
                break
            prompt.insert(
                -1, message_to_add
            )  # HACK: assumes cycle instruction to be at the end
            current_tokens_used += tokens_to_add
        return prompt

    def on_response(
        self,
        llm_response: ChatModelResponse,
        thought_process_id: ThoughtProcessID,
        prompt: ChatSequence,
        instruction: str,
    ) -> ThoughtProcessOutput:
        """Called upon receiving a response from the chat model.

        Adds the last/newest message in the prompt and the response to `history`,
        and calls `self.parse_and_process_response()` to do the rest.

        Params:
            llm_response: The raw response from the chat model
            prompt: The prompt that was executed
            instruction: The instruction for the current cycle, also used in constructing the prompt

        Returns:
            The parsed command name and command args, if any, and the agent thoughts.
        """

        return self.parse_and_process_response(
            llm_response, thought_process_id, prompt, instruction
        )

        # TODO: update memory/context

    @abstractmethod
    def parse_and_process_response(
        self,
        llm_response: ChatModelResponse,
        thought_process_id: ThoughtProcessID,
        prompt: ChatSequence,
        instruction: str,
    ) -> ThoughtProcessOutput:
        """Validate, parse & process the LLM's response.

        Must be implemented by derivative classes: no base implementation is provided,
        since the implementation depends on the role of the derivative Agent.

        Params:
            llm_response: The raw response from the chat model
            prompt: The prompt that was executed
            instruction: The instruction for the current cycle, also used in constructing the prompt

        Returns:
            The parsed command name and command args, if any, and the agent thoughts.
        """
        pass
