# Drift-trigger calibration on drift-free windows

Run 2026-11-12T11:14:27+00:00 from commit `74571721637f611a0317b06924889735853750ac` (`python -m scripts.drift_calibration`), 1773 s. Frozen rules (`configs/drift.toml` sha256 `bd47526f8ece…`), min_rows 1,000. Windows fixed in code before the run (`plan_windows`).

**False fires: 5 of 11 windows (45%).**

| Window (days, end excl.) | Current rows | Reference rows | Drifted share | PSI | Delayed PR-AUC / comparator | Drifted top-gain | Fired | Reasons |
|---|---|---|---|---|---|---|---|---|
| 61-68 | 23,476 | 89,399 | 0.107 | 0.0236 | 0.3193 / 0.2232 | - | no | - |
| 68-75 | 20,638 | 89,408 | 0.071 | 0.0233 | 0.4064 / 0.2994 | - | no | - |
| 75-82 | 20,437 | 89,336 | 0.143 | 0.0058 | 0.3556 / 0.3481 | - | no | - |
| 82-89 | 21,059 | 89,439 | 0.036 | 0.0045 | 0.3760 / 0.3612 | - | no | - |
| 89-96 | 28,004 | 90,944 | 0.143 | 0.0058 | 0.4289 / 0.3249 | - | no | - |
| 96-103 | 22,030 | 95,945 | 0.107 | 0.0081 | 0.4820 / 0.3454 | addr2, R_emaildomain | **yes** | top-feature drift: addr2, R_emaildomain drifted (among the champion's top 5 features by gain) |
| 103-110 | 20,718 | 96,956 | 0.107 | 0.0121 | 0.3864 / 0.3512 | addr2 | **yes** | top-feature drift: addr2 drifted (among the champion's top 5 features by gain) |
| 110-117 | 20,980 | 96,878 | 0.143 | 0.0149 | 0.3297 / 0.4365 | card3 | **yes** | top-feature drift: card3 drifted (among the champion's top 5 features by gain); performance: delayed-label PR-AUC 0.3297 < champion validation 0.4365 - 0.05 |
| 117-124 | 22,376 | 97,302 | 0.214 | 0.0183 | 0.2399 / 0.3759 | card3 | **yes** | top-feature drift: card3 drifted (among the champion's top 5 features by gain); performance: delayed-label PR-AUC 0.2399 < champion validation 0.3759 - 0.05 |
| 124-131 | 21,717 | 92,746 | 0.036 | 0.0058 | 0.2304 / 0.2851 | - | **yes** | performance: delayed-label PR-AUC 0.2304 < champion validation 0.2851 - 0.05 |
| 131-138 | 18,589 | 91,457 | 0.036 | 0.0046 | 0.3451 / 0.2238 | - | no | - |

Fires per rule: performance 3, top-feature drift 4

Top-gain features that drifted (windows): addr2 2, card3 2, R_emaildomain 1
