# First Closed-Loop Skill Activation Batch

## Material Passport
- Origin: academic-research-suite / experiment-agent.
- Stage: first-round development experiment, explicitly authorized by the user.
- No held-out efficacy, statistical significance, RL improvement, or native MPE rollout claim.

## Question and Fixed Design
Does mission-level skill activation change subsequent body-local execution and
inference cost relative to the full available catalog or lexical activation?

- Coverage, pursuit, and world communication; seeds 101, 102, 103, 104, 105.
- Full catalog, BM25 top-6, and LLM mission-level top-6: 45 episodes.
- The same 22 existing Mock skill documents are used by all conditions.
- All conditions retain the common own-body hold action. No template repair of
  missing task skills and no per-role replacement of the activated catalog.
- Documented private-information and executor preconditions remain identical.
- Role assignments, scene generation, body permissions, observations, directed
  communication, Mock physics, movement trackers, and rewards are unchanged.
- Pursuit evaders and world-communication foragers use the same existing local
  engineering policy across conditions. This fixes opponent policy, not its
  trajectory. Their records remain separate from LLM-controlled team records.
- Natural-language parsing is outside this activation ablation. The same fixed
  mission text and structured scenario assignment are used in every condition.
- Official deepseek-v4-flash, thinking disabled, activation temperature 0,
  decision temperature 0.2. Actual response model IDs and usage are archived.
- 24 decision intervals, 0.5 simulated seconds per interval, 10 existing Mock
  physics ticks per interval. No simulation advance during model inference.
- Each local agent receives only its own observation. A barrier synchronizes
  simulation scheduling, not semantic planning or global information access.
- Three independent episode workers, at most three LLM-controlled agents per
  episode. One API attempt per call; 40-second request timeout and 30-minute
  episode budget. No failed-call retries, heuristic policy replacement, or
  discarded failed episodes. An invalid decision executes a logged own-body
  hold, whose reward is not attributed to an unexecuted requested skill.

## Outcomes and Scope
Report task success for coverage and pursuit under existing Mock criteria.
World communication has no binary completion rule; report separate role returns,
contacts, food visits, and cover occupancy without manufacturing a success label.
Report actual total provider tokens including activation, per-decision tokens,
request latency, decision errors, execution errors, peer messages and raw returns.
Early success shortens an episode, so per-decision cost is reported separately
from episode totals. Latency is not interpreted as a real-time robustness test.
Task-skill coverage against existing engineering role templates is diagnostic,
not independently annotated ground truth. Five paired seeds are exploratory.

## Experience and Verification
Every executed interval is recorded in the existing SQLite memory under a unique
episode ID, with condition, seed, role, actual skill invocation, local state,
next state, reward, gamma=0.95 and terminal/time-limit metadata. Rewards are the
pinned MPE2 1.1.0 scenario reward functions on mapped Mock state, not native MPE
dynamics. Online updates and experience reuse remain off. Records are staged for
future experience-library admission rather than automatically reused.

An isolated three-scenario one-round preflight uses seed 9001 and a separate
database. The formal development batch starts only after tests and this preflight
pass without decision/execution errors. Independent recomputation checks every
discounted return, export-to-SQL equality, and paired initial-state hashes.
Source hashes, calls, plans, failures and all completed/partial episodes remain
archived. Existing pursuit-tracker heuristics are shared by every group; this
experiment does not isolate learned flight control from the fixed executor.
