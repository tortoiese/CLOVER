# Paired exploratory analysis

Declared blocks: 4; arms: 20; full observation arms: 20.

Parent arm slots not yet identifiable (all policies): 0.

Differences are candidate minus control. CSV blank values mean undefined/withheld, not zero. Means use only complete comparable pairs; all failed, missing, partial and undefined arms remain in analysis.json and planned denominators. Any incomplete planned pair withholds inference.

Per-layout estimates use independent demand-seed pairs. Across-layout estimates weight each configured layout mean equally. Repeated factories, reused seeds across layouts, deterministic inputs, insufficient replication and unreviewed sampling withhold intervals. Layout/hardware labels are declarations, not proof of independence or representativeness.

The t interval is a pointwise exploratory interval. Paired percentile bootstrap is a fixed-method sensitivity analysis, not an alternative chosen after looking at results. Holm adjusts p-values for the whole declared family, including unavailable slots; it does not adjust these intervals. The t approximation assumes independent unit differences and a suitable distribution of their mean. The 10-pair/3-layout gates do not establish normality, adequate power or independence. Observed-start P95 does not describe censored tasks; inference is withheld when starts are censored. An absent due-date denominator or zero completions keeps the respective ratio undefined.

Batch makespan is available only for declared finite batches with every offered job completed inside the measurement window. It measures elapsed time from first release to last completion; partial batches and streams do not receive a completed-only makespan.

Recorded run wall time uses the runner's existing execution timer; it excludes preflight, final disposal and storage. It is neither complete CLI cost nor operational decision latency. Energy/completion is window consumption divided by window completions, not a matched job-cohort energy estimate. Rescue, charging, faults and unfinished counts must be reviewed separately.

E0 always withholds inference. No algorithm recommendation, equivalence or held-out validation is produced by this analysis. Analysis can read older source-frozen runs without rerunning solvers; original input/result/event digests and source/settings contexts remain recorded.

Methods: [NIST paired intervals](https://itl.nist.gov/div898/handbook/prc/section3/prc312.htm), [SciPy paired tests](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ttest_rel.html), [paired resampling](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html), [Holm adjustment](https://www.statsmodels.org/stable/generated/statsmodels.stats.multitest.multipletests.html).
