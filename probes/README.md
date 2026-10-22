# Forked probes

`world-mechanics.example.json` contains starter fixture items for offline
development. It is not the frozen 60-item pilot bank or a calibrated exam, and
its answers must not be mounted into a live harness workspace. Scientific items
need held-out, fact, and own-situation strata with their source URLs and frozen
answer keys in the private researcher data directory.

`analysis.probes.score_probe_cells` accepts recorded results from fresh contexts
in `pre_off`, `pre_on`, `post_off`, `post_on`, and `post_mem` cells. It excludes a
probe item from every cell when any of its source URLs was fetched in the run,
computes gains only on matched item ids, and reports each stratum separately.
It schedules no calls and claims no R1 result.

`analysis.probes.expectation_calibration` keeps unresolved, expired, void, held,
and missed outcomes distinct. It publishes a Brier score only with at least 30
resolved expectations. Callers must stratify resolver and venue before scoring.
Late final-grace resolutions remain in resolution coverage but are excluded
from Brier/ECE when their actual outcome marks `headline_eligible:false` or
`late_final:true`. Redirected batch results and browser destinations enter the
whole-item exposure exclusion set, including URLs carried by async events.
