"""Reproducible native MPE2 engineering experiments. No paid model calls."""

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
# Native MPE allocates SDL resources even with render_mode=None. Keep batch
# runs independent of the host's desktop/audio session and repeated teardown.
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"

from memory.mpe_experience import MPEExperienceMemory
from sim.mpe_catalog import PAPER_SCENARIOS
from sim.mpe_runtime import MPERuntime


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenarios", nargs="+", default=list(PAPER_SCENARIOS), choices=PAPER_SCENARIOS)
    p.add_argument("--policies", nargs="+", default=["random_skills", "local_heuristic", "reward_memory", "individual_memory"],
                   choices=["random_skills", "local_heuristic", "reward_memory", "individual_memory"])
    p.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2])
    p.add_argument("--episodes", type=int, default=3)
    p.add_argument("--cycles", type=int, default=50)
    p.add_argument("--beta", type=float, default=0.1)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.episodes < 1 or len(set(args.seeds)) != len(args.seeds):
        p.error("Positive episode count and distinct seed streams required")
    args.output.mkdir(parents=True, exist_ok=False)
    source_paths = [Path(__file__), ROOT / "backend/sim/mpe_runtime.py", ROOT / "backend/sim/mpe_catalog.py", ROOT / "backend/sim/mpe_resources.py",
                    ROOT / "backend/skills/mpe_skills.py", ROOT / "backend/brain/mpe_policy.py", ROOT / "backend/memory/mpe_experience.py"]
    manifest = {**vars(args), "output": str(args.output), "status": "running", "experiment_type": "engineering_mock_policy_pilot",
                "llm_calls": 0, "claim_scope": "Native API, skills, reward recording, and memory plumbing; not LLM efficacy evidence",
                "reward_source": "unmodified_mpe2", "continuous_actions": False, "native_peer_channels_only": True,
                "sdl_video_driver": os.environ["SDL_VIDEODRIVER"], "sdl_audio_driver": os.environ["SDL_AUDIODRIVER"],
                "score_source": "fixed_local_heuristic_not_llm_logits", "gamma": 0.95, "memory_capacity": 2000, "neighbors": 8,
                "memory_admission": "completed episodes only; same scenario and team; reset each independent stream",
                "versions": {name: importlib.metadata.version(name) for name in ["mpe2", "pettingzoo", "numpy", "gymnasium"]},
                "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}}
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows = []
    started = time.monotonic()
    try:
        for scenario in args.scenarios:
            cycles = min(args.cycles, 25) if scenario == "simple_crypto" else args.cycles
            for policy in args.policies:
                for stream in args.seeds:
                    memory = MPEExperienceMemory(sharing="individual" if policy == "individual_memory" else "role")
                    for episode in range(args.episodes):
                        seed = stream * 10000 + episode
                        runtime = MPERuntime(scenario, seed, cycles, memory if "memory" in policy else None, args.beta)
                        try:
                            while not runtime.done:
                                runtime.policy_step("reward_memory" if policy == "individual_memory" else policy)
                            name = f"{scenario}-{policy}-s{stream}-e{episode}"
                            tracefile = args.output / f"{name}.jsonl"
                            with tracefile.open("w", encoding="utf-8") as handle:
                                for record in runtime.trace:
                                    handle.write(json.dumps(record) + "\n")
                            for agent, total in runtime.returns.items():
                                spec = runtime.specs[agent]
                                rows.append({"scenario": scenario, "policy": policy, "stream": stream, "episode": episode, "seed": seed,
                                             "agent": agent, "role": spec.role, "team": spec.team, "native_return": total,
                                             "cycles": runtime.round, "trace_sha256": runtime.trace_hash()})
                            print(f"{name}: cycles={runtime.round} decision_ms_median={statistics.median(r['decision_ms'] for r in runtime.trace):.3f}", flush=True)
                        finally:
                            runtime.close()
        with (args.output / "returns.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        # Descriptive only: never average rewards across scenarios or opposing teams.
        groups = {}
        for row in rows:
            key = (row["scenario"], row["policy"], row["team"])
            groups.setdefault(key, []).append(row["native_return"])
        summary = [{"scenario": k[0], "policy": k[1], "team": k[2], "mean_agent_episode_return": statistics.mean(v)} for k, v in groups.items()]
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        manifest.update(status="complete", elapsed_s=time.monotonic() - started, agent_episode_records=len(rows),
                        episodes_completed=sum(1 for _ in args.output.glob("*.jsonl")))
    except Exception as exc:
        manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
