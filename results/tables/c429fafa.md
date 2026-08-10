| model | format | task_id | n_trials | pass_pow_n | pass_at_n | correct | incorrect | non_answer | n_unscorable | n_budget_exhausted | n_timeout | mean_cache_creation | mean_output_tokens | mean_tool_calls | mean_duration_ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude | flat | 21_error_root_cause | 3 | 0.667 | 1 | 2 | 1 | 0 | 0 | 1 | 0 | 26738.3 | 7018.3 | 9.33 | 119789 |
| claude | flat | 22_critical_path | 3 | 0.0 | 0 | 0 | 3 | 0 | 0 | 3 | 0 | 23708.3 | 7744.3 | 8.67 | 149691 |
| claude | flat | 23_trace_shape | 3 | 0.667 | 1 | 2 | 1 | 0 | 0 | 0 | 1 | 10219.7 | 4086.7 | 6 | 271849 |
| claude | flat | 24_attribute_hunt | 3 | 0.0 | 0 | 0 | 3 | 0 | 0 | 2 | 0 | 19067.3 | 7520.7 | 9 | 293306 |
| claude | flat | 25_dependency | 3 | 0.667 | 1 | 2 | 1 | 0 | 0 | 0 | 1 | 8286.7 | 3502.3 | 5 | 309469 |
| claude | flat | 26_compare_traces | 3 | 0.0 | 0 | 0 | 3 | 0 | 0 | 3 | 0 | 13871.7 | 8659.3 | 8.67 | 176353 |
| claude | tiered | 21_error_root_cause | 3 | 1.0 | 1 | 3 | 0 | 0 | 0 | 0 | 0 | 15728.3 | 4132.7 | 6.67 | 57495 |
| claude | tiered | 22_critical_path | 3 | 1.0 | 1 | 3 | 0 | 0 | 0 | 0 | 0 | 16269.3 | 4121.7 | 3.33 | 67309 |
| claude | tiered | 23_trace_shape | 3 | 0.333 | 1 | 1 | 2 | 0 | 0 | 2 | 0 | 24310 | 7599 | 7.33 | 121555 |
| claude | tiered | 24_attribute_hunt | 3 | 1.0 | 1 | 3 | 0 | 0 | 0 | 0 | 0 | 20478.3 | 3000.3 | 4.67 | 55005 |
| claude | tiered | 25_dependency | 3 | 1.0 | 1 | 3 | 0 | 0 | 0 | 0 | 0 | 9776 | 1045.3 | 2.67 | 21064 |
| claude | tiered | 26_compare_traces | 3 | 1.0 | 1 | 3 | 0 | 0 | 0 | 0 | 0 | 26956 | 6706.7 | 5.33 | 78162 |
