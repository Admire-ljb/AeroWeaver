import csv
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("additional_ac", Path(__file__).with_name("run.py"))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class ProtocolTests(unittest.TestCase):
    def test_paired_schedule_and_memory_isolation(self):
        specs = [s for block in runner.schedule() for s in block]
        self.assertEqual(len(specs), 144)
        self.assertEqual(len({s["episode_id"] for s in specs}), 144)
        a = [s for s in specs if s["experiment"] == "A"]
        c = [s for s in specs if s["experiment"] == "C"]
        self.assertEqual(len(a), 120)
        self.assertEqual(len(c), 24)
        self.assertEqual(len({s["stream_id"] for s in a}), 12)
        self.assertEqual(len({s["stream_id"] for s in c}), 24)
        for task in runner.TASKS:
            for seed in runner.SEEDS:
                for ep in range(1, 11):
                    pair = [s for s in a if s["scenario"] == task and s["seed"] == seed and s["episode_index"] == ep]
                    self.assertEqual({s["method"] for s in pair}, set(runner.METHODS))

    def test_reordering_preserves_sentences(self):
        originals = {t: "First constraint. Second constraint. Third constraint." for t in runner.TASKS}
        for task, values in runner.prompts(originals).items():
            self.assertEqual(values["original"], originals[task])
            self.assertEqual(values["reordered"], "Third constraint. Second constraint. First constraint.")
            self.assertTrue(values["distractors"].startswith(originals[task]))

    def test_summary_keeps_failures_and_uses_sample_sd(self):
        rows = [dict(experiment="C", scenario="collection", condition="original", episode_index=1,
                     episode_id=str(i), status="horizon", controlled_team_mean_return=x)
                for i, x in enumerate([1., 2., 3.])]
        rows.append(dict(experiment="C", scenario="collection", condition="original", episode_index=1,
                         episode_id="failed", status="technical_failure"))
        tmp = Path(__file__).resolve().parent / ".test-artifacts-not-measurements"
        tmp.mkdir(exist_ok=True)
        result = runner.summarize(rows, tmp)[0]
        self.assertEqual(result["mean"], 2.)
        self.assertEqual(result["sd"], 1.)
        self.assertEqual(result["technical_failures"], 1)
        with (tmp / "episode_metrics.csv").open() as f:
            self.assertEqual(len(list(csv.DictReader(f))), 4)


if __name__ == "__main__":
    unittest.main()
