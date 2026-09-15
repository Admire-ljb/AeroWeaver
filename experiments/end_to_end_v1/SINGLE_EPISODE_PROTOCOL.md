# Single-Episode LLM Comparison

## Material Passport
- Origin: academic-research-suite / experiment-agent, run mode.
- Status: protocol frozen before outcome inspection.
- Scope: nine descriptive, cold-start LLM episodes; no MARL training or evaluation.

## Frozen Run
- Command: `python experiments/end_to_end_v1/launch.py --single-episode --seed 65001`.
- Host: 192.0.2.10; isolated staging directory, no production restart or memory writes.
- Methods: Centralized API, HMAS-2 adapted, AeroWeaver pilot.
- Tasks: coverage, pursuit, world communication. One episode per task and method.
- Initial-state seed: 65001, paired across methods within each task.
- Start every task/method with an empty SQLite experience store. All executed transitions are retained; only AeroWeaver consumes its own controlled-agent experience during this episode.
- No extra warmup, saved-state calibration, retries, or selective reruns.
- Keep the body-scoped-api-v2 prompts, DeepSeek V4 Flash backbone, registered catalog, fixed-step Mock dynamics and MPE2 reward mapping unchanged.
- Maximum 24 decision rounds of 0.5 simulated seconds, physics step 0.05 seconds. This short-horizon run is not the full main experiment.
- Activation k=8; gamma=0.95; beta=0.8; retrieval at most 32 same-role records.
- Budgets: 800 model requests; stop starting new requests after 1,600,000 reported tokens; 40 seconds per request, 900 seconds per episode, 3600 seconds per batch. In-flight calls may cross the observed-token stopping threshold.
- No-network preflight tests run before paid model calls.

## Outputs and Interpretation
- Retain every episode's initial state, active skills, raw requests/responses, decisions, peer messages, per-agent rewards and discounted returns, errors and fallback actions.
- Report all nine outcomes with team mean episode return, completion or horizon status, inference tokens and latency.
- Audit paired initial states, reward-to-return consistency, role-scoped retrieval and exclusion of future transitions.
- This run tests within-episode reward correction, not cross-episode adaptation. A single run does not estimate seed variability or statistical significance.
- MAPPO and MADDPG remain outside this LLM-only batch. Do not import their results from a different environment.
