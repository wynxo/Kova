"""What each answer cost and how fast it was (#75).

Everything here is local: token counts come from the model server's own
``usage`` block (or a rough estimate when it sends none), timing comes from the
clock in :class:`TurnMeter`, and prices come from the small built-in table
below plus ``[pricing]`` overrides in ``config.toml``. Nothing is looked up
over the network.

A price is only shown when Kova knows it. An unknown model shows tokens and
speed, never a made-up number; a local model costs ``$0``.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kova.privacy import is_loopback_url

#: When the built-in prices were last compared with the providers' public
#: price pages. Prices change; ``[pricing]`` in config.toml overrides them.
PRICES_CHECKED = "2025-11"

#: USD per million (input, output) tokens, keyed by provider kind and model id.
#: Only list prices you have checked. A dated snapshot of a listed id (such as
#: ``gpt-4o-2024-08-06``) uses the same price unless it has its own row.
PRICING: dict[str, dict[str, tuple[float, float]]] = {
    "openai": {
        "gpt-4o": (2.50, 10.00),
        "gpt-4o-2024-05-13": (5.00, 15.00),
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4.1": (2.00, 8.00),
        "gpt-4.1-mini": (0.40, 1.60),
        "gpt-4.1-nano": (0.10, 0.40),
        "gpt-5": (1.25, 10.00),
        "gpt-5-mini": (0.25, 2.00),
        "gpt-5-nano": (0.05, 0.40),
        "o3": (2.00, 8.00),
        "o4-mini": (1.10, 4.40),
    },
    "anthropic": {
        "claude-3-haiku": (0.25, 1.25),
        "claude-3-5-haiku": (0.80, 4.00),
        "claude-3-7-sonnet": (3.00, 15.00),
        "claude-sonnet-4": (3.00, 15.00),
        "claude-sonnet-4-5": (3.00, 15.00),
        "claude-haiku-4-5": (1.00, 5.00),
        "claude-opus-4": (15.00, 75.00),
        "claude-opus-4-1": (15.00, 75.00),
        "claude-opus-4-5": (5.00, 25.00),
    },
}

#: Provider kinds that always run on the user's own hardware.
LOCAL_KINDS = frozenset({"local"})

# A dated snapshot or alias of a listed model: ``-20250514``, ``-2024-08-06``, ``-latest``.
_SNAPSHOT = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2}|latest)")

#: Rough characters per token, for servers that report no usage.
CHARS_PER_TOKEN = 4
#: Rough tokens for one inline image in an estimate (providers vary by size).
IMAGE_TOKENS = 1000


@dataclass(frozen=True)
class Price:
    """USD per million input and output tokens, and where the number came from."""
    input: float
    output: float
    source: str  # "local", "builtin" or "config"

    @property
    def local(self) -> bool:
        return self.source == "local"

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.input + completion_tokens * self.output) / 1_000_000


def parse_pricing(table: object) -> dict[str, tuple[float, float]]:
    """``[pricing]`` overrides as ``{key: (input, output)}``; bad rows are skipped.

    Each row is ``"<model>" = { input = 2.5, output = 10 }`` in USD per million
    tokens. The key may be a Kova model name, a model id, or ``kind/model-id``.
    """
    if not isinstance(table, Mapping):
        return {}
    prices = {}
    for key, row in table.items():
        if not isinstance(row, Mapping):
            continue
        try:
            values = float(row.get("input", 0)), float(row.get("output", 0))
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(v) and v >= 0 for v in values):
            prices[str(key)] = values
    return prices


def _builtin(kind: str, model: str) -> tuple[float, float] | None:
    table = PRICING.get(kind)
    if not table:
        return None
    if model in table:
        return table[model]
    # The longest listed id this model is a dated snapshot of.
    for key in sorted(table, key=len, reverse=True):
        if model.startswith(key) and _SNAPSHOT.fullmatch(model[len(key):]):
            return table[key]
    return None


def is_local(entry) -> bool:
    """Whether a model entry runs on this PC (or the user's own server)."""
    provider = getattr(entry, "_provider", None)
    kind = getattr(provider, "kind", "") or ""
    return (kind in LOCAL_KINDS or (entry.provider or "").startswith("managed-")
            or is_loopback_url(entry.base_url or ""))


def price_for(entry, overrides: Mapping[str, tuple[float, float]] | None = None) -> Price | None:
    """The price for one configured model, or ``None`` when Kova does not know it."""
    overrides = overrides or {}
    provider = getattr(entry, "_provider", None)
    kind = getattr(provider, "kind", "") or ""
    model = entry.model or ""
    for key in (entry.name, f"{kind}/{model}" if kind else None, model):
        if key and key in overrides:
            return Price(*overrides[key], "config")
    if is_local(entry):
        return Price(0.0, 0.0, "local")
    builtin = _builtin(kind, model)
    return Price(*builtin, "builtin") if builtin else None


def _without_images(value: object, found: list[int]) -> object:
    """A copy with each inline image replaced by a marker, counting the images."""
    if isinstance(value, Mapping):
        if value.get("type") in ("image_url", "input_image", "image"):
            found.append(1)
            return {"type": "image"}
        return {key: _without_images(item, found) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_without_images(item, found) for item in value]
    return value


def estimate_tokens(value: object) -> int:
    """About how many tokens a string or a request body is (never below 1 for text).

    Inline images count as ``IMAGE_TOKENS`` each, not as their base64 text.
    """
    if not value:
        return 0
    if isinstance(value, str):
        return max(1, math.ceil(len(value) / CHARS_PER_TOKEN))
    images: list[int] = []
    text = json.dumps(_without_images(value, images), ensure_ascii=False)
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN)) + IMAGE_TOKENS * len(images)


def normalize_usage(raw: object) -> dict[str, int] | None:
    """Map an OpenAI, Anthropic or Responses usage block to prompt/completion tokens."""
    if not isinstance(raw, Mapping):
        return None

    def number(*keys: str) -> int | None:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return int(value)
        return None

    prompt = number("prompt_tokens", "input_tokens")
    completion = number("completion_tokens", "output_tokens")
    if prompt is None and completion is None:
        return None
    # Anthropic counts cached prompt tokens separately from input_tokens.
    prompt = (prompt or 0) + (number("cache_creation_input_tokens") or 0) + (number("cache_read_input_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion or 0}


class TurnMeter:
    """Add up tokens and time across every model round of one answer.

    ``ms_first_token`` is measured from the start of the turn, so it is the wait
    the user sees. ``ms_generation`` only counts time while a model was
    streaming output, so tokens per second is the model's speed and not the
    time spent running tools in between.
    """

    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.started = clock()
        self.first: float | None = None
        self.generation = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated = False
        self._round_first: float | None = None
        self._round_usage: dict | None = None
        self._round_chars = 0

    def start_round(self) -> None:
        self._round_first = None
        self._round_usage = None
        self._round_chars = 0

    def output(self, text: str = "") -> None:
        """Note streamed output: text, reasoning or tool-call arguments."""
        now = self.clock()
        if self._round_first is None:
            self._round_first = now
        if self.first is None:
            self.first = now
        self._round_chars += len(text)

    def count(self, text: str) -> None:
        """Count output for the estimate without touching timing (a finished tool call)."""
        self._round_chars += len(text or "")

    def usage(self, usage: dict | None) -> None:
        if usage:
            self._round_usage = usage

    def end_round(self, request: object) -> None:
        if self._round_first is not None:
            self.generation += self.clock() - self._round_first
        usage = self._round_usage
        if usage:
            self.prompt_tokens += usage["prompt_tokens"]
            self.completion_tokens += usage["completion_tokens"]
        else:
            self.estimated = True
            self.prompt_tokens += estimate_tokens(request)
            self.completion_tokens += math.ceil(self._round_chars / CHARS_PER_TOKEN)
        self.start_round()

    def summary(self, entry, overrides: Mapping[str, tuple[float, float]] | None = None) -> dict:
        now = self.clock()
        ms = lambda seconds: max(0, round(seconds * 1000))  # noqa: E731
        price = price_for(entry, overrides)
        generation = ms(self.generation)
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "estimated": self.estimated,
            "ms_first_token": ms(self.first - self.started) if self.first is not None else None,
            "ms_generation": generation,
            "ms_total": ms(now - self.started),
            "tokens_per_second": round(self.completion_tokens / (generation / 1000), 1) if generation > 0 else None,
            "cost_usd": round(price.cost(self.prompt_tokens, self.completion_tokens), 6) if price else None,
            "local": bool(price and price.local),
            "model": entry.name,
        }


def tokens_per_second(completion_tokens: int, ms_generation: int | None) -> float | None:
    return round(completion_tokens / (ms_generation / 1000), 1) if ms_generation else None


def _local_day(created: str, tz=None) -> str:
    """The calendar day of a stored time in ``tz`` (the machine's zone when None)."""
    try:
        return datetime.fromisoformat(created).astimezone(tz).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _bucket() -> dict:
    return {"answers": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
            "unpriced": 0, "estimated": 0, "ms_generation": 0}


def _add(bucket: dict, row: Mapping) -> None:
    bucket["answers"] += 1
    bucket["prompt_tokens"] += row["prompt_tokens"] or 0
    bucket["completion_tokens"] += row["completion_tokens"] or 0
    bucket["ms_generation"] += row["ms_generation"] or 0
    if row["cost_usd"] is None:
        bucket["unpriced"] += 1
    else:
        bucket["cost_usd"] += row["cost_usd"]
    if row["usage_estimated"]:
        bucket["estimated"] += 1


def _finish(bucket: dict) -> dict:
    bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    bucket["tokens_per_second"] = tokens_per_second(bucket["completion_tokens"], bucket["ms_generation"])
    return bucket


def summarize(rows: list[Mapping], *, days: int, now: datetime | None = None,
              budget_usd: float = 0.0) -> dict:
    """Totals for today, this month and the last ``days`` days, by day and by model.

    ``rows`` are stored answers with usage (see ``Store.usage_rows``), which must
    cover at least the start of this month and the start of the window. Days
    are the user's local calendar days.
    """
    now = now or datetime.now().astimezone()
    today = now.date()
    first = (today - timedelta(days=max(1, days) - 1)).isoformat()
    month = today.replace(day=1).isoformat()
    total, today_bucket, month_bucket = _bucket(), _bucket(), _bucket()
    by_day: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    for row in rows:
        day = _local_day(row["created"], now.tzinfo)
        if not day:
            continue
        if day >= month:
            _add(month_bucket, row)
        if day == today.isoformat():
            _add(today_bucket, row)
        if day < first:
            continue
        _add(total, row)
        _add(by_day.setdefault(day, {"day": day, **_bucket()}), row)
        model = row["usage_model"] or "unknown"
        _add(by_model.setdefault(model, {"model": model, **_bucket()}), row)
    spent = round(month_bucket["cost_usd"], 6)
    budget = {"monthly_usd": budget_usd, "spent_usd": spent,
              "ratio": round(spent / budget_usd, 4) if budget_usd > 0 else None}
    budget["warning"] = bool(budget_usd > 0 and spent >= 0.8 * budget_usd)
    return {
        "days": max(1, days),
        "since": first,
        "total": _finish(total),
        "today": _finish(today_bucket),
        "month": _finish(month_bucket),
        "by_day": [_finish(by_day[key]) for key in sorted(by_day)],
        "by_model": sorted((_finish(b) for b in by_model.values()),
                           key=lambda b: (-b["cost_usd"], -b["completion_tokens"], b["model"])),
        "budget": budget,
        "prices_checked": PRICES_CHECKED,
    }


def window_start(days: int, now: datetime | None = None) -> str:
    """The UTC ISO time to read stored answers from: the earlier of the window and the month."""
    now = now or datetime.now().astimezone()
    first = now.date() - timedelta(days=max(1, days) - 1)
    first = min(first, now.date().replace(day=1)) - timedelta(days=1)  # a day of slack for time zones
    # Stored timestamps are UTC ISO strings, so compare against one.
    return datetime.combine(first, datetime.min.time(), tzinfo=now.tzinfo).astimezone(timezone.utc).isoformat()


def short_tokens(count: int) -> str:
    """``950``, ``1.2k``, ``34k``, ``1.5M``."""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k".replace(".0k", "k")
    if count < 1_000_000:
        return f"{round(count / 1000)}k"
    return f"{count / 1_000_000:.1f}M".replace(".0M", "M")


def money(value: float | None, *, local: bool = False) -> str:
    if local:
        return "$0.00 local"
    if value is None:
        return "price unknown"
    if value == 0:
        return "$0.00"
    if value < 0.01:
        return f"${value:.4f}".rstrip("0")
    return f"${value:.2f}"


def footer(usage: Mapping) -> str:
    """One line for under an answer: ``1.2k in · 340 out · ~$0.004 · 38 tok/s``."""
    mark = "~" if usage.get("estimated") else ""
    parts = [f"{mark}{short_tokens(usage.get('prompt_tokens') or 0)} in",
             f"{mark}{short_tokens(usage.get('completion_tokens') or 0)} out"]
    if usage.get("local"):
        parts.append("$0.00 local")
    elif usage.get("cost_usd") is not None:
        parts.append("~" + money(usage["cost_usd"]))
    speed = usage.get("tokens_per_second")
    if speed:
        parts.append(f"{speed:g} tok/s")
    return " · ".join(parts)
