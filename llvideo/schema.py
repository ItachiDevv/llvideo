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


# ---------------------------------------------------------------------------
# Craft analysis — how a video is SHOT and CUT, not what is said in it.
#
# This needs dense sampling. A hard cut occupies one frame; at 1 fps you see
# the shots either side and never the transition itself. So the craft path
# raises fps inside short windows rather than sampling the whole timeline.
# ---------------------------------------------------------------------------

TRANSITION_KINDS = [
    "hard_cut", "crossfade", "fade_to_black", "fade_from_black", "fade_to_white",
    "wipe", "slide", "whip_pan", "match_cut", "jump_cut", "morph", "zoom_transition",
    "glitch", "light_leak", "none",
]

CAMERA_MOVES = [
    "static", "pan_left", "pan_right", "tilt_up", "tilt_down", "dolly_in", "dolly_out",
    "truck", "crane", "handheld", "steadicam", "zoom_in", "zoom_out", "whip_pan",
    "orbit", "drone", "rack_focus",
]

SHOT_SIZES = [
    "extreme_wide", "wide", "medium_wide", "medium", "medium_close", "close_up",
    "extreme_close_up", "over_the_shoulder", "two_shot", "insert", "pov", "aerial",
]

CRAFT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "overall": {
            "type": "OBJECT",
            "properties": {
                "style": {"type": "STRING", "description": "The editing and visual style in one sentence."},
                "pacing": {"type": "STRING", "enum": ["very_slow", "slow", "moderate", "fast", "very_fast", "varied"]},
                "pacing_note": {"type": "STRING", "description": "How the rhythm changes across the piece."},
                "colour": {"type": "STRING", "description": "Palette and grade — warm/cool, contrast, saturation, any obvious LUT."},
                "lighting": {"type": "STRING", "description": "Key quality, direction, motivation, practicals."},
            },
            "required": ["style", "pacing", "pacing_note", "colour", "lighting"],
        },
        "shots": {
            "type": "ARRAY",
            "description": "Every distinct SHOT — a continuous run of camera between two transitions. A camera move within one take is NOT a new shot.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start": {"type": "STRING"},
                    "end": {"type": "STRING"},
                    "shot_size": {"type": "STRING", "enum": SHOT_SIZES},
                    "camera_move": {"type": "STRING", "enum": CAMERA_MOVES},
                    "subject": {"type": "STRING", "description": "What the shot is of."},
                    "composition": {"type": "STRING", "description": "Framing, balance, leading lines, depth, rule of thirds."},
                    "notable": {"type": "STRING", "description": "Anything a colourist or editor would flag. Empty if nothing."},
                },
                "required": ["start", "end", "shot_size", "camera_move", "subject", "composition"],
            },
        },
        "transitions": {
            "type": "ARRAY",
            "description": "Every transition BETWEEN shots. Include the type and how long it takes.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "at": {"type": "STRING", "description": "MM:SS where the transition begins."},
                    "kind": {"type": "STRING", "enum": TRANSITION_KINDS},
                    "duration_seconds": {"type": "NUMBER", "description": "0 for a hard cut. Otherwise how long the blend lasts."},
                    "from_shot": {"type": "STRING", "description": "What is on screen going in."},
                    "to_shot": {"type": "STRING", "description": "What is on screen coming out."},
                    "motivation": {"type": "STRING", "description": "Why the editor cut here — on action, on beat, on dialogue, on a look. Say 'unclear' honestly."},
                    "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
                },
                "required": ["at", "kind", "duration_seconds", "from_shot", "to_shot", "motivation", "confidence"],
            },
        },
        "rhythm": {
            "type": "ARRAY",
            "description": "Shot lengths in order, seconds. Lets the caller compute pacing statistics.",
            "items": {"type": "NUMBER"},
        },
        "uncertainties": {
            "type": "ARRAY",
            "description": "Transitions you could not classify, or moments where camera movement and a cut are hard to tell apart. Be honest.",
            "items": {"type": "STRING"},
        },
    },
    "required": ["overall", "shots", "transitions", "rhythm", "uncertainties"],
}


CRAFT_PROMPT = """You are a film editor breaking down how this video is SHOT and CUT.
Ignore what is being said. The subject is the craft.

Report:
- Every SHOT: size, camera movement, subject, composition.
- Every TRANSITION between shots: the type, how long it lasts, and why the cut lands there.
- Pacing, colour grade, and lighting.

Rules that decide whether this is any good:

1. A CAMERA MOVE IS NOT A CUT. A whip pan, a fast tilt, or a rack focus can look
   like an edit at low frame rates, but the footage is continuous. If frames flow
   into each other with motion blur and no content jump, it is one shot with
   movement — say so, and classify the move.

2. A HARD CUT is instantaneous — one frame is shot A, the next is shot B, with no
   blended frames between them. If you can see frames containing BOTH images mixed
   together, it is a crossfade, not a cut, and you must give its duration.

3. Distinguish the blend types. A crossfade mixes two images directly. A fade to
   black passes through black between them. A wipe moves a hard edge across the
   frame. A slide pushes one image off as the other comes on.

4. Give transition timing from what you actually see, not from a guess. If you
   cannot tell a 0.3s dissolve from a hard cut at this sampling rate, set
   confidence low and say so in `uncertainties`.

5. Never invent a transition to fill a gap between shots you noticed. Missing one
   is better than inventing one.
"""
