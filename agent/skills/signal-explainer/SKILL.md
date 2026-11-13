---
name: signal-explainer
description: Explain one deterministic credit signal from the panel with its filing, current row, prior row and threshold. Use when asked why a BDC was flagged or what a signal means.
---
# Steps
1. Call `bdc_signals`; use the signal's exact `adsh`, `row` and `prior_row`.
2. State the before and after values and the rule threshold from `params.yaml`.
3. For markdowns, express the price move in points, not percent.
4. Say when the pair is missing or when the move is below the threshold.
5. Do not claim an event that the same-filing pair and deterministic rule do not support.
