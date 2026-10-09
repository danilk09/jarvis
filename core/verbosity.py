"""
How long spoken answers should be. Short by default (just the main point, since
everything is read aloud); "explain in depth", "go into more detail", "tell me
more"... switch the current command to a full explanation.
"""

import re

_DETAIL_RE = re.compile(
    r"\b(in[- ]depth|in (more |full |great(er)? |much more )?detail|more detail(s|ed)?|detailed|"
    r"elaborate|go deeper|dig deeper|tell me more|tell me everything|explain (it |this |that )?"
    r"(fully|thoroughly|properly|everything)|walk me through|break (it|this|that) down|"
    r"long(er)? (answer|version|explanation)|full (story|explanation|answer|breakdown))\b",
    re.IGNORECASE,
)

SHORT = ("Give only the main point: one short sentence, two at most, about 20 words in total. "
         "No background, caveats or pleasantries — it is read aloud.")
LONG  = ("The user asked for detail: explain thoroughly, up to 8 spoken sentences, "
         "covering the main point first and then the useful specifics.")

detailed = False   # set for each command in jarvis.py


def update(command: str) -> bool:
    """Decide the length for this command from how it was phrased."""
    global detailed
    detailed = bool(_DETAIL_RE.search(command or ""))
    return detailed


def rule(short: str = SHORT, long: str = LONG) -> str:
    return long if detailed else short


def tokens(short: int, long: int) -> int:
    return long if detailed else short
