# Cross-model format comparison

Run sources: claude=b75f18cd, gemini=b75f18cd


## Pass^3 by (model, format, task)

| task | claude/summary | claude/series | gemini/summary | gemini/series |
|---|---|---|---|---|
| 01_p99_now | 1.0 | 1.0 | 1.0 | 1.0 |
| 02_spike_detection | 0.0 | 1.0 | 0.0 | 1.0 |
| 03_rank_error_rate | 0.667 | 1.0 | 0.333 | 1.0 |
| 04_correlation | 0.333 | 1.0 | 0.667 | 0.667 |
| 05_threshold | 1.0 | 1.0 | 1.0 | 1.0 |
| 06_trend | 0.333 | 1.0 | 0.667 | 1.0 |

## Format-specific input tokens (mean per trial; lower = cheaper)

| task | claude/summary | claude/series | gemini/summary | gemini/series |
|---|---|---|---|---|
| 01_p99_now | 3882 | 18566.3 | 12347 | 37084 |
| 02_spike_detection | 2871 | 4940.3 | 11444.7 | 36414.3 |
| 03_rank_error_rate | 5688 | 24802 | 13240 | 41272.3 |
| 04_correlation | 3287.3 | 5129.7 | 11168.7 | 23950.3 |
| 05_threshold | 7518.7 | 8117.3 | 12286.7 | 17660.7 |
| 06_trend | 5299 | 3945.3 | 22885.3 | 25934.7 |

## Output tokens (mean per trial)

| task | claude/summary | claude/series | gemini/summary | gemini/series |
|---|---|---|---|---|
| 01_p99_now | 358 | 589.3 | 45.3 | 45.7 |
| 02_spike_detection | 848 | 3049 | 107.7 | 55.3 |
| 03_rank_error_rate | 1812.7 | 724.7 | 179.3 | 160 |
| 04_correlation | 834 | 779 | 162.3 | 77.3 |
| 05_threshold | 585.3 | 322.3 | 25 | 25 |
| 06_trend | 1557.7 | 318.7 | 185 | 16.3 |

## Tool calls (mean per trial)

| task | claude/summary | claude/series | gemini/summary | gemini/series |
|---|---|---|---|---|
| 01_p99_now | 1 | 1 | 1 | 1 |
| 02_spike_detection | 1 | 1 | 1.33 | 1 |
| 03_rank_error_rate | 4.67 | 2 | 5.33 | 4 |
| 04_correlation | 1 | 1 | 3 | 2 |
| 05_threshold | 1 | 1 | 1 | 1 |
| 06_trend | 3 | 1 | 4.33 | 1 |

## Aggregate correct rate across all 6 tasks

- **claude/series**: correct=18/18 (1.000 mean across 6 tasks); incorrect=0; non_answer=0
- **claude/summary**: correct=10/18 (0.555 mean across 6 tasks); incorrect=1; non_answer=7
- **gemini/series**: correct=17/18 (0.945 mean across 6 tasks); incorrect=0; non_answer=1
- **gemini/summary**: correct=11/18 (0.611 mean across 6 tasks); incorrect=0; non_answer=7

## Two-proportion z-tests on correct/total

| comparison | a_correct/n | b_correct/n | z | p (2-sided) | sig α=0.05 |
|---|---|---|---|---|---|
| claude/summary vs claude/series | 10/18 | 18/18 | -3.21 | 0.001 | yes |
| gemini/summary vs gemini/series | 11/18 | 17/18 | -2.41 | 0.016 | yes |
| claude/summary vs gemini/summary | 10/18 | 11/18 | -0.34 | 0.735 | no |
| claude/series vs gemini/series | 18/18 | 17/18 | 1.01 | 0.310 | no |
