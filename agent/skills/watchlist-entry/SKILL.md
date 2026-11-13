---
name: watchlist-entry
description: Draft a cited watchlist entry for a BDC held by the active mandate and route it through Priya's approval. Use when a filing creates a credit item that needs monitoring.
---
# Steps
1. Confirm the BDC is in the active mandate's positions with `mandate_profile`.
2. Read `bdc_signals` and use only signals supported by the filing and panel rows.
3. Write one concise reason that states the rule, accession number, current row and prior row.
4. Call `watchlist_write` without a token. This creates a request; it must not write directly.
5. Ask Priya to approve the exact request. Call `watchlist_write` again only with the returned token.
