"""The adapter turns fluid's per-field estimate flags into one ``usage_estimated``.

fluid marks a guessed count on the call's ``Usage.raw`` (``input_tokens_estimated``
/ ``output_tokens_estimated``: the provider sent no usage, or the reply was
interrupted) and sums only numbers into ``TurnComplete.usage``, so the turn
total never carries the flags. The adapter stamps the per-call record and the
turn's terminal usage; the pumps copy it into the result metadata; the booking
flags the ledger row. The Claude CLI leg never estimates, so nothing here fires
for it.
"""

from fluid.events import ErrorEvent, TurnComplete, Usage
from fluid.events.model import Interrupted

from app.core.provider.harness_adapter import HarnessAdapter


def _usage(index: int, **raw) -> Usage:
    return Usage(
        session_id="engine-s", turn_id="t1", input_tokens=100, output_tokens=20,
        raw={"model_call_index": index, "cost_usd": 0.001, **raw},
    )


def _terminal_usage(adapter: HarnessAdapter, event) -> dict:
    [result] = adapter._translate(event)
    return result.usage


def test_a_measured_turn_carries_no_flag():
    adapter = HarnessAdapter()
    [call] = adapter._translate(_usage(1))
    assert "usage_estimated" not in call.usage
    done = TurnComplete(session_id="engine-s", turn_id="t1", stop_reason="end_turn",
                        num_model_calls=1, usage={"input_tokens": 100, "output_tokens": 20},
                        cost_usd=0.001)
    assert "usage_estimated" not in _terminal_usage(adapter, done)


def test_one_estimated_call_marks_the_call_and_the_turn_total():
    adapter = HarnessAdapter()
    [measured] = adapter._translate(_usage(1))
    [guessed] = adapter._translate(_usage(2, output_tokens_estimated=True))
    assert "usage_estimated" not in measured.usage
    assert guessed.usage["usage_estimated"] is True
    assert guessed.usage["output_tokens_estimated"] is True  # fluid's own flag survives
    done = TurnComplete(session_id="engine-s", turn_id="t1", stop_reason="end_turn",
                        num_model_calls=2, usage={"input_tokens": 200, "output_tokens": 40},
                        cost_usd=0.002)
    assert _terminal_usage(adapter, done)["usage_estimated"] is True
    # The roll-up still sums the estimated amounts (budgets count them).
    assert adapter._turn_usage_snapshot()["input_tokens"] == 200


def test_interrupted_and_error_terminals_are_stamped_too():
    for terminal in (
        Interrupted(session_id="engine-s", turn_id="t1", message="stopped",
                    usage={"input_tokens": 100, "output_tokens": 20}, cost_usd=0.001,
                    num_model_calls=1),
        ErrorEvent(session_id="engine-s", turn_id="t1", message="boom", num_model_calls=1),
    ):
        adapter = HarnessAdapter()
        adapter._translate(_usage(1, input_tokens_estimated=True))
        assert _terminal_usage(adapter, terminal)["usage_estimated"] is True


def test_the_flag_resets_at_the_next_turn_floor():
    adapter = HarnessAdapter()
    adapter._translate(_usage(1, input_tokens_estimated=True))
    assert adapter._turn_usage_snapshot()["usage_estimated"] is True
    adapter._turn_usage_totals = {}
    adapter._turn_usage_estimated = False  # what the turn floor does
    adapter._translate(_usage(1))
    assert "usage_estimated" not in adapter._turn_usage_snapshot()
