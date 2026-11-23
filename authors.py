"""Where an author you follow is in a paper's author list, for the Middle author setting (see ranking.py).

A paper's authors come from its feed: every author on arXiv, and on Google Scholar the first few, as
"Y LeCun, L Bottou, ..." when the profile page cuts the list short (posts then have authors_cut). Names are
written in different ways ("Yann LeCun", "LeCun, Yann", "Y LeCun"), so they're compared by surname and the
first letter of the given name, ignoring case and accents.
"""

import re
import unicodedata

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def scholar_names(text: str) -> tuple[list[str], bool]:
    """The names in a Google Scholar author line, and whether it ends with "..." because it was cut short."""
    names = [n.strip() for n in text.split(",")]
    cut = bool(names) and names[-1] in ("...", "…")
    return [n for n in names if n and n not in ("...", "…")], cut


def plain(text: str) -> str:
    """Lowercase letters and digits only, without accents."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if c.isalnum() and not unicodedata.combining(c)).lower()


def name_key(name: str) -> tuple[str, str]:
    """(surname, first letter of the given name, or "" if there is none) for comparing names."""
    parts = [p for p in name.split(",") if plain(p) and plain(p) not in SUFFIXES]  # "John Smith, Jr."
    if len(parts) > 1:  # "Smith, John"
        last, first = parts[0], parts[1]
    else:  # "John Smith"
        words = [w for w in " ".join(parts).split() if plain(w) not in SUFFIXES]
        last, first = (words[-1] if words else ""), " ".join(words[:-1])
    words = [plain(w) for w in last.split() if plain(w)]
    return (words[-1] if words else ""), plain(first)[:1]


def position(names: list[str], name: str) -> int | None:
    """Where name is in names (0 for the first), or None if it isn't there. A name with the same surname
    and a different initial is someone else; a missing initial matches any."""
    surname, initial = name_key(name)
    if not surname:
        return None
    for i, other in enumerate(names):
        other_surname, other_initial = name_key(other)
        if other_surname == surname and (not initial or not other_initial or initial == other_initial):
            return i
    return None


def middle(post: dict, name: str) -> tuple[int, int, bool] | None:
    """(position from 1, number of authors, whether there are more) if name is a middle author of the post:
    neither first nor last. None if they're first or last, or it can't be told: the post has no author list,
    the name isn't in it, or the list was cut short before them. With a cut list, the last author isn't
    shown, so any shown author but the first is in the middle, and the count is at least one more."""
    names = post.get("authors") or []
    i = position(names, name)
    if i is None or i == 0:
        return None
    cut = bool(post.get("authors_cut"))
    if not cut and i == len(names) - 1:
        return None
    return i + 1, len(names) + cut, cut
