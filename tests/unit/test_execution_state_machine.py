"""Tests for the order state machine — pure function, deterministic."""
from __future__ import annotations

import pytest

from trading_agent.execution.state_machine import (
    OrderEvent,
    OrderState,
    StateMachineError,
    is_active,
    is_terminal,
    transition,
)


def test_new_submits_to_sent():
    assert transition(OrderState.NEW, OrderEvent.SUBMIT) == OrderState.SENT


def test_sent_can_be_partial():
    assert transition(OrderState.SENT, OrderEvent.PARTIAL_FILL) == OrderState.PARTIAL


def test_sent_can_be_fully_filled():
    assert transition(OrderState.SENT, OrderEvent.FILL) == OrderState.FILLED


def test_sent_can_be_cancelled():
    assert transition(OrderState.SENT, OrderEvent.CANCEL) == OrderState.CANCELLED


def test_sent_can_be_rejected():
    assert transition(OrderState.SENT, OrderEvent.REJECT) == OrderState.REJECTED


def test_partial_can_fully_fill():
    assert transition(OrderState.PARTIAL, OrderEvent.FILL) == OrderState.FILLED


def test_partial_can_get_more_partials():
    assert transition(OrderState.PARTIAL, OrderEvent.PARTIAL_FILL) == OrderState.PARTIAL


def test_filled_cannot_transition():
    with pytest.raises(StateMachineError):
        transition(OrderState.FILLED, OrderEvent.CANCEL)
    with pytest.raises(StateMachineError):
        transition(OrderState.FILLED, OrderEvent.FILL)


def test_cancelled_cannot_transition():
    with pytest.raises(StateMachineError):
        transition(OrderState.CANCELLED, OrderEvent.FILL)


def test_rejected_cannot_transition():
    with pytest.raises(StateMachineError):
        transition(OrderState.REJECTED, OrderEvent.SUBMIT)


def test_cannot_submit_twice():
    sent = transition(OrderState.NEW, OrderEvent.SUBMIT)
    with pytest.raises(StateMachineError):
        transition(sent, OrderEvent.SUBMIT)


def test_is_terminal():
    assert is_terminal(OrderState.FILLED)
    assert is_terminal(OrderState.CANCELLED)
    assert is_terminal(OrderState.REJECTED)
    assert not is_terminal(OrderState.NEW)
    assert not is_terminal(OrderState.SENT)
    assert not is_terminal(OrderState.PARTIAL)


def test_is_active():
    assert is_active(OrderState.SENT)
    assert is_active(OrderState.PARTIAL)
    assert not is_active(OrderState.NEW)
    assert not is_active(OrderState.FILLED)
    assert not is_active(OrderState.CANCELLED)
    assert not is_active(OrderState.REJECTED)
