"""Self-consistency checking.

Prompt engineering cannot fully stop a model from reading blurred text. But an
unreliable reading betrays itself: ask the same question twice and the answer
changes. A reliable one comes back identical.

Measured on real footage — a motion-blurred car dashboard, asked three times:
    call 1: "642"  / "55 F" / "330 mi"
    call 2: "23.9" / "65 F" / "330"
    call 3: "6:42" / "55 F" / "330 mi"
"330 mi" held; the temperature and the first field did not. The agreement score
separates the two automatically, with no human in the loop.

This costs one extra call per repeat. At roughly $0.003 for a short window, that
is a good trade whenever the answer will be acted on.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field


def _normalise(text: str) -> str:
    """Fold away formatting so '55 °F' and '55F' compare equal."""
    s = (text or "").lower().strip()
    s = s.replace("°", "").replace("_", " ")
    s = re.sub(r"[^\w\s.:/-]", "", s)
    s = re.sub(r"\s+", "", s)
    return s


@dataclass
class Agreement:
    value: str
    votes: int
    trials: int
    variants: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.votes / self.trials if self.trials else 0.0

    @property
    def verdict(self) -> str:
        if self.ratio >= 0.99:
            return "unanimous"
        if self.ratio >= 0.67:
            return "majority"
        return "unreliable"

    def to_dict(self) -> dict:
        return {"value": self.value, "votes": self.votes, "trials": self.trials,
                "ratio": round(self.ratio, 2), "verdict": self.verdict,
                "variants": self.variants}


def agree(answers: list[str]) -> Agreement:
    """Majority vote over normalised answers."""
    answers = [a for a in answers if a is not None]
    if not answers:
        return Agreement("", 0, 0)
    norm = [_normalise(a) for a in answers]
    counts = Counter(norm)
    top_norm, votes = counts.most_common(1)[0]
    original = next(a for a, n in zip(answers, norm) if n == top_norm)
    variants = sorted({a for a, n in zip(answers, norm) if n != top_norm})
    return Agreement(original, votes, len(answers), variants)


def check_text_claims(runs: list[dict]) -> list[dict]:
    """Compare on_screen_text across repeated index runs.

    Any string that does not appear in every run is flagged. This is the field
    that hallucinates most, so it gets the scrutiny.
    """
    per_run: list[set[str]] = []
    display: dict[str, str] = {}
    for r in runs:
        seen: set[str] = set()
        for sc in (r.get("scenes") or []):
            for item in (sc.get("on_screen_text") or []):
                raw = item.get("text", "") if isinstance(item, dict) else str(item)
                if not raw:
                    continue
                key = _normalise(raw)
                if key:
                    seen.add(key)
                    display.setdefault(key, raw)
        per_run.append(seen)

    if not per_run:
        return []
    all_keys: set[str] = set().union(*per_run)
    out = []
    for key in sorted(all_keys):
        hits = sum(1 for s in per_run if key in s)
        out.append({
            "text": display[key],
            "seen_in": hits,
            "of": len(per_run),
            "verdict": ("stable" if hits == len(per_run)
                        else "unstable — the model did not read this the same way twice"),
        })
    return out


def summarise(checks: list[dict]) -> dict:
    stable = [c for c in checks if c["verdict"] == "stable"]
    unstable = [c for c in checks if c["verdict"] != "stable"]
    return {
        "claims": checks,
        "stable": [c["text"] for c in stable],
        "unstable": [c["text"] for c in unstable],
        "note": (
            "Unstable readings changed between identical calls, so they are not reliable. "
            "Treat them as 'a display is present but not legible'. Confirm anything that "
            "matters by exporting a full-resolution still and looking at it yourself."
            if unstable else
            "Every on-screen text reading was identical across runs."
        ),
    }
