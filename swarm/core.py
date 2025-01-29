import copy
import json
from collections import defaultdict
from typing import List

from openai import OpenAI
from .util import function_to_json, debug_print, merge_chunk
from .types import (
    Agent,
    AgentFunction,
    ChatCompletionMessage,
    ChatCompletionMessageToolCall,
    Function,
    Response,
    Result,
)

__CTX_VARS_NAME__ = "context_variables"

class Swarm():
    def __init__(self, client=None, mode: str = "",  GEMINI_API_KEY:str = ""):
        if not mode:
            raise ValueError("Please set mode (e.g., openai, gemini, ollama).")
        
        self.mode = mode # set mode
        if not client:
            if self.mode == "ollama":
                client = OpenAI(
                api_key='ollama', # required, but unused
                base_url = 'http://localhost:11434/v1', # default ollama api
            )
                
            elif self.mode == "gemini":
                if GEMINI_API_KEY:
                    client = OpenAI(
                        api_key=GEMINI_API_KEY, # required
                        base_url="https://generativelanguage.googleapis.com/v1beta/openai/" # default gemini api
                    )
                else:
                    raise ValueError("Please provide GEMINI_API_KEY !")
                    
            elif self.mode == "openai":
                client = OpenAI()
            
        self.client = client

    def get_chat_completion( 
        self,
        agent: Agent,
        history: List,
        context_variables: dict,
        model_override: str,
        stream: bool,
        debug: bool,
        model_config: dict,
    ) -> ChatCompletionMessage:
        context_variables = defaultdict(str, context_variables)
        instructions = (
            agent.instructions(context_variables)
            if callable(agent.instructions)
            else agent.instructions
        )
        messages = [{"role": "system", "content": instructions}] + history
        debug_print(debug, "Getting chat completion for...:", messages)

        tools = [function_to_json(f) for f in agent.functions]
        # hide context_variables from model
        for tool in tools:
            params = tool["function"]["parameters"]
            params["properties"].pop(__CTX_VARS_NAME__, None)
            if __CTX_VARS_NAME__ in params["required"]:
                params["required"].remove(__CTX_VARS_NAME__)
        
        if not agent.model and not model_override:
            raise ValueError("Please provide either the agent model name or model_override that is compatible with the current mode.")
        
        create_params = {
            "model": model_override or agent.model,
            "messages": messages,
            "tools": tools or None,
            "tool_choice": agent.tool_choice,
            "stream": stream,
        }
        create_params.update(model_config)
        
        if self.mode in ("ollama", "openai"):
            try:
                if tools:
                    create_params["parallel_tool_calls"] = agent.parallel_tool_calls                
                return self.client.chat.completions.create(**create_params)
            
            except Exception as e:
                # Warn if the model does not support tools and switch to disabling tools
                print("Warning: This model does not support tools. Switching to tool-disabled mode.")
                # Remove tool-related parameters from create_params
                create_params["tools"] = None
                create_params["tool_choice"] = None
                return self.client.chat.completions.create(**create_params)

            except ValueError:
                raise ValueError("Please provide either the agent model name or model_override that is compatible with the current mode.")

        elif self.mode == "gemini":
            try:
                return self.client.chat.completions.create(**create_params)
            except Exception:
                raise ValueError("Please provide either the agent model name or model_override that is compatible with the current mode.")
        
    def handle_function_result(self, result, debug) -> Result: 
        match result:
            case Result() as result:
                return result

            case Agent() as agent:
                return Result(
                    value=json.dumps({"assistant": agent.name}),
                    agent=agent,
                )
            case _:
                try:
                    return Result(value=str(result))
                except Exception as e:
                    error_message = f"Failed to cast response to string: {result}. Make sure agent functions return a string or Result object. Error: {str(e)}"
                    debug_print(debug, error_message)
                    raise TypeError(error_message)

    def handle_tool_calls( 
        self,
        tool_calls: List[ChatCompletionMessageToolCall],
        functions: List[AgentFunction],
        context_variables: dict,
        debug: bool,
    ) -> Response:
        function_map = {f.__name__: f for f in functions}
        partial_response = Response(
            messages=[], agent=None, context_variables={})

        for tool_call in tool_calls:
            name = tool_call.function.name
            
            # handle missing tool case, skip to next tool
            if name not in function_map:
                debug_print(debug, f"Tool {name} not found in function map.")
                if self.mode in ("ollama", "openai"):
                    partial_response.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "tool_name": name,
                            "content": f"Error: Tool {name} not found.",
                        }
                    )
                    continue
                
                elif self.mode in ("gemini"):
                    partial_response.messages.append(
                        {
                            "role": "assistant",
                            "content": f"Error: Tool {name} not found.",
                        }
                    )
                    continue
                
            args = json.loads(tool_call.function.arguments)
            debug_print(
                debug, f"Processing tool call: {name} with arguments {args}")

            func = function_map[name]
            # pass context_variables to agent functions
            if __CTX_VARS_NAME__ in func.__code__.co_varnames:
                args[__CTX_VARS_NAME__] = context_variables
            raw_result = function_map[name](**args)

            result: Result = self.handle_function_result(raw_result, debug)
            
            if self.mode in ("ollama", "openai"):
                partial_response.messages.append(
                    {
                        "role": "tool", 
                        "tool_call_id": tool_call.id,
                        "tool_name": name,
                        "content": result.value, 
                    }
                )
                
            elif self.mode in ("gemini"):
                partial_response.messages.append(
                    {
                        "role": "assistant", 
                        "content": f"Tool {name} returned: {result.value}. \nTool_call args: {args}", 
                    }
                )
                
            partial_response.context_variables.update(result.context_variables)
            if result.agent:
                partial_response.agent = result.agent

        return partial_response

    def run_and_stream(
        self,
        agent: Agent,
        messages: List,
        context_variables: dict = {},
        model_override: str = None,
        model_config: dict = {},
        debug: bool = False,
        max_turns: int = float("inf"),
        execute_tools: bool = True,
    ):
        active_agent = agent
        context_variables = copy.deepcopy(context_variables)
        history = copy.deepcopy(messages)
        init_len = len(messages)

        while len(history) - init_len < max_turns:

            message = {
                "content": "",
                "sender": agent.name,
                "role": "assistant",
                "function_call": None,
                "tool_calls": defaultdict(
                    lambda: {
                        "function": {"arguments": "", "name": ""},
                        "id": "",
                        "type": "",
                    }
                ),
            }

            # get completion with current history, agent
            completion = self.get_chat_completion(
                agent=active_agent,
                history=history,
                context_variables=context_variables,
                model_override=model_override,
                stream=True,
                debug=debug,
                model_config=model_config,
            )

            yield {"delim": "start"}
            for chunk in completion:
                delta = json.loads(chunk.choices[0].delta.json())
                if delta["role"] == "assistant":
                    delta["sender"] = active_agent.name
                yield delta
                delta.pop("role", None)
                delta.pop("sender", None)
                merge_chunk(message, delta)
            yield {"delim": "end"}

            message["tool_calls"] = list(
                message.get("tool_calls", {}).values())
            if not message["tool_calls"]:
                message["tool_calls"] = None
            debug_print(debug, "Received completion:", message)
            history.append(message)

            if not message["tool_calls"] or not execute_tools:
                debug_print(debug, "Ending turn.")
                break

            # convert tool_calls to objects
            tool_calls = []
            for tool_call in message["tool_calls"]:
                function = Function(
                    arguments=tool_call["function"]["arguments"],
                    name=tool_call["function"]["name"],
                )
                tool_call_object = ChatCompletionMessageToolCall(
                    id=tool_call["id"], function=function, type=tool_call["type"]
                )
                tool_calls.append(tool_call_object)

            # handle function calls, updating context_variables, and switching agents
            partial_response = self.handle_tool_calls(
                tool_calls, active_agent.functions, context_variables, debug
            )
            history.extend(partial_response.messages)
            context_variables.update(partial_response.context_variables)
            if partial_response.agent:
                active_agent = partial_response.agent

        yield {
            "response": Response(
                messages=history[init_len:],
                agent=active_agent,
                context_variables=context_variables,
            )
        }

    def run(
        self,
        agent: Agent,
        messages: List,
        context_variables: dict = {},
        model_override: str = None,
        model_config:dict = {},
        stream: bool = False,
        debug: bool = False,
        max_turns: int = float("inf"),
        execute_tools: bool = True,
    ) -> Response:
        if stream:
            return self.run_and_stream(
                agent=agent,
                messages=messages,
                context_variables=context_variables,
                model_override=model_override,
                model_config=model_config,
                debug=debug,
                max_turns=max_turns,
                execute_tools=execute_tools,
            )
        active_agent = agent
        context_variables = copy.deepcopy(context_variables)
        history = copy.deepcopy(messages)
        init_len = len(messages)

        while len(history) - init_len < max_turns and active_agent:

            # get completion with current history, agent
            completion = self.get_chat_completion(
                agent=active_agent,
                history=history,
                context_variables=context_variables,
                model_override=model_override,
                stream=stream,
                debug=debug,
                model_config=model_config,
            )
            message = completion.choices[0].message
            debug_print(debug, "Received completion:", message)
            message.sender = active_agent.name
            
            # update history
            if self.mode in ("openai", "ollama"): 
                history.append(
                    json.loads(message.model_dump_json())
                )  
                
            elif self.mode in ("gemini"):
                history.append(
                    {
                        "role": message.role,
                        "content": str(json.loads(message.model_dump_json())).replace("'", '"').replace("None", "null")
                    }
                ) 

            if not message.tool_calls or not execute_tools:
                debug_print(debug, "Ending turn.")
                break

            # handle function calls, updating context_variables, and switching agents
            partial_response = self.handle_tool_calls(
                message.tool_calls, active_agent.functions, context_variables, debug
            )
            history.extend(partial_response.messages)
            context_variables.update(partial_response.context_variables)
            if partial_response.agent:
                active_agent = partial_response.agent

        # post-process last output for Gemini
        if self.mode in ("gemini"):
            messages = history[init_len:][-1]["content"]
            last_content = json.loads(messages)["content"]
            history[init_len:][-1]["content"] = last_content.strip()
        
        return Response(
            messages=history[init_len:],
            agent=active_agent,
            context_variables=context_variables,
        )
