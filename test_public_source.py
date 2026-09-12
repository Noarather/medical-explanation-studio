from tools.check_public_source import inspect


def test_public_gate_flags_sensitive_paths_and_config_without_printing_values(tmp_path):
    (tmp_path / "config.yaml").write_text('api_key: "not-a-public-placeholder"', encoding="utf-8")
    results = inspect(tmp_path, ["config.yaml", "data/questions.json", "private.db", "models/model.onnx"])
    assert len(results) == 4
    assert all("not-a-public-placeholder" not in str(item) for item in results)


def test_public_gate_accepts_placeholders_and_checks_token_patterns(tmp_path):
    (tmp_path / "config.yaml").write_text('api_key: "${DASHSCOPE_API_KEY}"', encoding="utf-8")
    assert inspect(tmp_path, ["config.yaml"]) == []
    (tmp_path / "bad.txt").write_text("ghp_" + "x" * 36, encoding="utf-8")
    assert inspect(tmp_path, ["bad.txt"])[0]["category"] == "provider-token"
