"""Bounded same-model diagnosis, with old and fixed protocols kept separate."""
import argparse
import csv
import json
from functools import partial
from pathlib import Path

import pilot
import run_all_tasks as suite


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=['interface', 'legacy', 'fixed'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--activation', type=Path, required=True)
    args = parser.parse_args()
    suite.configure_support()
    args.output.mkdir(parents=True, exist_ok=False)
    client = pilot.Client(args.output / 'calls.jsonl', call_limit=100, token_limit=250000,
                          max_inflight=2, model=pilot.DEFAULT_MODEL)
    if args.phase == 'interface':
        suite.check_model_interface(client, args.output)
        print(json.dumps({'interface': 'passed', 'model': client.model}), flush=True)
        return 0
    skills = json.loads(args.catalog.read_text())
    active = json.loads(args.activation.read_text())['active']
    methods = ['aeroweaver_pilot', 'aeroweaver_no_rl'] if args.phase == 'legacy' else suite.CONDITIONS
    pilot.support.dump(args.output / 'plan.json', {
        'phase': args.phase, 'methods': list(methods), 'seed': 66001, 'rounds': 3,
        'model': client.model, 'calls_limit': 100, 'token_limit': 250000, 'retries': 0,
        'activation': active, 'activation_reused': True,
        'runtime_hashes': {name: pilot.support.sha(pilot.support.ROOT / name) for name in pilot.support.SOURCE_FILES},
        'reward_unchanged': True, 'old_results_overwritten': False,
        'purpose': 'Protocol regression diagnostic; not a multi-seed performance claim',
    })
    rows = []
    for method in methods:
        catalog = active if method in pilot.LOCAL_METHODS and method != 'aeroweaver_full_catalog' else [s['name'] for s in skills]
        row = pilot.run_episode('private_communication', method, 66001, args.phase + '_protocol_recheck',
                               args.output, skills, client, args.output / 'memory' / (method + '.sqlite3'),
                               rounds=3, active_override=catalog,
                               task_factory=partial(suite.ExperimentTask, condition=method))
        rows.append(row)
        pilot.support.dump(args.output / 'progress.json', rows)
        if row['status'] == 'technical_failure':
            break
    fields = ['method','model','status','rounds','task_success','controlled_team_mean_return',
              'decision_errors','invocation_errors','review_errors','memory_ranking_changes','records']
    with (args.output / 'summary.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
    pilot.support.dump(args.output / 'completion.json', {'episodes': len(rows), 'planned': len(methods),
                       'calls': client.calls, 'tokens': client.tokens, 'rows': rows})
    return int(len(rows) != len(methods) or any(r['status'] == 'technical_failure' for r in rows))


if __name__ == '__main__':
    raise SystemExit(main())
