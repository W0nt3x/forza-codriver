"""The co-driver vocabulary, every token a stage can ask for.

One place maps a token (what stage files and the scheduler use) to the text
a TTS engine should speak for it, per language. The token keys are
language-neutral and never change: a stage built once works with every voice
pack, because "tightens" is a token, and "zieht zu" is merely how the German
pack pronounces it.

Phrasing follows how co-drivers actually deliver the calls ("one fifty", not
"one hundred and fifty"; "zieht zu", not a dictionary translation of
"tightens").

A voice pack does not have to cover all of this, the loader reports what is
missing and falls back to a beep for those tokens, but a generated pack
covers everything, so hand-edited stages can use words the generator does
not emit yet.
"""

from __future__ import annotations

# token -> text handed to the TTS engine
VOCABULARY: dict[str, str] = {
    # corner classes
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    # direction
    "left": "left",
    "right": "right",
    # special corners
    "hairpin": "hairpin",
    "square": "square",
    # modifiers
    "long": "long",
    "short": "short",
    "tightens": "tightens",
    "opens": "opens",
    "cut": "cut",
    "dont_cut": "don't cut",
    "caution": "caution",
    "care": "care",
    # hazards
    "crest": "crest",
    "over_crest": "over crest",
    "jump": "jump",
    "dip": "dip",
    "bump": "bump",
    "narrows": "narrows",
    # distances, rally phrasing
    "30": "thirty",
    "50": "fifty",
    "70": "seventy",
    "100": "one hundred",
    "150": "one fifty",
    "200": "two hundred",
    "250": "two fifty",
    "300": "three hundred",
    "400": "four hundred",
    "500": "five hundred",
    # links
    "into": "into",
    "and": "and",
    "then": "then",
    # stage bookends
    "start": "start",
    "finish": "finish",
    "keep_left": "keep left",
    "keep_right": "keep right",
}


VOCABULARY_DE: dict[str, str] = {
    # corner classes
    "1": "eins",
    "2": "zwei",
    "3": "drei",
    "4": "vier",
    "5": "fünf",
    "6": "sechs",
    # direction
    "left": "links",
    "right": "rechts",
    # special corners
    "hairpin": "Spitzkehre",
    "square": "eckig",
    # modifiers, German co-driver phrasing, not dictionary translation
    "long": "lang",
    "short": "kurz",
    "tightens": "zieht zu",
    "opens": "geht auf",
    "cut": "schneiden",
    "dont_cut": "nicht schneiden",
    "caution": "Achtung",
    "care": "Vorsicht",
    # hazards
    "crest": "Kuppe",
    "over_crest": "über Kuppe",
    "jump": "Sprung",
    "dip": "Senke",
    "bump": "Buckel",
    "narrows": "wird eng",
    # distances
    "30": "dreißig",
    "50": "fünfzig",
    "70": "siebzig",
    "100": "hundert",
    "150": "hundertfünfzig",
    "200": "zweihundert",
    "250": "zweihundertfünfzig",
    "300": "dreihundert",
    "400": "vierhundert",
    "500": "fünfhundert",
    # links
    "into": "in",
    "and": "und",
    "then": "dann",
    # stage bookends
    "start": "Start",
    "finish": "Ziel",
    "keep_left": "links halten",
    "keep_right": "rechts halten",
}

VOCABULARIES: dict[str, dict[str, str]] = {
    "en": VOCABULARY,
    "de": VOCABULARY_DE,
}

# The voice a language gets unless the user picks one. All edge-tts names;
# `edge-tts --list-voices` shows the alternatives.
DEFAULT_VOICES: dict[str, str] = {
    "en": "en-GB-RyanNeural",
    "de": "de-DE-ConradNeural",
}


def vocabulary(language: str = "en") -> dict[str, str]:
    try:
        return VOCABULARIES[language]
    except KeyError:
        raise ValueError(
            f"no vocabulary for language {language!r}; "
            f"available: {sorted(VOCABULARIES)}"
        ) from None


def spoken_text(token: str, language: str = "en") -> str:
    """What to say for a token. Unknown tokens are spoken literally, so a
    hand-edited stage with a custom word still gets *something*."""
    return vocabulary(language).get(token, token.replace("_", " "))
