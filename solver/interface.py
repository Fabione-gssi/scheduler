from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import Overrides, Problem, SolveLimits, Solution, Weights


class Solver(ABC):
    @abstractmethod
    def solve(
        self,
        problem: Problem,
        weights: Weights,
        overrides: Overrides | None = None,
        limits: SolveLimits | None = None,
    ) -> Solution:
        raise NotImplementedError
