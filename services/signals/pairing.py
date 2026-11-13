"""Pair each current holding with the same holding in the prior fiscal year-end schedule of
the SAME filing, when the identifier text has changed between the two schedules.

Filers write the terms that move into the identifier: the all-in rate (it moves with SOFR),
the spread, the maturity (it moves on an extension), sometimes "Cash + PIK". Exact matching
on the identifier therefore misses exactly the holdings whose terms changed, which are the
ones the signals are about. We pair in three passes, all within one filing (adsh):

1. exact:      identical identifier (the kit's baseline);
2. normalized: identical identifier once the volatile terms (rates, spreads, dates, rate
               vocabulary) are removed, and only where that key is unique on both sides;
3. fuzzy:      what is left, by token overlap of the normalized identifiers (Jaccard >= 0.8),
               best pairs first, one-to-one, and never across tranches: the words that tell
               a term loan from its delayed-draw or revolver sibling, or tranche 1 from 2,
               must agree.

A holding with no counterpart (new loan, exit) stays unpaired: no pair is better than a
wrong pair, because a wrong pair manufactures a markdown, a PIK step-up or an extension.

pair(panel) returns the same frame as creditwatch.signals.pair_exact (current columns, prior
columns suffixed "_prior"), plus `pair_method`, so signals.find_signals(panel, m, pair=pair)
runs the same rules.
"""
import re
from difflib import SequenceMatcher

import pandas as pd

MIN_JACCARD = 0.8

_DATE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{1,2}/\d{4}\b|\b\d{4}-\d{2}-\d{2}\b"
                   r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}\b")
_RATE = re.compile(r"\b[a-z]{1,6}(?:\s*\([a-z]\))?\s*\+\s*\d+(?:\.\d+)?\s*%?"   # S + 5.25%, S+450, SOFR(Q) + 6.00
                   r"|\d+(?:\.\d+)?\s*%"                                         # 9.30%
                   r"|\b\d+\.\d+\b")                                             # 5.60
_VOCAB = {"interest", "rate", "rates", "coupon", "total", "spread", "reference", "ref", "and",
          "floor", "cash", "pik", "plus", "includes", "maturity", "mat", "date", "due",
          "sofr", "libor", "euribor", "sonia", "bbsy", "prime", "fixed", "all", "in"}
# Words that separate one tranche of a borrower from another: they must agree in a fuzzy pair.
_TRANCHE = {"delayed", "draw", "ddtl", "revolver", "revolving", "rcf", "drawn", "undrawn",
            "unfunded", "second", "third", "last", "incremental", "first", "one", "two", "three",
            "1", "2", "3", "4", "5", "6", "7", "8", "9", "common", "preferred", "warrant",
            "warrants", "equity", "stock", "units", "notes", "note", "subordinated", "mezzanine"}


def normalize(identifier):
    """The identifier without the terms that move between schedules."""
    s = str(identifier).lower()
    s = _DATE.sub(" ", s)
    s = _RATE.sub(" ", s)
    toks = re.sub(r"[^0-9a-z]+", " ", s).split()
    toks = [t for t in toks if t not in _VOCAB
            and not (len(t) == 1 and t.isalpha())          # S, L, Q, M rate letters
            and not (t.isdigit() and len(t) > 1)]          # basis points, leftover years
    return " ".join(toks)


def pair(panel):
    cols = list(panel.columns)
    cur = panel[panel["period_role"] == "current"].reset_index(drop=True)
    pri = panel[panel["period_role"] == "prior"].reset_index(drop=True)
    cn = cur["identifier"].map(normalize)
    pn = pri["identifier"].map(normalize)
    used_c, used_p, links = set(), set(), []        # links: (current index, prior index, method)

    # 1 and 2: identical key in the same filing, unique on both sides
    for method, ck, pk in (("exact", cur["identifier"], pri["identifier"]), ("normalized", cn, pn)):
        c = pd.DataFrame({"adsh": cur["adsh"], "k": ck, "i": cur.index})[~cur.index.isin(used_c)]
        p = pd.DataFrame({"adsh": pri["adsh"], "k": pk, "j": pri.index})[~pri.index.isin(used_p)]
        c = c[~c.duplicated(["adsh", "k"], keep=False)]
        p = p[~p.duplicated(["adsh", "k"], keep=False)]
        for i, j in c.merge(p, on=["adsh", "k"])[["i", "j"]].itertuples(index=False):
            used_c.add(i); used_p.add(j); links.append((i, j, method))

    # 3: fuzzy, within each filing, on what is left; best pairs first, one-to-one
    left_p = [j for j in pri.index if j not in used_p]
    by_filing = {}
    for j in left_p:
        by_filing.setdefault(pri.at[j, "adsh"], []).append((j, set(pn[j].split())))
    cand = []
    for i in (i for i in cur.index if i not in used_c):
        ct = set(cn[i].split())
        for j, pt in by_filing.get(cur.at[i, "adsh"], []):
            if not ct or not pt or (ct & _TRANCHE) != (pt & _TRANCHE):
                continue                                 # never across tranches
            jac = len(ct & pt) / len(ct | pt)
            if jac >= MIN_JACCARD:
                raw = SequenceMatcher(None, str(cur.at[i, "identifier"]).lower(),
                                      str(pri.at[j, "identifier"]).lower()).ratio()
                cand.append((-jac, -raw, i, j))
    for _, _, i, j in sorted(cand):                      # deterministic: score, then index
        if i not in used_c and j not in used_p:
            used_c.add(i); used_p.add(j); links.append((i, j, "fuzzy"))

    ci = [i for i, _, _ in links]
    pj = [j for _, j, _ in links]
    out = pd.concat([cur.loc[ci, cols].reset_index(drop=True),
                     pri.loc[pj, cols].reset_index(drop=True).add_suffix("_prior")], axis=1)
    out["pair_method"] = [m for _, _, m in links]
    return out
