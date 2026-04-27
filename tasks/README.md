# Tasks

Each task is one YAML file with:

- `id`: unique numeric or string identifier
- `prompt`: text shown to the agent
- `predicted_winner`: `summary` | `series` | `mixed` - author's expectation, used only to verify the corpus has balanced discrimination
- `scoring`: `programmatic` | `judge`
- `ground_truth`: structured field used by `ground_truth.py` to verify the agent's answer
- `tags`: free-form labels (e.g. `temporal`, `ranking`, `threshold`)

The 6-task corpus targets ~3 tasks favoring summary, ~3 favoring series. If results don't match predictions on most tasks, the corpus is too biased and needs adjustment before the post.
