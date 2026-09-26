from __future__ import annotations

import threading

import pytest

from askdb.multiagent.budget import BudgetExceeded, TokenBudget


def test_parallel_reservation_waits_for_actual_usage_before_rejecting():
    budget = TokenBudget(30_000, spent=2_600)
    budget.reserve(9_900)
    budget.reserve(9_900)
    entered = threading.Event()
    done = threading.Event()
    errors = []

    def third_worker():
        entered.set()
        try:
            budget.reserve(9_900, wait_seconds=2)
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=third_worker)
    worker.start()
    assert entered.wait(1)
    assert not done.wait(0.05)
    budget.settle(9_900, 5_000)
    assert done.wait(1)
    worker.join()
    assert not errors
    assert budget.spent == 7_600
    assert budget.reserved == 19_800


def test_parallel_reservation_rejects_after_settlement_if_still_over_cap():
    budget = TokenBudget(20_000, spent=2_600)
    budget.reserve(9_900)
    entered = threading.Event()
    done = threading.Event()
    errors = []

    def second_worker():
        entered.set()
        try:
            budget.reserve(9_900, wait_seconds=2)
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=second_worker)
    worker.start()
    assert entered.wait(1)
    assert not done.wait(0.05)
    budget.settle(9_900, 12_000)
    assert done.wait(1)
    worker.join()
    assert len(errors) == 1 and isinstance(errors[0], BudgetExceeded)
    assert budget.reserved == 0
