import json



def _ingest(run, copy_log, key, name):
    copy_log(key, name)
    code, env = run("ingest", name)
    assert code == 0, env
    return env["data"]["dataset_id"]


def _findings(env):
    return [(m["module"], f) for m in env["data"]["modules"] for f in m["findings"]]


def _titles(env):
    return " | ".join(f["title"] for _, f in _findings(env))


def test_clean_log_has_no_attention_findings(run, copy_log):
    _ingest(run, copy_log, "clean", "clean.csv")
    code, env = run("analyze", "clean.csv")
    assert env["status"] == "ok"
    bad = [(m, f["title"]) for m, f in _findings(env) if f["severity"] != "info"]
    assert bad == [], bad                                   # no false positives


def test_injected_faults_detected(run, copy_log):
    _ingest(run, copy_log, "faulty", "faulty.csv")
    code, env = run("analyze", "faulty.csv")
    titles = _titles(env)
    assert "ect exceeded 120" in titles                     # overheat
    assert "isolated spike to 9800" in titles               # rpm spike
    assert "Dropouts in" in titles                          # logger dropout
    ect = next(f for _, f in _findings(env) if "exceeded 120" in f["title"])
    assert ect["severity"] == "warning" and ect["confidence"] == "medium"   # generic threshold cap
    assert ect["threshold"]["tier"] == "generic" and ect["evidence"][0]["n"] > 50


def test_sentinels_and_resets_reported(run, copy_log):
    _ingest(run, copy_log, "generic", "g.csv")
    _, env = run("analyze", "g.csv")
    titles = _titles(env)
    assert "sentinel" in titles and "timestamp reset" in titles


def test_lean_pattern_visible_in_condition_stats(run, copy_log):
    _ingest(run, copy_log, "generic_lean", "lean.csv")
    code, env = run("stats", "lean.csv", "--params", "ltft_b1", "--by-condition")
    cond = env["data"]["by_condition"]["ltft_b1"]
    assert cond["idle"]["mean"] > 10                         # injected +14 % at idle
    assert cond["wot"]["mean"] < 4
    assert cond["idle"]["n"] > 20


def test_results_are_reproducible_and_cached(run, copy_log):
    _ingest(run, copy_log, "faulty", "faulty.csv")
    _, a = run("analyze", "faulty.csv", "--no-cache")
    _, b = run("analyze", "faulty.csv", "--no-cache")
    for env in (a, b):
        env["data"].pop("run_id")
    assert json.dumps(a["data"], sort_keys=True) == json.dumps(b["data"], sort_keys=True)
    _, c = run("analyze", "faulty.csv")
    assert any("cache" in x for x in c["caveats"])


def test_mapping_change_invalidates_cache(run, copy_log):
    _ingest(run, copy_log, "faulty", "faulty.csv")
    run("analyze", "faulty.csv")
    run("map", "faulty.csv", "--set", "Bearing=custom")
    _, env = run("analyze", "faulty.csv")
    assert not any("cache" in x for x in env["caveats"])


def test_modules_skipped_with_reason(run, ws):
    (ws / "one.csv").write_text("Time,Banana\n" + "\n".join(f"{i},{i%7}" for i in range(100)), encoding="utf-8")
    run("ingest", "one.csv")
    _, env = run("analyze", "one.csv")
    skipped = {s["module"]: s["reason"] for s in env["data"]["skipped"]}
    assert "correlation" in skipped and "two" in skipped["correlation"]
    assert any(m["module"] == "data_quality" for m in env["data"]["modules"])


def test_correlate_and_anomalies_commands(run, copy_log):
    _ingest(run, copy_log, "faulty", "faulty.csv")
    _, c = run("correlate", "faulty.csv", "--params", "throttle_position,engine_load")
    assert c["data"]["pairs"][0]["pearson"] > 0.95 and c["data"]["pairs"][0]["n"] > 100
    _, a = run("anomalies", "faulty.csv", "--params", "engine_speed", "--sensitivity", "low")
    assert a["data"]["total_events"] >= 1 and a["data"]["events"][0]["type"] == "spike"


def test_query_bounded_and_flag_exclusion(run, copy_log):
    _ingest(run, copy_log, "generic", "g.csv")
    _, q = run("query", "g.csv", "--params", "engine_speed,ect", "--max-points", "50")
    assert len(q["data"]["rows"]) <= 50 and q["truncated"]
    ects = [r[2] for r in q["data"]["rows"] if r[2] is not None]
    assert min(ects) > -39                                    # sentinel -40 C excluded by default
    _, q2 = run("query", "g.csv", "--params", "ect", "--include-flagged", "--max-points", "1000")
    _, q3 = run("query", "g.csv", "--params", "ect", "--t0", "0", "--t1", "5", "--max-points", "1000")
    assert q3["data"]["rows"] and all(r[0] <= 5 for r in q3["data"]["rows"])
    assert len(q2["data"]["rows"]) > 100                      # thinned to budget, not to 20 rows


def test_parameter_not_available_lists_near_matches(run, copy_log):
    _ingest(run, copy_log, "clean", "clean.csv")
    code, env = run("query", "clean.csv", "--params", "boost")
    assert env["error"]["code"] == "PARAMETER_NOT_AVAILABLE" and "near_matches" in env["data"]
