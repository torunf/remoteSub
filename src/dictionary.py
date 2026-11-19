"""Pre-translated subtitle dictionaries: a JSON object mapping source lines to translations.

A dictionary built from a game's own text tables answers most subtitles instantly (well under a
millisecond) with a human translation; only lines it doesn't know go to the model. OCR rarely
reads a line perfectly, so close matches count too.
"""
import json

from rapidfuzz import fuzz, process

FUZZY_CUTOFF = 88  # rapidfuzz ratio; below this, distinct lines start to match each other
MIN_FUZZY_LENGTH = 6  # very short lines ("Or?") match the wrong entry too easily

_TYPOGRAPHY = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "…": "..."})


def normalize(text):
    return " ".join(text.translate(_TYPOGRAPHY).split())


class Dictionary:
    def __init__(self, path=None, raw=None):
        if path is not None:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        raw = raw or {}
        self.pairs = {normalize(k): v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
        self.keys = list(self.pairs)

    def put(self, source, translation):
        """Add a line, as the learned dictionary does with every new translation."""
        key = normalize(source)
        if key not in self.pairs:
            self.keys.append(key)
        self.pairs[key] = translation

    def __len__(self):
        return len(self.pairs)

    def lookup(self, text):
        line = normalize(text)
        if line in self.pairs:
            return self.pairs[line]
        if len(line) < MIN_FUZZY_LENGTH:
            return None
        best = process.extractOne(line, self.keys, scorer=fuzz.ratio, score_cutoff=FUZZY_CUTOFF)
        return self.pairs[best[0]] if best else None
