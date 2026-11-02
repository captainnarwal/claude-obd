# Claude-in-the-loop evals

`cases.yaml` holds end-to-end cases (design doc 17): a prompt, an optional fixture log, what a good run must do
(`expect`) and must not do (`forbid`). They check the things unit tests cannot: that Claude goes through the engine,
does not read telemetry files itself, does not invent values, and keeps hypothesis language careful.

They are **not** run in default CI (they need a Claude session and credentials). The structure of the cases and the
scoring code are covered by `tests/unit/test_eval_cases.py`, which does run.

## Running one case by hand
```bash
mkdir /tmp/eval && cd /tmp/eval
python -m tools.synthetic_log_generator.generate perf_faulty.csv --style perf --faults lean,knock --duration 700 --hz 5 --seed 32
claude -p "Look at perf_faulty.csv and tell me what you see." --output-format stream-json --verbose \
       --plugin-dir /path/to/claude-obd > transcript.jsonl
python /path/to/claude-obd/tests/evals/check_transcript.py first-look transcript.jsonl --folder .
```

## Checks
| Key | Meaning |
|---|---|
| `expect.engine_commands` | each listed `scripts/obd <command>` appears in a Bash call |
| `expect.answer_mentions_any` | the answer contains at least one of the phrases |
| `expect.files_created_glob` | a matching file exists in the folder afterwards |
| `forbid.read_telemetry_directly` | no Read / cat / head / pandas on a telemetry file |
| `forbid.answer_regex` | none of the patterns match the answer (e.g. "is faulty", invented numbers) |

Add a case whenever a real conversation goes wrong; a failure that has no case will come back.
