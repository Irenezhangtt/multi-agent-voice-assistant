"""Supervisor + independent LangGraph ReAct specialists using real MCP tools."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
import json
from pathlib import Path
from time import perf_counter
from threading import RLock
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from voice_agent.mcp_client import MCPToolkit
from voice_agent.mcp_policy import AGENT_TOOLS
from voice_agent.models import claude_model
from voice_agent.context import specialist_context, trusted_facts
from voice_agent.execution import current_execution, ExecutionInterrupted
from voice_agent.state import AgentState

PROMPTS = Path(__file__).parent / "prompts"


class ReactState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    rounds: int
    exhausted: bool


def message_text(message):
    if isinstance(message.content, str):
        return message.content
    return " ".join(block.get("text", "") for block in message.content if isinstance(block, dict) and block.get("type") == "text")


def react_graph(name, model, tools, prompt, max_rounds, terminal=lambda: False, dispatch=None):
    """Each actor has its own model/tool loop and system prompt, not a fixed tool path."""
    bound = model.bind_tools(tools, parallel_tool_calls=name == 'supervisor')
    by_name = {tool.name: tool for tool in tools}

    def reason(state):
        control = current_execution.get()
        invoke = lambda: bound.invoke(state["messages"], config={"run_name": name + "_model", "metadata": {"agent": name}})
        answer = control.model(f"{name}:model:{state.get('rounds', 0)}", invoke) if control else invoke()
        return {"messages": [answer], "rounds": state.get("rounds", 0) + 1}

    def act(state):
        if dispatch:
            return {"messages": dispatch(state['messages'][-1].tool_calls, by_name)}
        observations = []
        for call in state["messages"][-1].tool_calls:
            control = current_execution.get()
            if control:
                control.check()
                control.step_key = f"{name}:tool:{call['id']}"
            start = perf_counter()
            try:
                if call["name"] not in by_name:
                    raise PermissionError("Tool is outside this agent's toolkit")
                result = by_name[call["name"]].invoke(call["args"])
            except ExecutionInterrupted:
                raise
            except Exception as error:
                result = {"error": type(error).__name__, "message": "Tool failed or was rejected. Do not assume the action succeeded."}
            observations.append(ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call["id"], name=call["name"]))
            if terminal():
                # Never execute more calls after a human escalation / completed child task.
                break
        return {"messages": observations}

    graph = StateGraph(ReactState)
    graph.add_node("model", reason)
    graph.add_node("tools", act)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", lambda s: "tools" if s["messages"][-1].tool_calls and s["rounds"] < max_rounds else END)
    graph.add_conditional_edges("tools", lambda s: END if terminal() else "model")
    return graph.compile(name=name + "_react")


class MultiAgentSystem:
    def __init__(self, settings, clock, *, toolkit=None, model_factory=None):
        self.settings, self.clock = settings, clock
        self.toolkit = toolkit or MCPToolkit(settings)
        self.owns_toolkit = toolkit is None
        self.model_factory = model_factory or (lambda name: claude_model(settings, max_tokens=1400))
        self.prompts = {name: (PROMPTS / f"{name}.md").read_text() for name in ("supervisor", *AGENT_TOOLS)}

    def close(self):
        if self.owns_toolkit:
            self.toolkit.close()

    def run(self, state):
        control = current_execution.get()
        now = control.row["clock"] if control else self.clock().isoformat()
        context = deepcopy(dict(state))
        context.update(events=[], agent_results={}, status="needs_information")
        parent_context = context
        merge_lock = RLock()
        batch_base = ContextVar('delegation_batch_base', default=None)
        batch_finished = False
        verified_replies = []
        visited = set()
        canonical_keys = {"booking", "booking_action", "insurance", "handoff", "status", "active_workflow", "pending_workflows"}
        if context.get("handoff", {}).get("status") == "human_escalation":
            return {"reply": "This call is already marked for human escalation. Please start a new call for another automated task.", "status": "human_escalation", "route": "handoff"}

        def snapshot(view=None):
            view = parent_context if view is None else view
            with merge_lock:
                return {key: deepcopy(view.get(key)) for key in ("call_id", "case_id", "turn_id", "history", "message", "booking", "booking_action", "insurance", "handoff", "status", "active_workflow", "pending_workflows", "agent_results", "trusted_facts") if key in view}

        def run_specialist(name, request, fact_keys=None):
            context = deepcopy(batch_base.get() or snapshot())
            context.update(events=[], agent_results={})
            control = current_execution.get()
            if context.get("handoff", {}).get("status") == "human_escalation":
                return {"status": "human_escalation", "error": "Automation has ended"}
            prompt = {**specialist_context(name, context, request, fact_keys), "clinic_now": now, "timezone": self.settings.timezone}
            with merge_lock:
                if name in visited:
                    return {"error": "already_delegated", "message": "Use this specialist's previous result; ask the caller for missing information."}
                visited.add(name)
            context["route"] = name
            context["active_workflow"] = name
            context["events"].append({"node": "supervisor", "type": "delegation", "agent": name})
            child_done = False
            child_reply = None
            prepared_this_turn = False

            def make_mcp_tool(tool_name):
                def invoke(**arguments):
                    nonlocal child_done, child_reply, prepared_this_turn
                    scope = {"call_id": state["call_id"], "turn_id": state["turn_id"], "user_message": state["message"],
                             "snapshot": snapshot(context), "now": now, "model": self.settings.model,
                             "prepared_this_turn": prepared_this_turn}
                    if control:
                        scope.update(control.scope())
                    start = perf_counter()
                    try:
                        invoke_tool = lambda: self.toolkit.call(name, tool_name, arguments, scope)
                        result = control.tool(control.step_key, invoke_tool) if control else invoke_tool()
                    except ExecutionInterrupted:
                        raise
                    except Exception as error:
                        context["events"].append({"node": tool_name, "type": "mcp_tool", "agent": name, "status": "error", "error_type": type(error).__name__})
                        raise
                    context["events"].append({"node": tool_name, "type": "mcp_tool", "agent": name, "status": "rejected" if result.get("error") else "ok", "result": result,
                                              "duration_ms": round((perf_counter() - start) * 1000, 2)})
                    patch = result.get("patch", {})
                    if patch:
                        context.update({k: v for k, v in patch.items() if k in canonical_keys})
                        for domain in ("booking", "insurance", "handoff"):
                            if domain in patch:
                                context.setdefault("trusted_facts", {}).pop(domain, None)
                        child_reply = result.get("reply")
                        child_done = True  # Committed result, review offer, or missing fields needs the caller.
                    if tool_name == "find_booking" and result.get("status") in {"confirmed", "modified"}:
                        if context.get("booking", {}).get("status") != "awaiting_confirmation":
                            context["booking"] = {**result, "customer_phone": result.get("customer_id", arguments["customer_phone"])}
                            context.setdefault("trusted_facts", {}).pop("booking", None)
                        child_reply = f"Your existing appointment is {result['id']}, {result['date']} at {result['time']} for {result['service']}. No appointment changes were made this turn."
                        context["status"] = "completed"
                    if tool_name == "search_availability" and result.get("status") == "success":
                        child_reply = "Available times: " + ", ".join(result["slots"]) + ". No appointment changes were made."
                    if tool_name == "prepare_appointment" and not result.get("error"):
                        prepared_this_turn = True
                    return result
                raw = self.toolkit.schemas[tool_name]
                return StructuredTool.from_function(invoke, name=tool_name, description=raw.get("description", tool_name), args_schema=self.toolkit.public_schema(tool_name))

            tools = [make_mcp_tool(tool_name) for tool_name in sorted(AGENT_TOOLS[name])]
            graph = react_graph(name, self.model_factory(name), tools, self.prompts[name], self.settings.agent_max_rounds, terminal=lambda: child_done)
            try:
                output = graph.invoke({"messages": [SystemMessage(self.prompts[name]), HumanMessage(json.dumps(prompt, ensure_ascii=False))], "rounds": 0},
                                      {"run_name": name + "_agent", "recursion_limit": 24, "metadata": {"agent": name, "call_id": state["call_id"]}})
                last = output["messages"][-1]
                exhausted = isinstance(last, AIMessage) and bool(last.tool_calls)
                if exhausted:
                    context["events"].append({"node": name, "type": "budget_exhausted", "rounds": output["rounds"]})
                answer = child_reply or ("I could not complete the task within this turn. Please try again or ask for a human." if exhausted else message_text(last))
                if not child_reply and name == "booking":
                    # Claims about writes come only from verified MCP receipts/review offers.
                    missing = [label for key, label in (("customer_phone", "your phone number"), ("patient_name", "your name"), ("service", "the service"), ("date", "the date"), ("time", "the time")) if not context.get("booking", {}).get(key)]
                    answer = "No appointment change was verified this turn. " + ("Please provide " + ", ".join(missing) + "." if missing else "Please ask me to prepare or review the appointment details.")
                if not child_reply and name == "insurance":
                    answer = "Insurance eligibility has not been verified. Please provide the insurer, member ID, and service, or ask me to retrieve the evidence."
                if not child_reply and name == "handoff":
                    answer = "Human escalation has not been recorded. Please try again."
            except ExecutionInterrupted:
                raise
            except Exception as error:
                context["events"].append({"node": name, "type": "agent_error", "error_type": type(error).__name__})
                answer = child_reply or "The specialist is temporarily unavailable. No new successful operation was verified. Please retry or ask for a human."
                if not child_reply:
                    context["status"] = "model_error"
            context["agent_results"][name] = {"reply": answer, "status": context["status"]}
            context["trusted_facts"] = trusted_facts(context)
            with merge_lock:
                if control:
                    control.check()
                # Each branch can merge only its own domain. Never replace sibling state.
                parent_context[name] = deepcopy(context.get(name, {}))
                if name == 'booking' and 'booking_action' in context:
                    parent_context['booking_action'] = context['booking_action']
                parent_context['events'].extend(context['events'])
                parent_context['agent_results'].update(context['agent_results'])
                parent_context.setdefault('trusted_facts', {}).pop(name, None)
                parent_context['trusted_facts'] = trusted_facts(parent_context)
                parent_context['route'] = name
                if parent_context.get('handoff', {}).get('status') == 'human_escalation':
                    parent_context['status'] = 'human_escalation'
                elif parent_context.get('booking', {}).get('status') == 'awaiting_confirmation':
                    parent_context['status'] = 'awaiting_confirmation'
                else:
                    parent_context['status'] = context['status']
                parent_context['active_workflow'] = 'booking' if parent_context['status'] == 'awaiting_confirmation' else name
                parent_context['pending_workflows'] = ['booking'] if parent_context['status'] == 'awaiting_confirmation' else context.get('pending_workflows', [])
                verified_replies.append(answer)
                # Only verified tool replies can leave the task before its final response.
                if child_reply and outer_control and outer_control.reply_sink:
                    progress = deepcopy(parent_context)
                    progress['reply'] = '\n'.join(dict.fromkeys(verified_replies))
                    progress['history'] = [*state.get('history', []), {'role':'user','content':state['message']}, {'role':'assistant','content':progress['reply']}][-40:]
                    outer_control.publish(progress, answer)
            return {"agent": name, "summary": answer[:1600], "status": context["status"], "fact_delta": {k: v for k, v in context["trusted_facts"].items() if k == name}}

        outer_control = control

        def dispatch(calls, tools):
            nonlocal batch_finished
            def invoke(call):
                token = None
                current = current_execution.get()
                if current:
                    token = current_execution.set(current.fork())
                try:
                    if current:
                        current.check()
                    result = tools[call['name']].invoke(call['args'])
                except ExecutionInterrupted:
                    raise
                except Exception as error:
                    result = {'error':type(error).__name__, 'message':'Delegation rejected; review required inputs.'}
                finally:
                    if token is not None:
                        current_execution.reset(token)
                return ToolMessage(content=json.dumps(result,ensure_ascii=False),tool_call_id=call['id'],name=call['name'])

            handoff = next((c for c in calls if c['name']=='delegate_handoff'), None)
            if handoff:
                # Explicit handoff wins before any sibling business task is launched.
                result = invoke(handoff)
                return [result if c is handoff else ToolMessage(content='{"error":"handoff_priority"}', tool_call_id=c['id'],name=c['name']) for c in calls]
            names = [c['name'] for c in calls]
            independent = (len(calls)==2 and set(names)=={'delegate_booking','delegate_insurance'}
                           and all(not c['args'].get('fact_keys') or set(c['args']['fact_keys']) <= {c['name'].removeprefix('delegate_')} for c in calls))
            if independent:
                base = snapshot()
                parent_context['events'].append({'node':'supervisor','type':'parallel_dispatch','agents':['booking','insurance']})
                token = batch_base.set(base)
                pool = ThreadPoolExecutor(max_workers=2,thread_name_prefix='clinic-child')
                try:
                    futures = [pool.submit(copy_context().run, invoke, c) for c in calls]
                    messages = [future.result() for future in futures]
                    batch_finished = True  # All independently planned domains have returned.
                    return messages
                finally:
                    pool.shutdown(wait=False,cancel_futures=True)
                    batch_base.reset(token)
            # A cross-domain fact request is a dependency, even if the model
            # emitted the dependent tool before its prerequisite in this batch.
            ordered, remaining = [], list(calls)
            while remaining:
                pending = {c['name'].removeprefix('delegate_') for c in remaining}
                ready = next((c for c in remaining if not ((set(c['args'].get('fact_keys') or []) - {c['name'].removeprefix('delegate_')}) & pending)),None)
                if ready is None:
                    return [ToolMessage(content='{"error":"cyclic_dependencies"}',tool_call_id=c['id'],name=c['name']) for c in calls]
                ordered.append(ready)
                remaining.remove(ready)
            observations = {c['id']:invoke(c) for c in ordered}
            return [observations[c['id']] for c in calls]

        def delegate(name):
            def invoke(request: str, fact_keys: list[Literal["booking", "insurance", "handoff"]] | None = None):
                return run_specialist(name, request, fact_keys)
            return StructuredTool.from_function(invoke, name="delegate_" + name, description=f"Delegate a concise task to the isolated {name} ReAct agent. Omit fact_keys to use defaults. fact_keys contains DOMAIN NAMES only, e.g. ['booking','insurance']; never field names such as phone or date. Its own pending task/offer is always included automatically. History is never forwarded.")

        graph = react_graph("supervisor", self.model_factory("supervisor"), [delegate(name) for name in AGENT_TOOLS], self.prompts["supervisor"], self.settings.agent_max_rounds,
                            terminal=lambda: batch_finished or context.get("handoff", {}).get("status") == "human_escalation", dispatch=dispatch)
        try:
            result = graph.invoke({"messages": [SystemMessage(self.prompts["supervisor"]), HumanMessage(json.dumps({"shared_context": snapshot(), "clinic_now": now, "timezone": self.settings.timezone}, ensure_ascii=False))], "rounds": 0},
                                  {"run_name": "supervisor_agent", "recursion_limit": 24, "metadata": {"agent": "supervisor", "call_id": state["call_id"]}})
            last = result["messages"][-1]
            reply = "\n".join(dict.fromkeys(verified_replies)) if verified_replies else message_text(last)
            if not reply or isinstance(last, AIMessage) and last.tool_calls and not verified_replies:
                reply = "I could not complete the request this turn. Please try again or ask for a human."
        except ExecutionInterrupted:
            raise
        except Exception as error:
            context["events"].append({"node": "supervisor", "type": "agent_error", "error_type": type(error).__name__})
            reply = "\n".join(dict.fromkeys(verified_replies)) or "The supervisor is temporarily unavailable. No successful operation was verified. Please retry."
            if not verified_replies:
                context["status"] = "model_error"
        if not visited:
            context["route"] = "clarify"
            if context["status"] != "model_error":
                reply = "I can manage local appointments, check demo insurance eligibility, or record human escalation. What would you like help with?"
        elif not verified_replies:
            context["status"] = "model_error"
            reply = "I could not verify a result for this request. Please retry or ask for human assistance."
        context["reply"] = reply
        context["history"] = [*state.get("history", []), {"role": "user", "content": state["message"]}, {"role": "assistant", "content": reply}][-40:]
        return {k: context[k] for k in (*canonical_keys, "route", "reply", "events", "history", "agent_results", "trusted_facts") if k in context}

    def graph(self, checkpointer):
        graph = StateGraph(AgentState)
        graph.add_node("supervisor", self.run)
        graph.add_edge(START, "supervisor")
        graph.add_edge("supervisor", END)
        return graph.compile(checkpointer=checkpointer, name="clinic_multi_agent")
