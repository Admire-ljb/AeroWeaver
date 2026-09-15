# LLM interface calibration v2

Status: development calibration, not a performance claim or a main-experiment result.
Parent batch: 20260910-014237. Old raw requests, failures, rewards, and memory remain unchanged.

## Changes frozen before calls

- Preserve direct `skill` and `parameters` outputs for Centralized API and HMAS-2. Do not replace either baseline with AeroWeaver activation or reward correction.
- Separate registered capabilities from per-body executable actions in the prompt. Keep exactly the same executor affordances; do not enlarge or shrink the true action set.
- Include catalog preconditions and explicit body/target/message-ID scoping. Global knowledge alone does not create an executable body-local target.
- Supply the public controlled-team role assignments and common communication/coordination guidance to all three LLM methods. Do not expose hidden positions or opponent internal state.
- Increase central output ceiling from 350 to 500 tokens, and local-review ceiling from 100 to 384. Continue to log actual token consumption and latency, including failures.
- An unavailable local review is logged and forwarded as unavailable, not converted into three stopped agents. The central revision still decides actions; no deterministic heuristic repairs its output.
- Keep DeepSeek v4 flash, reward functions, Mock physics, activation k=8, gamma=0.95, beta=0.8, and each method's orchestration unchanged.

## Stages

1. No-network unit and execution-bookkeeping checks, including reviewer-failure isolation and per-body action scope.
2. Replay 14 fixed saved-state cases: the first three failing adaptation steps for each affected task/method pair, plus first-step coverage controls. These are deliberately selected regressions, not a representative accuracy estimate. Gate: zero malformed/invalid final actions, zero failed reviews, and zero provider errors. Maximum 96 calls and 300,000 reported tokens; no retries.
3. Only after the replay gate passes, run 18 new development episodes: three methods, three tasks, one adaptation seed 64011 and one probe seed 64012. At most 24 rounds per episode. Independent experiment memory; no production-store writes. Maximum 1,600 calls, 3,000,000 observed tokens, 40-second request timeout, 15-minute episode timeout and one-hour batch budget.

Do not tune beta, choose tasks, replace failed outcomes, or change the scoring protocol based on the resulting returns. New-seed episode outcomes are not a paired before/after performance comparison with the old seed batch.

## External MARL result reuse

Published MAPPO/MADDPG numbers may be cited as literature context with their original environment, metric, and source. They cannot be inserted as directly comparable rows alongside this custom Mock evaluation. Sharing MPE task names or reward equations does not match physics, state mapping, observation/action interfaces, reward sampling, opponents, or horizon.

Reuse official implementations or compatible checkpoints, then evaluate in a shared protocol. A checkpoint is not guaranteed to transfer to this Mock interface without adaptation or training. If choosing a standard native-MPE benchmark instead, run AeroWeaver under that same benchmark and document the remaining differences; do not silently replace the user-selected Mock testbed.

- MAPPO official implementation: https://github.com/marlbenchmark/on-policy
- MADDPG official implementation: https://github.com/openai/maddpg
