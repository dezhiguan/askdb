from askdb.diagnostics.worker_timeout import probe


def test_isolated_worker_timeout_probe_uses_real_executor(sample_db):
    result = probe(sample_db)
    assert result["ok"] is True
    assert result["source"] == "isolated_duckdb"
    assert result["elapsed_ms"] < 5000
