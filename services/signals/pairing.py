"""Pair current and prior schedules without relying on volatile identifiers.

SEC identifiers often embed all-in rates, spreads or maturities. Those values
can change between schedules even when the economic holding is unchanged.
Pair on filing + borrower + instrument type, then keep matches one-to-one.
"""
import re
import unicodedata

import pandas as pd


def _text(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z]+", " ", value).strip()


def _borrower_and_instrument(identifier):
    text = str(identifier or "").strip()
    if "|" in text:
        parts = [part.strip() for part in text.split("|")]
        if len(parts) >= 2:
            return parts[1], parts[0]
    borrower, separator, instrument = text.partition(",")
    if separator:
        return borrower.strip(), instrument.strip()
    return text, ""


def _canonical_instrument(label):
    """Collapse equivalent filing labels while preserving tranche distinctions."""
    label = _text(label)
    if "common stock" in label:
        return "common stock"
    if "preferred equity" in label or "preferred stock" in label:
        return "preferred equity"
    lien = "second lien" if "second lien" in label else "first lien"
    if "delayed draw" in label:
        return f"{lien} delayed draw"
    if "revolv" in label:
        return f"{lien} revolver"
    if "term loan" in label:
        return f"{lien} term loan"
    return f"{lien} loan"


def _keys(panel):
    frame = panel.copy()
    parsed = frame["identifier"].map(_borrower_and_instrument)
    frame["_borrower_key"] = parsed.map(lambda value: _text(value[0]))
    frame["_instrument_key"] = parsed.map(lambda value: _canonical_instrument(value[1]))
    frame["_match_rank"] = frame.groupby(
        ["adsh", "_borrower_key", "_instrument_key"], sort=False
    ).cumcount()
    return frame


def pair(panel):
    """Return one-to-one current/prior pairs from the same filing.

    The result has current-period columns unchanged and prior-period columns
    suffixed with ``_prior``, matching ``creditwatch.signals.pair_exact``.
    """
    current = _keys(panel[panel["period_role"] == "current"])
    prior = _keys(panel[panel["period_role"] == "prior"])
    keys = ["adsh", "_borrower_key", "_instrument_key", "_match_rank"]
    paired = current.merge(prior, on=keys, how="inner", suffixes=("", "_prior"))
    return paired.drop(columns=["_borrower_key", "_instrument_key", "_match_rank"])
