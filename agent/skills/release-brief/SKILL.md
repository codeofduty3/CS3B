---
name: release-brief
description: Build a release-day brief for the active mandate from held BDCs only, ordered by exposure and supported by deterministic signal citations. Use after an SEC BDC release or when asked for a release brief.
---
# Steps
1. Call `mandate_profile` and `bdc_signals`.
2. Keep only BDCs in the active mandate's positions.
3. Run `scripts/signal_digest.py` on the filtered signals; do not retype numbers.
4. Open with the client's own exposure, then report material changes in points.
5. Include the accession number and panel row for every number. State paired-holding coverage.
6. Keep another client's positions, aliases, contacts and account codes out of the brief.
