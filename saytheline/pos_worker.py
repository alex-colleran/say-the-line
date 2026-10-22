"""Emphasis tier for each caption word, from spaCy parts of speech and dependencies.

Runs in the STT venv (spaCy + en_core_web_sm live there, not in the web server's Python):
  echo '{"words": ["I", "am", "the", "one", "who", "knocks."]}' | python -I pos_worker.py
prints one tier per word:
  4  exclamations, greetings, expletives      (largest, highlight colour)
  3  the core of the clause: subject/object nouns, names, the main verb, numbers
  2  other content words: nouns, verbs, adjectives, adverbs, subject pronouns
  1  function words: articles, prepositions, conjunctions, auxiliaries, other pronouns
"""
import json
import re
import sys

import spacy

EMPHATIC = {
    "oh", "ah", "wow", "whoa", "hey", "hi", "hello", "yo", "bye", "goodbye", "yes", "no", "nope", "yeah",
    "damn", "dammit", "goddamn", "goddamnit", "hell", "shit", "fuck", "fucking", "fucker", "crap", "bitch",
    "bastard", "asshole", "christ", "jesus", "bloody", "god", "omg", "wait", "stop", "please", "thanks",
}
CORE_DEPS = {"nsubj", "nsubjpass", "dobj", "attr", "pobj", "acomp", "oprd", "npadvmod", "appos"}

nlp = spacy.load("en_core_web_sm", disable=["ner", "lemmatizer"])


def token_tier(t):
    low = t.text.lower()
    if t.pos_ == "INTJ" or low in EMPHATIC:
        return 4
    if t.pos_ in ("PUNCT", "SYM", "SPACE"):
        return 0
    if t.pos_ in ("DET", "ADP", "CCONJ", "SCONJ", "PART", "AUX"):
        return 1
    if t.pos_ == "PRON":
        return 2 if t.dep_ in ("nsubj", "nsubjpass") else 1
    if t.pos_ == "PROPN" or t.pos_ == "NUM":
        return 3
    if t.pos_ in ("NOUN", "ADJ") and t.dep_ in CORE_DEPS:
        return 3
    if t.pos_ == "VERB" and t.dep_ in ("ROOT", "relcl", "xcomp", "ccomp"):
        return 3
    return 2


def main():
    words = json.load(sys.stdin)["words"]
    text, spans = "", []
    for w in words:                      # tag the words as one text, then map tokens back by offset
        if text:
            text += " "
        spans.append((len(text), len(text) + len(w)))
        text += w
    doc = nlp(text)
    tiers = []
    for (a, b), w in zip(spans, words):
        toks = [t for t in doc if t.idx < b and t.idx + len(t) > a]
        tier = max((token_tier(t) for t in toks), default=1) or 1
        bare = re.sub(r"[^\w']", "", w)
        if w.endswith("!") or (len(bare) > 1 and bare.isupper() and not bare.isdigit()):
            tier = max(tier, 4 if w.endswith("!") else 3)          # shouted
        tiers.append(tier)
    json.dump(tiers, sys.stdout)


if __name__ == "__main__":
    main()
