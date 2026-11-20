"""The terminal side of compare (#96): read ``/compare``, follow its events.

``/compare [-m a,b] <message>`` asks two to four models the same message
(``POST /chats/{id}/compare``) and offers to keep one answer
(``.../compare/keep``). The full-screen chat and the plain line chat share this
module, so both read the arguments and fold the per-model events the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kova.usage import footer

#: Same bounds as the server (``kova.compare``), kept here so the terminal can
#: explain a bad request without a round trip.
MIN_MODELS = 2
MAX_MODELS = 4

USAGE = "Usage: /compare [-m model,model] <message>"


class CompareError(ValueError):
    """The request cannot be made; the message says what to do instead."""


def parse(rest: str) -> tuple[list[str] | None, str]:
    """Split ``[-m a,b] message`` into the wanted models (None = choose) and the message."""
    rest = rest.strip()
    wanted: list[str] | None = None
    for flag in ("--models", "-m"):
        if rest.startswith(flag + "=") or rest == flag or rest.startswith(flag + " "):
            value, _, remainder = rest[len(flag):].lstrip("= ").partition(" ")
            wanted = [part.strip() for part in value.split(",") if part.strip()]
            if not wanted:
                raise CompareError("Name the models after -m, for example: /compare -m lab,cloud hello")
            rest = remainder.strip()
            break
    if not rest:
        raise CompareError(USAGE)
    return wanted, rest


def choose_models(configured: list[dict], active: str, wanted: list[str] | None) -> list[str]:
    """The models to ask: the ones named, or the active one first and then the others.

    Raises :class:`CompareError` for an unknown name, a repeat, or a count
    outside 2-4. Models Local only blocks are left to the server to refuse, so
    its explanation reaches the user unchanged.
    """
    names = [str(entry["name"]) for entry in configured]
    if wanted is not None:
        unknown = [name for name in wanted if name not in names]
        if unknown:
            raise CompareError(f'No model named "{unknown[0]}". Type /model to see them.')
        if len(set(wanted)) != len(wanted):
            raise CompareError("Name each model once.")
        chosen = list(wanted)
    else:
        chosen = ([active] if active in names else []) + [name for name in names if name != active]
        chosen = chosen[:MAX_MODELS]
    if len(chosen) < MIN_MODELS:
        raise CompareError(
            "Compare needs at least two models. Add another with kova setup, or name two with -m."
            if wanted is None else f"Name at least {MIN_MODELS} models to compare.")
    if len(chosen) > MAX_MODELS:
        raise CompareError(f"Compare up to {MAX_MODELS} models at once.")
    return chosen


@dataclass
class Answer:
    model: str
    text: str = ""
    phase: str = "waiting"      # waiting, thinking, writing, done, error, stopped
    usage: dict | None = None
    error: str = ""

    @property
    def keepable(self) -> bool:
        return self.phase == "done" and bool(self.text.strip())

    def stats(self) -> str:
        return footer(self.usage) if self.usage else ""


@dataclass
class Collector:
    """Folds a compare stream into one :class:`Answer` per model."""

    compare_id: str = ""
    answers: dict[str, Answer] = field(default_factory=dict)
    finished: str = ""          # "done" or "stopped" once the stream has ended
    error: str = ""             # a failure of the whole compare

    def feed(self, event: dict) -> None:
        kind = event.get("type")
        answer = self.answers.get(str(event.get("model") or ""))
        if kind == "start":
            self.compare_id = str(event.get("compare_id") or "")
            for name in event.get("models") or []:
                self.answers.setdefault(str(name), Answer(str(name)))
        elif kind == "text" and answer is not None:
            answer.text += str(event.get("text") or "")
            answer.phase = "writing"
        elif kind == "reasoning" and answer is not None and answer.phase == "waiting":
            answer.phase = "thinking"
        elif kind == "model_done" and answer is not None:
            answer.phase, answer.usage = "done", event.get("usage") or None
            if isinstance(event.get("text"), str):
                answer.text = event["text"]
        elif kind == "model_error" and answer is not None:
            answer.phase = "error"
            answer.error = str(event.get("message") or "This model could not answer.")
        elif kind in ("done", "stopped"):
            self.finished = kind
            for item in self.answers.values():
                if item.phase not in ("done", "error"):
                    item.phase = "stopped"
        elif kind == "error":
            self.error = str(event.get("message") or "The comparison failed.")

    def keepable(self) -> list[Answer]:
        return [answer for answer in self.answers.values() if answer.keepable]

    def progress(self) -> str:
        """One line for the spinner: who has answered so far."""
        total = len(self.answers)
        if not total:
            return "Comparing"
        done = sum(1 for answer in self.answers.values() if answer.phase in ("done", "error"))
        return f"Comparing {total} models · {done} of {total} answered"
