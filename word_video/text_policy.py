"""Spoken-text policy: what the Chinese reading says, derived from the display meaning.

Tim approved on 2026-09-16 that **only the derived reading field** is cleaned.  The
word, the phonetic, the display meaning and the protected legacy subtitle core keep
whatever the word list says; the text that will actually be read aloud is derived
here from the display meaning.

The rule is deliberately the *same expression* the independent M0 acceptance tool
applies (``tools/m0/accept_range.py::spoken_of``), so "what the pipeline produced"
and "what the checker expects" are one rule instead of two drifting copies.  The
same tool recomputes the Chinese stage length from the cleaned text, so cleaning a
line changes that stage's duration -- expected, not a defect.

Policies
    ``v2``        the approved rule below (default).
    ``legacy``    what the delivered batches were built with: each part-of-speech
                  tag is removed on its own and nothing else, so the connectors
                  around a tag survive ("n. & v. 谋 杀" is read as "& 谋 杀") and an
                  embedded "/kənˈtent/" is still read out.  Kept so an old batch
                  can be reproduced and compared, never for new production.
    ``supplied``  provenance only: the request carried its own reading text, so no
                  policy was applied to it.  Never requested by a user.

v2, step by step (only the derived reading field):
    1. a part-of-speech tag is removed together with the connectors that join
       tags (``& / , ，、``) -- but not when a Latin letter precedes it, so the
       real word forms ``R&D`` and ``A/B`` survive;
    2. an embedded phonetic (``/.../``) is removed;
    3. runs of whitespace collapse and the ends are trimmed.
Nothing is rewritten, corrected, completed or reordered: a leftover the rule
cannot judge is left exactly as it is and reported by :func:`leftover_connectors`.
"""
import re

POLICY_V2 = 'v2'
POLICY_LEGACY = 'legacy'
POLICY_SUPPLIED = 'supplied'
#: Policies a request may ask for; ``supplied`` is provenance, not a request.
POLICIES = (POLICY_V2, POLICY_LEGACY)
ALL_POLICIES = POLICIES + (POLICY_SUPPLIED,)
DEFAULT_POLICY = POLICY_V2

# The tag list and the two expressions are the M0 acceptance tool's, verbatim.
_POS_WORD = r'(?:n|v|vt|vi|adj|adv|prep|conj|pron|num|art|int|aux|abbr|pl)\.'
# ``\b`` is useless next to a Chinese character ("根本的n." has no word boundary),
# so only a preceding Latin letter is rejected: that catches a tag glued to Chinese
# while refusing to eat the tail of an English word such as "print.".
_TAG = re.compile(r'(?<![A-Za-z])%s' % _POS_WORD)
_TAG_RUN = re.compile(r'(?<![A-Za-z])%s(?:\s*(?:&|/|,|，|、)\s*%s)*' % (_POS_WORD, _POS_WORD))
_PHONETIC = re.compile(r'/[^/\s]+/')
# Marks that only ever join tags are expected to disappear; the comma family is
# ordinary punctuation inside a definition and is reported, not touched.
TAG_MARKS = ('&', '/')
PUNCTUATION_MARKS = (',', '，', '、')


def check_policy(policy, allow_supplied=False):
    """Return ``policy`` when it is one a request may carry.

    A new production request asks for ``v2`` or ``legacy``.  ``supplied`` is only
    meaningful on a document that brings its own reading text, so it is refused
    unless the caller says it has that text (a stored request is replayed through
    here with the value it was normalised with).
    """
    allowed = ALL_POLICIES if allow_supplied else POLICIES
    if policy not in allowed:
        raise ValueError('spoken_policy must be one of %s, not %r'
                         % ('/'.join(allowed), policy))
    return policy


def spoken_from_meaning(meaning, policy=DEFAULT_POLICY):
    """The text to read aloud, derived from one display meaning.

    Pure: no Qt, no environment, no network, no word list.  ``meaning`` must be
    the display text (``d_f`` from the word list); the word and the phonetic are
    never passed through here.
    """
    check_policy(policy)
    if not isinstance(meaning, str):
        raise TypeError('meaning must be a string, got %r' % (type(meaning).__name__,))
    if policy == POLICY_LEGACY:
        # Each tag alone, spaces kept: exactly what the delivered batches sound like.
        return _TAG.sub('', meaning).strip()
    text = _TAG_RUN.sub(' ', meaning)
    text = _PHONETIC.sub(' ', text)
    return ' '.join(text.split())


def leftover_connectors(text):
    """Marks still present in a cleaned line, for the change report.

    Not a validation rule: ``&``/``/`` surviving means the rule met something it
    cannot judge, while a comma inside a real definition ("偏爱，喜爱") is correct
    and is listed only so a human can抽查 it.
    """
    if not isinstance(text, str):
        raise TypeError('text must be a string, got %r' % (type(text).__name__,))
    return tuple(mark for mark in TAG_MARKS + PUNCTUATION_MARKS if mark in text)
