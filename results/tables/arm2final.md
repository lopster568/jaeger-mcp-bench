# Arm 2 comparison: tiered vs flat, claude vs gemini

Run source: arm2final


## Pass rate (correct / scorable trials) by (model, format, task)

| task | claude/tiered | claude/flat | gemini/tiered | gemini/flat |
|---|---|---|---|---|
| 21_error_root_cause | 1.0 | 0.667 | 1.0 | 1.0 |
| 22_critical_path | 1.0 | 0.0 | 1.0 | 0.667 |
| 23_trace_shape | 0.333 | 0.667 | 1.0 | 0.0 |
| 24_attribute_hunt | 1.0 | 0.0 | 0.333 | 0.0 |
| 25_dependency | 1.0 | 0.667 | 1.0 | 1.0 |
| 26_compare_traces | 1.0 | 0.0 | 0.333 | 0.0 |

## Unscorable trials by (model, format, task)

| task | claude/tiered | claude/flat | gemini/tiered | gemini/flat |
|---|---|---|---|---|
| 21_error_root_cause | 0 | 0 | 0 | 0 |
| 22_critical_path | 0 | 0 | 0 | 0 |
| 23_trace_shape | 0 | 0 | 0 | 0 |
| 24_attribute_hunt | 0 | 0 | 0 | 0 |
| 25_dependency | 0 | 0 | 0 | 0 |
| 26_compare_traces | 0 | 0 | 0 | 0 |

## Format-specific input tokens (mean per trial; H1 cost signal)

| task | claude/tiered | claude/flat | gemini/tiered | gemini/flat |
|---|---|---|---|---|
| 21_error_root_cause | 15728.3 | 26738.3 | 13214 | 580745.3 |
| 22_critical_path | 16269.3 | 23708.3 | 13292 | 106399.7 |
| 23_trace_shape | 24310 | 10219.7 | 6766 | 454937 |
| 24_attribute_hunt | 20478.3 | 19067.3 | 4817.3 | 3750.7 |
| 25_dependency | 9776 | 8286.7 | 11032.7 | 255561.7 |
| 26_compare_traces | 26956 | 13871.7 | 10006.3 | 332623.7 |

## Output tokens (mean per trial)

| task | claude/tiered | claude/flat | gemini/tiered | gemini/flat |
|---|---|---|---|---|
| 21_error_root_cause | 4132.7 | 7018.3 | 375.7 | 351.3 |
| 22_critical_path | 4121.7 | 7744.3 | 101.3 | 28 |
| 23_trace_shape | 7599 | 4086.7 | 110 | 83.7 |
| 24_attribute_hunt | 3000.3 | 7520.7 | 109.3 | 34.3 |
| 25_dependency | 1045.3 | 3502.3 | 304.3 | 38.3 |
| 26_compare_traces | 6706.7 | 8659.3 | 275.3 | 15 |

## Tool calls (mean per trial)

| task | claude/tiered | claude/flat | gemini/tiered | gemini/flat |
|---|---|---|---|---|
| 21_error_root_cause | 6.67 | 9.33 | 11.67 | 6.33 |
| 22_critical_path | 3.33 | 8.67 | 3 | 0.67 |
| 23_trace_shape | 7.33 | 6 | 1.67 | 1 |
| 24_attribute_hunt | 4.67 | 9 | 2 | 1 |
| 25_dependency | 2.67 | 5 | 7.33 | 1 |
| 26_compare_traces | 5.33 | 8.67 | 3 | 0.33 |

## Duration ms (mean per trial)

| task | claude/tiered | claude/flat | gemini/tiered | gemini/flat |
|---|---|---|---|---|
| 21_error_root_cause | 57495 | 119789 | 73105 | 132704 |
| 22_critical_path | 67309 | 149691 | 42197 | 500411 |
| 23_trace_shape | 121555 | 271849 | 32292 | 154363 |
| 24_attribute_hunt | 55005 | 293306 | 38609 | 45211 |
| 25_dependency | 21064 | 309469 | 69309 | 56970 |
| 26_compare_traces | 78162 | 176353 | 81750 | 252413 |

## Registered two-proportion z-tests on correctness (Bonferroni alpha = 0.05/4 = 0.0125)

| comparison | a_correct/n | b_correct/n | z | p (2-sided) | sig (Bonferroni) |
|---|---|---|---|---|---|
| claude tiered vs flat | 16/18 | 6/18 | 3.42 | 0.0006 | yes |
| gemini tiered vs flat | 14/18 | 8/18 | 2.05 | 0.0402 | no |
| tiered claude vs gemini | 16/18 | 14/18 | 0.89 | 0.3711 | no |
| flat claude vs gemini | 6/18 | 8/18 | -0.68 | 0.4941 | no |

Note: these are the four PRE-REGISTERED comparisons (docs/arm2-design.md, Statistics section) - not a search over all possible pairs. Any other comparison surfaced elsewhere in the writeup is exploratory and not tested here. Unscorable trials are excluded from every correct/n figure above.

