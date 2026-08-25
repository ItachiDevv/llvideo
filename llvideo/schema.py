"""The VideoIndex — structured data the agent reasons OVER.

This is the core of the design. The video-native model is a temporal SENSOR,
not a narrator. It returns typed, timestamped observations. The agent reads
those, then looks at real frames itself, and forms its own conclusion.

A prose summary would make the agent a middleman relaying someone else's
opinion. A typed index makes it an analyst working from evidence.
"""
from __future__ import annotations

# Google's Schema dialect: uppercase types, no $ref, no additionalProperties.
VIDEO_INDEX_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {
            "type": "STRING",
            "description": "Two sentences maximum. What this video is, and what happens in it.",
        },
        "content_kind": {
            "type": "STRING",
            "enum": ["screen_recording", "talking_head", "camera_footage", "animation",
                     "screencast_tutorial", "gameplay", "promo", "slideshow", "mixed", "other"],
        },
        "scenes": {
            "type": "ARRAY",
            "description": "Every distinct scene or shot, in order, covering the whole timeline.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start": {"type": "STRING", "description": "MM:SS or HH:MM:SS"},
                    "end": {"type": "STRING", "description": "MM:SS or HH:MM:SS"},
                    "description": {"type": "STRING", "description": "What is visible and what happens."},
                    "on_screen_text": {
                        "type": "ARRAY",
                        "description": (
                            "Text you can literally READ in the frame, character by character. "
                            "An empty array is the correct and expected answer for most camera "
                            "footage. Do NOT fill this field to be helpful. If a screen or sign "
                            "is present but blurred, out of focus, too small, or glare-washed, "
                            "add one entry with legibility 'illegible' and leave text empty — "
                            "never guess the characters, and never infer them from what such a "
                            "device or sign usually displays."
                        ),
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "text": {
                                    "type": "STRING",
                                    "description": "Exact characters read from the frame. Empty string if illegible.",
                                },
                                "legibility": {
                                    "type": "STRING",
                                    "enum": ["clear", "partial", "illegible"],
                                    "description": "'clear' only if you could type the characters from this frame alone with no context.",
                                },
                                "where": {
                                    "type": "STRING",
                                    "description": "Where it appears, e.g. 'dashboard display, lower left'.",
                                },
                            },
                            "required": ["text", "legibility", "where"],
                        },
                    },
                    "actions": {
                        "type": "ARRAY",
                        "description": "Discrete actions or events in this scene.",
                        "items": {"type": "STRING"},
                    },
                    "camera": {
                        "type": "STRING",
                        "description": "Shot type and camera movement, e.g. 'close-up, static' or 'wide, pan left'.",
                    },
                },
                "required": ["start", "end", "description", "on_screen_text", "actions"],
            },
        },
        "speech": {
            "type": "ARRAY",
            "description": "Spoken dialogue with timestamps. Empty array if there is no speech.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start": {"type": "STRING"},
                    "end": {"type": "STRING"},
                    "speaker": {"type": "STRING", "description": "Label such as 'speaker 1' or a name if stated."},
                    "text": {"type": "STRING"},
                },
                "required": ["start", "end", "text"],
            },
        },
        "audio_events": {
            "type": "ARRAY",
            "description": "Non-speech audio: music, effects, silence. Empty if no audio track.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start": {"type": "STRING"},
                    "end": {"type": "STRING"},
                    "description": {"type": "STRING"},
                },
                "required": ["start", "description"],
            },
        },
        "key_moments": {
            "type": "ARRAY",
            "description": "The handful of moments that matter most, for someone who will not watch it.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "timestamp": {"type": "STRING"},
                    "why": {"type": "STRING"},
                },
                "required": ["timestamp", "why"],
            },
        },
        "uncertainties": {
            "type": "ARRAY",
            "description": "Anything you could not determine, or where the footage is ambiguous. Be honest; do not fill gaps with plausible guesses.",
            "items": {"type": "STRING"},
        },
    },
    "required": ["summary", "content_kind", "scenes", "speech", "audio_events",
                 "key_moments", "uncertainties"],
}


ANSWER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answer": {"type": "STRING", "description": "Direct answer to the question asked."},
        "citations": {
            "type": "ARRAY",
            "description": "The timestamps that support the answer. At least one, unless the answer is that it cannot be determined.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "timestamp": {"type": "STRING"},
                    "observation": {"type": "STRING", "description": "What is visible at that exact moment."},
                },
                "required": ["timestamp", "observation"],
            },
        },
        "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "caveats": {
            "type": "ARRAY",
            "description": "What would change this answer, or what could not be seen clearly.",
            "items": {"type": "STRING"},
        },
    },
    "required": ["answer", "citations", "confidence", "caveats"],
}


_LEGIBILITY = """
THE ON-SCREEN TEXT RULE — this is the rule most often broken, so read it twice.

Only report text you can actually READ, character by character, in the frame.

A display can be visibly present and still be unreadable. Small, motion-blurred,
out-of-focus, glare-washed, or low-contrast text is UNREADABLE. In that case the
correct output is to say a display is present and that its content is not legible.

You must NEVER:
  - infer characters from what a device of that kind usually shows
  - complete a partially visible string from context
  - report a plausible number when the actual digits are blurred
  - report units (degrees, miles, currency) you cannot literally see

A wrong reading is far worse than no reading. If you are not certain of a
character, the text is not legible. Say so, and put it in `uncertainties`.

Test yourself: if you were shown this frame alone with no context, could you type
out the characters? If not, it is not legible.
"""

INDEX_PROMPT = """You are a video analysis sensor. Return observations, not opinions.

Rules:
- Cover the ENTIRE timeline. Do not skip quiet stretches; describe them as quiet.
- Timestamps must be real positions in this video, in MM:SS (or HH:MM:SS past an hour).
- If a scene is static, say so and give its full duration rather than inventing change.
- Put anything you are unsure about in `uncertainties`. An honest gap is more
  useful than a confident guess. Never invent detail to fill a field.
- Describe what is present. Do not identify specific songs, people, brands or
  places by name unless the video itself states the name in text or speech.
""" + _LEGIBILITY


ANSWER_PROMPT = """Answer the question from this video. You are a sensor reporting evidence.

Rules:
- Cite the exact timestamps your answer rests on, and say what is visible there.
- If the video does not show enough to answer, say so plainly and set confidence low.
  Do not speculate to produce an answer.
- Quote speech verbatim when it supports the answer.
""" + _LEGIBILITY + """
Question: """


def normalise_timestamp(value: str) -> float:
    """'01:23' or '01:02:03' or '83' -> seconds. Returns -1.0 if unparseable."""
    if value is None:
        return -1.0
    s = str(value).strip()
    if not s:
        return -1.0
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return -1.0
    return -1.0


def index_frame_times(index: dict, duration: float, limit: int = 16) -> list[float]:
    """Timestamps worth looking at with your own eyes: scene starts and key moments."""
    cand: list[float] = []
    for km in (index.get("key_moments") or []):
        t = normalise_timestamp(km.get("timestamp"))
        if 0 <= t <= duration:
            cand.append(t)
    for sc in (index.get("scenes") or []):
        t = normalise_timestamp(sc.get("start"))
        if 0 <= t <= duration:
            cand.append(t + 0.2)
    seen: list[float] = []
    for t in cand:
        if all(abs(t - u) > 0.75 for u in seen):
            seen.append(t)
    return sorted(seen)[:limit]
