from langgraph.graph import END, START, StateGraph

from voice_agent.state import AgentState


def subgraph(name, handler):
    graph = StateGraph(AgentState)
    graph.add_node(name, handler)
    graph.add_edge(START, name)
    graph.add_edge(name, END)
    return graph.compile(name=name)


def call_tool(state, tools, name, args):
    """Keep local tool spans even when cloud tracing is disabled."""
    from time import perf_counter

    start = perf_counter()
    try:
        result = tools[name].invoke(args)
        state["events"].append({
            "node": name, "type": "tool", "status": "ok", "result": result,
            "duration_ms": round((perf_counter() - start) * 1000, 2),
        })
        return result
    except TimeoutError:
        state["events"].append({"node": name, "type": "tool", "status": "timeout", "duration_ms": round((perf_counter() - start) * 1000, 2)})
        return {"status": "timeout"}
    except Exception as exc:
        state["events"].append({"node": name, "type": "tool", "status": "error", "error_type": type(exc).__name__, "duration_ms": round((perf_counter() - start) * 1000, 2)})
        raise


def booking_label(booking):
    service = {"cleaning": "cleaning", "consultation": "consultation"}.get(booking.get("service"), "visit")
    return f"{booking.get('patient_name', '')}, {booking.get('date', '')} at {booking.get('time', '')}, {service}"
