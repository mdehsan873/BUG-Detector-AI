"""
AI cost tracking utility for Buglyft.

Tracks OpenAI token usage and estimated costs per call, per session,
and per analysis run. Thread-safe accumulator pattern.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.utils.logger import logger


# ── Pricing (USD per 1M tokens) ─────────────────────────────────────────────
# https://openai.com/api/pricing  (updated Feb 2025)
_MODEL_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate cost in USD for a single API call."""
    pricing = _MODEL_PRICING.get(model, _MODEL_PRICING["gpt-4o-mini"])
    input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 6)


@dataclass
class CallRecord:
    """A single OpenAI API call record."""
    function: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    session_id: str
    duration_ms: float


@dataclass
class CostTracker:
    """
    Accumulates AI cost data across an analysis run.
    Thread-safe for use with async/parallel calls.
    """
    calls: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.calls)

    @property
    def total_completion_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.calls)

    @property
    def total_tokens(self) -> int:
        return sum(c.total_tokens for c in self.calls)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 6)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def record(
        self,
        function: str,
        model: str,
        response: Any,
        session_id: str = "",
        duration_ms: float = 0.0,
    ) -> CallRecord:
        """
        Extract usage from an OpenAI response and record it.
        Returns the CallRecord for optional per-call logging.
        """
        usage = getattr(response, "usage", None)
        if not usage:
            return CallRecord(
                function=function, model=model,
                prompt_tokens=0, completion_tokens=0, total_tokens=0,
                cost_usd=0.0, session_id=session_id, duration_ms=duration_ms,
            )

        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        total = getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)
        cost = estimate_cost(model, prompt_tokens, completion_tokens)

        rec = CallRecord(
            function=function,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            cost_usd=cost,
            session_id=session_id,
            duration_ms=duration_ms,
        )

        with self._lock:
            self.calls.append(rec)

        return rec

    def get_session_cost(self, session_id: str) -> dict:
        """Get cost breakdown for a specific session."""
        session_calls = [c for c in self.calls if c.session_id == session_id]
        return {
            "session_id": session_id,
            "calls": len(session_calls),
            "prompt_tokens": sum(c.prompt_tokens for c in session_calls),
            "completion_tokens": sum(c.completion_tokens for c in session_calls),
            "total_tokens": sum(c.total_tokens for c in session_calls),
            "cost_usd": round(sum(c.cost_usd for c in session_calls), 6),
        }

    def get_function_breakdown(self) -> dict[str, dict]:
        """Get cost breakdown by function name."""
        funcs: dict[str, list[CallRecord]] = {}
        for c in self.calls:
            funcs.setdefault(c.function, []).append(c)
        return {
            name: {
                "calls": len(recs),
                "prompt_tokens": sum(r.prompt_tokens for r in recs),
                "completion_tokens": sum(r.completion_tokens for r in recs),
                "total_tokens": sum(r.total_tokens for r in recs),
                "cost_usd": round(sum(r.cost_usd for r in recs), 6),
                "avg_duration_ms": round(sum(r.duration_ms for r in recs) / len(recs), 1) if recs else 0,
            }
            for name, recs in funcs.items()
        }

    def summary(self) -> dict:
        """Full cost summary for the analysis run."""
        return {
            "total_calls": self.call_count,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "by_function": self.get_function_breakdown(),
        }

    def log_summary(self, analysis_id: str = "") -> None:
        """Log a human-readable cost summary."""
        s = self.summary()
        prefix = f"[{analysis_id}] " if analysis_id else ""

        logger.info(
            f"{prefix}AI Cost Summary: "
            f"{s['total_calls']} calls | "
            f"{s['total_tokens']:,} tokens "
            f"({s['total_prompt_tokens']:,} in / {s['total_completion_tokens']:,} out) | "
            f"${s['total_cost_usd']:.4f} USD"
        )

        for func_name, breakdown in s["by_function"].items():
            logger.info(
                f"{prefix}  {func_name}: "
                f"{breakdown['calls']} calls | "
                f"{breakdown['total_tokens']:,} tokens | "
                f"${breakdown['cost_usd']:.4f} | "
                f"avg {breakdown['avg_duration_ms']:.0f}ms"
            )

    def log_session_cost(self, session_id: str) -> None:
        """Log cost for a single session."""
        sc = self.get_session_cost(session_id)
        if sc["calls"] > 0:
            logger.info(
                f"Session {session_id[:12]}… cost: "
                f"{sc['calls']} calls | "
                f"{sc['total_tokens']:,} tokens | "
                f"${sc['cost_usd']:.4f} USD"
            )
