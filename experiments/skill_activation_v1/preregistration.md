# Stage A Pilot: Frozen Protocol

## Material Passport

- Origin Skill: academic-research-suite / experiment-agent
- Origin Mode: run
- Date: 2026-09-09
- Version: activation_pilot_v1
- Verification Status: UNVERIFIED before execution
- Label status: provisional, developer-authored, not independently reviewed

## Scope

Run a retrieval-only pilot on the remote machine, isolated from the web server and its world state.
There are 18 English mission instructions spanning nine scenarios and 36 mission-role queries.
This is not the planned 90-instruction held-out experiment and contains no closed-loop performance measurement.
Authoring the documents and labels in the same development workflow limits their independence.
They are frozen before the first model response, not tuned using pilot outcomes.

The common candidate set is all 22 existing Mock skill executors.
Using the same deliberately broad catalog isolates role-level activation; upstream task-catalog retrieval and mission parsing are not evaluated.
No artificial/non-executable distractors are added.
Static role templates are measured as an engineering reference and may match or outperform retrieval on these fixed scenarios.

## Conditions

- Full catalog: all 22 skills.
- Lexical Top-K: rank_bm25 BM25Okapi, default parameters, lowercased alphanumeric word tokenization.
- Semantic Top-K prototype: the configured planner LLM ranks skill documents for a mission and role, without access to labels or ROLE_SKILLS.
- Static template: the existing ROLE_SKILLS mapping, including its built-in exploration/hold helpers.

Both Top-K methods receive the same document fields and query fields.
The semantic method is an experimental implementation of document-aware activation, not a claim that the current deployed Mock route already performs it.
Primary K is 3; descriptive sensitivity results at K=1 and K=5 reuse a single top-5 ranking.
No winner or pass threshold is assumed.
The prototype system prompt explicitly ranks required capabilities ahead of generic optional helpers.

## Measurements

Required-capability coverage, complete coverage, useful-skill precision, selected skill count, rendered skill-document token proxy, activation latency, and provider-reported usage for the semantic call.
The document-token proxy uses cl100k_base for all methods; it is NOT the native DeepSeek tokenizer or measured downstream inference consumption.
Provider-reported tokens are separate actual activation-inference usage.
There are no downstream LLM calls in this retrieval-only phase, so document reduction alone is not an end-to-end cost-saving result.
Rank coverage and precision permit the alternatives explicitly recorded in labels.json.
Score each mission-role, then average roles/wordings within scenario and macro-average scenarios.
Report counts and descriptive statistics without inferential confidence claims from this small non-independent author-generated corpus.

## Runtime and Failures

Use the live configured planner model, temperature 0, 900 maximum output tokens per ranking, 60-second request timeout, and at most 36 ranking requests.
No automatic model retry or rule fallback. A failed call remains a failed row with empty semantic selection.
Stop on authentication failures or after three consecutive call failures; retain incomplete outputs.
Overall hard timeout: 1800 seconds, with a process-alive/output heartbeat every 30 seconds.
Use an isolated dependency venv and do not restart or mutate the online environment.
Send only synthetic task instructions and authored capability descriptions to the configured provider, not the manuscript or private telemetry.
Record input hashes, code hashes, package versions, raw rankings, usage, and all errors.

## Outputs

run_manifest.json, raw_rankings.jsonl, metrics.csv, summary.json, failures.json, report.md, and run.log in the unique run directory.
Do not replace previous runs. Do not insert pilot scores into the paper automatically.
After the run, review failures and label ambiguity before scheduling a separately frozen formal test.
