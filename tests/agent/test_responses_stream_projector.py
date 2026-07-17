from types import SimpleNamespace

from agent.responses_stream_projector import ResponsesStreamProjector


def test_commentary_message_delta_routes_to_reasoning():
    projector = ResponsesStreamProjector()
    projector.project(
        SimpleNamespace(
            type="response.output_item.added",
            item=SimpleNamespace(type="message", phase="commentary"),
        )
    )

    projection = projector.project(
        SimpleNamespace(type="response.output_text.delta", delta="checking")
    )

    assert projection.reasoning_delta == "checking"
    assert projection.content_delta == ""


def test_final_answer_delta_routes_to_content():
    projector = ResponsesStreamProjector()
    projector.project(
        {
            "type": "response.output_item.added",
            "item": {"type": "message", "phase": "final_answer"},
        }
    )

    projection = projector.project(
        {"type": "response.output_text.delta", "delta": "done"}
    )

    assert projection.content_delta == "done"
    assert projection.reasoning_delta == ""


def test_reasoning_delta_and_completed_item_preserve_ordered_classification():
    projector = ResponsesStreamProjector()
    reasoning = projector.project(
        {"type": "response.reasoning_summary_text.delta", "delta": "think"}
    )
    item = {"type": "message", "phase": "final_answer"}
    completed = projector.project(
        {"type": "response.output_item.done", "item": item}
    )

    assert reasoning.reasoning_delta == "think"
    assert completed.completed_item is item
