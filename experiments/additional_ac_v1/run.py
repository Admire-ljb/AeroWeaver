"""Measured two-task adaptation and prompt-robustness experiments."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import csv
from functools import partial
import json
from pathlib import Path
import random
import statistics
import sys
import threading
import time
import traceback

TASKS = ("world_communication", "collection")
SEEDS = (66201, 66202, 66203)
VARIANTS = ("original", "paraphrased", "reordered", "distractors")
METHODS = ("aeroweaver_pilot", "aeroweaver_no_rl")


def prompts(originals):
    paraphrases = {
        "world_communication": (
            "A mobile leader works with two pursuers against two foragers. "
            "With its broader sensing range, the leader can share information with its teammates. "
            "The pursuers aim to contact the foragers. The foragers look for food and evade pursuit using forest cover. "
            "Communication is restricted to teammates that are reachable."
        ),
        "collection": (
            "Two moving collectors gather treasures and bring them to moving deposit participants of the corresponding color: one red and one blue. "
            "A collector may carry no more than one treasure. Using local observations and peer reports, "
            "deposits rendezvous with loaded collectors carrying a compatible treasure."
        ),
    }
    result = {}
    for task in TASKS:
        sentences = originals[task].split(". ")
        sentences = [s.rstrip(".") + "." for s in sentences]
        result[task] = {
            "original": originals[task],
            "paraphrased": paraphrases[task],
            "reordered": " ".join(reversed(sentences)),
            "distractors": originals[task] + (
                " Unrelated archive metadata: the report folder is named Cedar; "
                "the cover sheet uses a gray border; the archived document has three sections. "
                "These metadata do not describe the arena or change the task."
            ),
        }
    return result


def schedule():
    blocks = []
    # Complete the prompt test first, then advance every adaptation stream once per block.
    for seed in SEEDS:
        block = [dict(experiment="C", scenario=t, seed=seed, condition=v,
                      method="aeroweaver_pilot", episode_index=1) for t in TASKS for v in VARIANTS]
        random.Random(seed).shuffle(block)
        blocks.append(block)
    for ep in range(1, 11):
        block = [dict(experiment="A", scenario=t, seed=s, condition=m, method=m,
                      episode_index=ep) for t in TASKS for s in SEEDS for m in METHODS]
        random.Random(67000 + ep).shuffle(block)
        blocks.append(block)
    for block in blocks:
        for spec in block:
            spec["stream_id"] = f"{spec['experiment']}-{spec['scenario']}-{spec['condition']}-{spec['seed']}"
            spec["episode_id"] = f"{spec['stream_id']}-ep{spec['episode_index']:02d}"
    return blocks


FIELDS = ("experiment", "scenario", "condition", "seed", "episode_index", "episode_id", "status",
          "controlled_team_mean_return", "rounds", "calls", "tokens", "decision_errors", "invocation_errors",
          "memory_ranking_changes", "retrieval_decisions", "cross_episode_retrieval_references",
          "cross_episode_retrieval_decisions", "uncertified_rankings", "initial_state_sha256",
          "initial_memory_records", "model", "response_models", "elapsed_s", "error")


def summarize(rows, output):
    with (output / "episode_metrics.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: r["episode_id"]))
    groups = defaultdict(list)
    for row in rows:
        groups[(row["experiment"], row["scenario"], row["condition"], row["episode_index"])].append(row)
    aggregated = []
    for (exp, task, condition, ep), group in sorted(groups.items()):
        valid = [r["controlled_team_mean_return"] for r in group if r.get("status") in {"horizon", "complete"}]
        aggregated.append(dict(experiment=exp, scenario=task, condition=condition, episode_index=ep,
                               attempted=len(group), measured=len(valid),
                               technical_failures=sum(r.get("status") == "technical_failure" for r in group),
                               mean=statistics.mean(valid) if valid else None,
                               sd=statistics.stdev(valid) if len(valid) > 1 else None))
    if aggregated:
        with (output / "aggregate_metrics.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, list(aggregated[0]))
            writer.writeheader()
            writer.writerows(aggregated)
    return aggregated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot-inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.pilot_inputs))
    import pilot
    import run_all_tasks as suite
    suite.configure_support()
    support = pilot.support
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    (output / "calls").mkdir()
    skills = json.loads((args.pilot_inputs / "catalog.json").read_text())
    texts = prompts({t: support.TASK_TEXT[t] for t in TASKS})
    blocks = schedule()
    sources = {n: support.sha(support.ROOT / n) for n in support.SOURCE_FILES}
    plan = dict(tasks=TASKS, seeds=SEEDS, planned_episodes=144, rounds=24,
                model="deepseek-v4-flash", gamma=.95, full_beta=.8, control_beta=0,
                prompts=texts, schedule=blocks, source_hashes=sources,
                runner_sha256=support.sha(Path(__file__)),
                input_hashes={p.name: support.sha(p) for p in args.pilot_inputs.glob("*.py")},
                catalog_sha256=support.sha(args.pilot_inputs / "catalog.json"),
                adaptation_protocol="10 repeated-layout episodes per seed/condition; separate persistent memory per stream; no held-out-generalization claim",
                prompt_protocol="One episode per prompt and seed, independent empty memory; fixed shared task activation; perturb mission text at local action selection only",
                initial_memory="Empty per A stream and per C condition/seed; no production memory imported",
                activation="One original-prompt activation per task shared across all conditions",
                endpoints="Raw controlled-team mean return; actual API tokens; trace-audited reranking and cross-episode retrieval",
                uncertainty="Sample SD across 3 independent seed streams, never across sequential adaptation episodes; no significance claim",
                limits=dict(calls=15000, observed_tokens=30000000, elapsed_s=21600,
                            max_inflight=3, per_episode_calls=150, per_episode_observed_tokens=600000),
                retries=0, production_memory_modified=False, native_mpe_rollouts=False,
                failure_policy="Keep all attempts and error-stop decisions. Stop failed A stream; stop batch on provider or global budget failure. No result-dependent reruns.")
    support.dump(output / "plan.json", plan)
    stop = threading.Event()
    slots = threading.BoundedSemaphore(3)
    lock = threading.Lock()
    budget = dict(calls=0, tokens=0, started=time.monotonic())

    class Client(pilot.Client):
        def __init__(self, path, mission=None):
            super().__init__(path, call_limit=150, token_limit=600000, model=plan["model"])
            self.mission = mission

        def request(self, messages, context, max_tokens=300, logits=False):
            messages = deepcopy(messages)
            if self.mission is not None:
                for message in messages:
                    if message["role"] == "user":
                        data = json.loads(message["content"])
                        if "mission" in data:
                            data["mission"] = self.mission
                        message["content"] = json.dumps(data, separators=(",", ":"))
            with slots:
                with lock:
                    if stop.is_set() or budget["calls"] >= 15000 or budget["tokens"] >= 30000000 or time.monotonic() - budget["started"] >= 21600:
                        stop.set()
                        raise pilot.BatchStop("Shared call/token/time/provider stop")
                    budget["calls"] += 1
                try:
                    result = super().request(messages, context, max_tokens, logits)
                    with lock:
                        budget["tokens"] += result.get("usage", {}).get("total_tokens", 0)
                    return result
                except pilot.BatchStop:
                    stop.set()
                    raise

    rows = []
    failed_streams = set()
    final = dict(status="failed", planned_episodes=144)
    try:
        client = Client(output / "calls" / "setup.jsonl")
        suite.check_model_interface(client, output)
        active = {t: suite.activate_once(client, t, skills, output) for t in TASKS}

        def one(spec):
            memory_path = output / "memory" / (spec["stream_id"] + ".sqlite3")
            if spec["episode_index"] == 1 and memory_path.exists():
                raise FileExistsError("Initial memory must be empty")
            memory_count = 0
            if memory_path.exists():
                import sqlite3
                with sqlite3.connect(memory_path) as db:
                    memory_count = db.execute("SELECT count(*) FROM transitions").fetchone()[0]
            path = output / "calls" / (spec["episode_id"] + ".jsonl")
            client = Client(path, texts[spec["scenario"]][spec["condition"] if spec["experiment"] == "C" else "original"])
            try:
                row = pilot.run_episode(spec["scenario"], spec["method"], spec["seed"], spec["experiment"],
                    output, skills, client, memory_path, rounds=24, active_override=active[spec["scenario"]],
                    task_factory=partial(suite.ExperimentTask, condition=spec["method"]), episode_id=spec["episode_id"])
            except Exception as exc:
                row = dict(status="technical_failure", error=str(exc), traceback=traceback.format_exc())
            row.update(spec, initial_memory_records=memory_count, calls=client.calls, tokens=client.tokens)
            row["cross_episode_retrieval_references"] = row["cross_episode_retrieval_decisions"] = 0
            trace = output / "episodes" / spec["episode_id"] / "trace.jsonl"
            if trace.exists():
                for line in trace.read_text().splitlines():
                    step = json.loads(line)
                    for decision in step["decisions"].values():
                        retrieved = (decision.get("ranking") or {}).get("retrieved", [])
                        count = sum(r["mission"] != spec["episode_id"] for r in retrieved)
                        row["cross_episode_retrieval_references"] += count
                        row["cross_episode_retrieval_decisions"] += bool(count)
            response_models = set()
            if path.exists():
                for line in path.read_text().splitlines():
                    model = json.loads(line).get("response", {}).get("model")
                    if model:
                        response_models.add(model)
            row["response_models"] = ";".join(sorted(response_models))
            support.dump(output / "episodes" / spec["episode_id"] / "summary.json", row)
            return row

        for index, block in enumerate(blocks):
            if stop.is_set():
                break
            todo = [s for s in block if s["stream_id"] not in failed_streams]
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(one, spec): spec for spec in todo}
                for future in as_completed(futures):
                    row = future.result()
                    rows.append(row)
                    if row["status"] == "technical_failure":
                        failed_streams.add(row["stream_id"])
                    summarize(rows, output)
                    support.dump(output / "progress.json", dict(finished=len(rows), planned=144, block=index,
                                                                 calls=budget["calls"], tokens=budget["tokens"]))
                    print(json.dumps(dict(finished=len(rows), episode=row["episode_id"], status=row["status"],
                                          return_value=row.get("controlled_team_mean_return"),
                                          cross_episode=row["cross_episode_retrieval_decisions"],
                                          tokens=budget["tokens"])), flush=True)
        final["status"] = "complete" if len(rows) == 144 and not failed_streams else "partial"
    except Exception as exc:
        final.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        summarize(rows, output)
        final.update(attempted_episodes=len(rows), measured_episodes=sum(r["status"] in {"horizon", "complete"} for r in rows),
                     technical_failures=sum(r["status"] == "technical_failure" for r in rows), failed_streams=sorted(failed_streams),
                     calls=budget["calls"], tokens=budget["tokens"], elapsed_s=time.monotonic()-budget["started"],
                     source_hashes_unchanged=all(support.sha(support.ROOT/n) == h for n, h in sources.items()))
        support.dump(output / "completion.json", final)
        print(json.dumps(final), flush=True)
    return 0 if final["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
