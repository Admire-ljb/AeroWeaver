# All-Task LLM and Component Pilot

## Material Passport
- Origin: academic-research-suite / experiment-agent.
- Status: protocol frozen before new outcomes; single-episode exploratory comparison.
- Authorization: all tasks and component ablations, following the one-run-per-condition pilot.

## Run Matrix
- Command: `python experiments/end_to_end_v1/launch.py --all-tasks-ablations --seed 66001`.
- 9 tasks x 6 conditions x 1 cold-start episode = 54 episodes, no extra paid warmup.
- Tasks: coverage; pursuit; guided navigation; private communication; circular formation; line formation; world communication; collection-delivery; goal concealment.
- Same initial seed (66001) for all conditions within a task; private goal, symbol and key are included in the archived initial-state hash, never exposed to unauthorized local observations.
- Fixed roles: evader in pursuit; foragers in world communication; eavesdropper in private communication; adversary in concealment. Remaining participants use the evaluated method.

| Condition | Skill catalog | Decision organization | Coordination reports | Reward correction |
| --- | --- | --- | --- | --- |
| Centralized API | All 22 | Joint plan over permitted team observations | On | None |
| HMAS-2 adapted | All 22 | Joint plan, local feedback, central revision | On | None |
| AeroWeaver | Shared mission activation | Body-local selectors | On | beta=0.8 |
| AeroWeaver w/o activation | All 22 | Body-local selectors | On | beta=0.8 |
| AeroWeaver w/o coordination reports | Shared mission activation | Body-local selectors | Off | beta=0.8 |
| AeroWeaver w/o reward correction | Shared mission activation | Body-local selectors | On | beta=0 |

The no-coordination condition suppresses controlled-agent state/intent and target reports at message delivery, not physical observations. It preserves the goal messages and ciphertext required by navigation and private communication. This tests the contribution of coordination reports; it is not a pure centralized-versus-distributed architecture ablation.
Centralized API and HMAS-2 remain external-structure comparisons and differ from AeroWeaver in multiple mechanisms. Their performance differences cannot isolate decentralization alone.

## Matched Settings
- DeepSeek V4 Flash, body-scoped-api-v2 prompts, unchanged executable skills, motion controllers and reward mapping.
- Activate once per task with exactly 8 distinct skills; reuse that result for the full, no-coordination and no-RL conditions. Preserve the common hold action. Do not repair failed activations or substitute an oracle catalog.
- Use empty, separate SQLite stores for every task/condition. Record all participant transitions; retrieve only controlled, same-task/same-role experience belonging to the current condition. No cross-episode evaluation is claimed.
- gamma=0.95, beta=0.8 except beta=0 control; retrieval at most 32 records. Keep retrieval enabled under beta=0 so that only the correction is removed.
- Maximum 24 decision rounds of 0.5 seconds, 0.05-second physics steps. LLM waiting does not advance the world.
- All-task capability adapter excludes motion skills for stationary participants, as already required by the catalog. It does not insert task-solving actions or fix the earlier repeated-landmark behavior.
- Opponent policies, sensing, bounds, episode endings and MPE2 reward-function adaptations are identical across conditions.
- Private communication ends after three protocol rounds; this is not automatically success. Report receiver/eavesdropper correctness separately. World communication and concealment have no binary success criterion.

## Budget and Failure Handling
- 5,000 model requests maximum; stop starting new requests after 12,000,000 reported tokens; 40-second request, 900-second episode and 3,600-second batch limits.
- Three episode workers. No automatic model request or episode retries.
- Test the existing runner plus every task/condition using synthetic, no-network responses before paid execution.
- Keep provider failures, invalid final actions, invalid local reviews, fallbacks and incomplete runs in the output. Do not exclude conditions based on return.
- No MAPPO/MADDPG training, production service restart, production memory import or paper-table replacement.

## Outputs and Analysis
- Archive raw requests/responses, prompts, activation artifacts, source hashes, states, executed actions, peer messages, per-step rewards, discounted returns and metrics.
- Report actual batch API calls/tokens. Since activation is shared for matching, also report per-condition accounted cost charging each activation-using condition one full activation. Do not mistake attributed cost for actual billed batch usage.
- Publish a nine-task, six-condition return matrix and full-minus-ablation differences, with task outcomes and inference cost alongside.
- Check all 54 planned cells, matching initial states, matching activation subsets, beta=0 behavior, blocked reports, role/future-data isolation and return consistency.
- Do not average raw reward values across tasks or claim significance from one paired seed. Reward-update effects here are within-episode and cannot establish continual cross-mission improvement.
- The earlier coverage problem remains part of the tested method. Parameter-aware reward updates or task-specific prompt changes require a separately identified later version.

## 429 Continuation Amendment
- Parent batch 20260910-040045 received HTTP 429 after 14 finished episodes, 2 interrupted episodes and before 38 episodes started. The provider reported a request concurrency limit of 5.
- Add a shared semaphore limiting the entire Client to 3 in-flight requests, including nested local-agent and HMAS feedback calls. This is scheduling only; prompts, model settings and controllers stay unchanged.
- `--parent-output` carries forward all parent artifacts in a new directory and schedules only entries with zero model calls and zero transitions. It does not automatically retry the 2 interrupted episodes or rerun the 14 completed ones.
- Reuse the parent's exact task activations and seed. The combined request/token budgets include all parent usage. The parent directory remains unchanged.
- Latency from parent and continuation has different request scheduling; retain provenance and do not make a pooled latency-speedup claim.

## User-Authorized Model Transition (2026-09-10)
- The user authorized using V4.1 Flash at the supplied lower tariff and combining existing results without repeating completed conditions. The live official `/models` endpoint lists `deepseek-flash`; the experiment sends this identifier without changing production settings.
- User-supplied CNY prices per million tokens, effective 2026-09-10 12:00 Beijing time: cache hit 0.02/0.04, cache miss 1/2, output 4/8 for off-peak/peak. The public pricing page still showed the older V4 schedule at verification time; preserve the source distinction.
- Continue from 20260910-040828 with `--model deepseek-flash --allow-mixed-models --restart-incomplete`. Retain 14 finished episodes; schedule 37 unstarted cells and one fresh attempt for each of 3 provider-interrupted/blocked cells. No performance-based repeats.
- Restarted episodes have distinct IDs and empty memory. Archive the prior episode folder and database; retain its raw calls in total usage. Do not copy zero-transition pending databases into the new active memory directory.
- Reuse the archived V4 task activations for matching. Record each rollout's requested model and the provider's response model. The combined table is a mixed-backbone exploratory pilot; it does not establish a controlled fixed-backbone effect or that the two backbones are equivalent.
- Before new task calls, run two minimal interface checks for JSON output and token log probabilities. No regenerated activations, warmup episodes or retries on a new failure.
