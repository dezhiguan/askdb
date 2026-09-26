"""Shared per-run token reservations for parallel multi-agent calls."""

from __future__ import annotations

import json
import threading
import time
from typing import Any


class BudgetExceeded(RuntimeError):
    pass


def estimate_tokens(*parts: Any) -> int:
    """Reserve prompt, tool schema and response room before contacting a model.

    Provider tokenizers can differ; actual usage is always reconciled afterwards.
    The reservation prevents concurrent workers from all spending the same balance.
    """
    prompt = "\n".join(
        json.dumps(part, ensure_ascii=False, default=str) if not isinstance(part, str)
        else part for part in parts)
    # Avoid a tokenizer's first-run network download in production. Chinese
    # characters count as one, ASCII as one per three characters, with margin.
    prompt_tokens = sum(1.0 if ord(char) > 127 else 0.34 for char in prompt)
    return max(256, int(prompt_tokens * 1.3) + 1024)


class TokenBudget:
    def __init__(self, cap: int, spent: int = 0, *,
                 cost_cap_cny: float = 0.0, cost_spent_cny: float = 0.0):
        self.cap = cap
        self.spent = spent
        self.reserved = 0
        self.cost_cap_cny = cost_cap_cny
        self.cost_spent_cny = cost_spent_cny
        self.cost_reserved_cny = 0.0
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)

    def reserve(self, amount: int, estimated_cost_cny: float = 0.0,
                *, wait_seconds: float = 60.0) -> None:
        deadline = time.monotonic() + wait_seconds
        with self._settled:
            while True:
                token_short = self.spent + self.reserved + amount > self.cap
                cost_short = (self.cost_cap_cny > 0 and self.cost_spent_cny
                              + self.cost_reserved_cny + estimated_cost_cny
                              > self.cost_cap_cny)
                if not token_short and not cost_short:
                    self.reserved += amount
                    self.cost_reserved_cny += estimated_cost_cny
                    return
                # Another parallel Worker may release most of its conservative
                # reservation. Do not reject this Worker until that call settles.
                if ((not self.reserved and not self.cost_reserved_cny)
                        or time.monotonic() >= deadline):
                    if token_short:
                        raise BudgetExceeded(
                            f"Token 预算不足：已用 {self.spent}，预留 {self.reserved}，"
                            f"本次预计 {amount}，上限 {self.cap}")
                    raise BudgetExceeded(
                        f"费用预算不足：已用 ¥{self.cost_spent_cny:.6f}，"
                        f"预留 ¥{self.cost_reserved_cny:.6f}，"
                        f"本次预计 ¥{estimated_cost_cny:.6f}，"
                        f"上限 ¥{self.cost_cap_cny:.6f}")
                self._settled.wait(timeout=max(0.0, deadline - time.monotonic()))

    def settle(self, reserved: int, actual: int,
               reserved_cost_cny: float = 0.0, actual_cost_cny: float = 0.0) -> None:
        with self._settled:
            self.reserved -= reserved
            self.spent += actual
            self.cost_reserved_cny -= reserved_cost_cny
            self.cost_spent_cny += actual_cost_cny
            self._settled.notify_all()

    def exhausted(self) -> bool:
        with self._lock:
            return self.spent >= self.cap or (
                self.cost_cap_cny > 0 and self.cost_spent_cny >= self.cost_cap_cny)
