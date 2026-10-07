from __future__ import annotations

import time
from dataclasses import dataclass

from paper_search.domain import DeadlineExceeded, RunLimits, Usage, now


@dataclass(frozen=True)
class Reservation:
    id: int
    input_tokens: int
    output_tokens: int
    purpose: str = "research"
    owner: str = ""


class RunMeter:
    """Track usage and enforce only the job deadline, never call quotas."""

    def __init__(
        self,
        limits: RunLimits,
        initial: Usage | None = None,
        *,
        deadline_at=None,
        owners=None,
    ):
        self.limits = limits
        self.deadline_at = deadline_at
        self.owners = owners or {}
        self._usage = initial or Usage()
        self.started = time.monotonic()
        self.reserved = 0
        self._next_reservation = 0
        self._reservations: dict[int, Reservation] = {}
        self.finalization_reason: str | None = None

    @property
    def finalizing(self) -> bool:
        return self.finalization_reason is not None

    @property
    def usage(self) -> Usage:
        return self._usage.model_copy(
            update={
                "active_seconds": self._usage.active_seconds
                + time.monotonic()
                - self.started,
            }
        )

    @property
    def remaining_seconds(self) -> float:
        if self.deadline_at is not None:
            return max(0, (self.deadline_at - now()).total_seconds())
        return max(0, self.limits.deadline_seconds - self.usage.active_seconds)

    @property
    def final_reserve_seconds(self) -> float:
        return self.limits.finalization_reserve_seconds

    @property
    def research_seconds(self) -> float:
        return max(0, self.remaining_seconds - self.final_reserve_seconds)

    def check_time(self) -> None:
        if self.remaining_seconds <= 0:
            raise DeadlineExceeded("run deadline reached")

    def model(
        self,
        input_tokens: int,
        output_tokens: int | None,
        *,
        final: bool = False,
        purpose: str = "research",
        owner: str = "",
    ) -> Reservation:
        self.check_time()
        if not final and self.research_seconds <= 0:
            raise DeadlineExceeded(
                "research deadline reached; finalize with current evidence"
            )
        if self.finalizing and not final:
            raise DeadlineExceeded(self.finalization_reason)
        output_tokens = output_tokens or 0
        self.reserved += input_tokens + output_tokens
        self._next_reservation += 1
        reservation = Reservation(
            self._next_reservation, input_tokens, output_tokens, purpose, owner
        )
        self._reservations[reservation.id] = reservation
        self._usage = self._usage.model_copy(
            update={"model_calls": self._usage.model_calls + 1}
        )
        return reservation

    def settle(self, reserved: Reservation, usage: Usage) -> None:
        if self._reservations.pop(reserved.id, None) is None:
            raise RuntimeError("model reservation was already settled")
        self.reserved -= reserved.input_tokens + reserved.output_tokens
        usage = (
            usage.model_copy(
                update={
                    "estimated_input_tokens": usage.estimated_input_tokens
                    + usage.input_tokens,
                    "estimated_output_tokens": usage.estimated_output_tokens
                    + usage.output_tokens,
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
            )
            if usage.estimated
            else usage
        )
        values = {
            name: getattr(self._usage, name) + getattr(usage, name)
            for name in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_input_tokens",
                "cost_cny",
                "estimated_input_tokens",
                "estimated_output_tokens",
            )
        }
        values.update(estimated=self._usage.estimated or usage.estimated)
        self._usage = self._usage.model_copy(update=values)
        prior = self.owners.get(reserved.owner, Usage())
        own_values = {
            name: getattr(prior, name) + getattr(usage, name)
            for name in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_input_tokens",
                "cost_cny",
                "estimated_input_tokens",
                "estimated_output_tokens",
            )
        }
        own_values.update(
            model_calls=prior.model_calls + 1,
            estimated=prior.estimated or usage.estimated,
        )
        self.owners[reserved.owner] = prior.model_copy(update=own_values)

    def release_unissued(self, reserved: Reservation) -> None:
        if self._reservations.pop(reserved.id, None) is None:
            raise RuntimeError("model reservation was already settled")
        self.reserved -= reserved.input_tokens + reserved.output_tokens
        self._usage = self._usage.model_copy(
            update={"model_calls": self._usage.model_calls - 1}
        )

    def durable_usage(self) -> Usage:
        return self.usage.model_copy(
            update={
                "estimated_input_tokens": self._usage.estimated_input_tokens
                + sum(item.input_tokens for item in self._reservations.values()),
                "estimated_output_tokens": self._usage.estimated_output_tokens
                + sum(item.output_tokens for item in self._reservations.values()),
                "estimated": self._usage.estimated or bool(self.reserved),
            }
        )

    @property
    def soft_target_exceeded(self):
        return (
            self.usage.input_tokens
            + self.usage.output_tokens
            + self.usage.estimated_input_tokens
            + self.usage.estimated_output_tokens
            + self.reserved
            >= self.limits.soft_token_target
        )

    def own_usage(self, owner):
        return self.owners.get(owner, Usage())

    def tool(self, owner="") -> None:
        self.check_time()
        if self.finalizing or self.research_seconds <= 0:
            raise DeadlineExceeded(
                "research deadline reached; finalize with current evidence"
            )
        self._usage = self._usage.model_copy(
            update={"tool_calls": self._usage.tool_calls + 1}
        )
        prior = self.owners.get(owner, Usage())
        self.owners[owner] = prior.model_copy(
            update={"tool_calls": prior.tool_calls + 1}
        )

    def delivery_tool(self, owner="") -> None:
        """Admit a deterministic delivery operation during the final reserve."""
        self.check_time()
        self._usage = self._usage.model_copy(
            update={"tool_calls": self._usage.tool_calls + 1}
        )
        prior = self.owners.get(owner, Usage())
        self.owners[owner] = prior.model_copy(
            update={"tool_calls": prior.tool_calls + 1}
        )


def estimate_tokens(text: str) -> int:
    return max(1, sum(1 if ord(char) > 127 else 0.35 for char in text).__ceil__())
