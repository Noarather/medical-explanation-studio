"""Provider contracts, draft testing and index repair; no live API requests."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from openai import OpenAI

from credentials import CredentialStore
from db_manager import DatabaseManager
from llm_client import LLMClient, LLMRequestError, ExplanationPackageError
from model_config import normalized_url, select_model, require_key_for_new_endpoint
from model_gateway import complete_native, GatewayError
from parser_health import index_error_summary, parser_health
from ui_bridge.library import _index_state
from ui_bridge.settings import SettingsBridge, SETTINGS_DEFAULTS


class Keys:
    def __init__(self):
        self.keys = {"deepseek": "saved-deep", "claude": "saved-claude", "gemini": "saved-gemini"}
    def get(self, provider): return self.keys.get(provider, "")
    def configured(self, provider): return bool(self.get(provider))
    def set(self, provider, value): self.keys[provider] = value


@pytest.mark.parametrize("protocol,url,expected", [
    ("openai", "https://example.test", "https://example.test/v1"),
    ("openai", "http://localhost:1234/v1/chat/completions", "http://localhost:1234/v1"),
    ("anthropic", "https://example.test/proxy/v1/messages/", "https://example.test/proxy/v1"),
    ("gemini", "https://example.test/v1beta/models/demo:generateContent", "https://example.test/v1beta"),
])
def test_normalize_endpoints(protocol, url, expected):
    assert normalized_url(url, protocol) == expected


# Assemble synthetic userinfo so the source safety gate still rejects real URL credentials.
@pytest.mark.parametrize("url", ["api.test", "file:///a", "https://" + "key:secret@a.test", "https://a.test?key=secret", "https://a.test:bad"])
def test_invalid_urls_are_rejected(url):
    with pytest.raises(ValueError): normalized_url(url)


@pytest.mark.parametrize("protocol,reply,header,suffix", [
    ("anthropic", {"id": "r1", "content": [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "OK"}], "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 1}}, "x-api-key", "/v1/messages"),
    ("gemini", {"responseId": "r1", "candidates": [{"content": {"parts": [{"thought": True, "text": "hidden"}, {"text": "OK"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}}, "x-goog-api-key", "/v1beta/models/demo:generateContent"),
])
def test_native_wire_contract(protocol, reply, header, suffix):
    def handle(req):
        assert str(req.url).endswith(suffix)
        assert req.headers[header] == "secret"
        assert "secret" not in str(req.url)
        body = json.loads(req.content)
        assert "thinking" not in body and "temperature" not in body
        if protocol == "anthropic":
            assert body["model"] == "demo" and body["messages"][0]["content"] == "ping"
            assert req.headers["anthropic-version"] == "2023-06-01"
        else:
            assert body["contents"][0]["parts"] == [{"text": "ping"}]
            assert body["generationConfig"]["maxOutputTokens"] == 1024
        return httpx.Response(200, json=reply)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        text, meta = complete_native(protocol=protocol, base_url="https://mock.test", api_key="secret", model="demo", system="s", prompt="ping", temperature=.3, max_tokens=1024, timeout=1, client=client)
    assert text == "OK" and meta["requestId"] == "r1" and meta["promptTokens"] == 3


@pytest.mark.parametrize("status", [401, 404, 429, 503])
def test_native_error_redaction(status):
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, json={"error": {"message": "bad secret"}}, headers={"retry-after": "2"}))) as client:
        with pytest.raises(GatewayError) as error:
            complete_native(protocol="anthropic", base_url="https://mock.test", api_key="secret", model="m", system="s", prompt="p", temperature=.3, max_tokens=100, timeout=1, client=client)
    assert error.value.status_code == status and error.value.retry_after == "2"
    assert "secret" not in str(error.value)


def test_custom_openai_does_real_completion_without_deepseek_flags():
    def handle(req):
        assert req.url.path == "/v1/chat/completions"
        body = json.loads(req.content)
        assert body["model"] == "my-model" and "thinking" not in body and "response_format" not in body
        return httpx.Response(200, json={"id": "r1", "model": "my-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        with OpenAI(api_key="secret", base_url="https://mock.test/v1", http_client=transport) as sdk:
            llm = LLMClient("secret", "https://mock.test/v1", "my-model", provider="custom")
            llm.client = sdk
            assert llm._complete("只输出 JSON", thinking=True) == "OK"


def test_native_errors_obey_shared_rate_controller():
    llm = LLMClient("secret", "https://mock.test", "demo", provider="claude", protocol="anthropic")
    with patch("llm_client.complete_native", side_effect=GatewayError("secret throttled", 429, "4")):
        with pytest.raises(LLMRequestError) as error: llm._complete("p")
    assert error.value.retryable and error.value.retry_after == 4 and "secret" not in str(error.value)
    with patch("llm_client.complete_native", return_value=("partial", {"finishReason": "length"})):
        with pytest.raises(ExplanationPackageError): llm._complete("p")


def test_endpoint_change_requires_key_but_equivalent_url_does_not():
    before = {"claude_base_url": "https://api.test/v1"}
    require_key_for_new_endpoint(before, {"claude_base_url": "https://api.test/v1/messages"}, "claude", "")
    with pytest.raises(ValueError, match="key_required"):
        require_key_for_new_endpoint(before, {"claude_base_url": "https://other.test"}, "claude", "")


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    keys = Keys()
    monkeypatch.setattr("ui_bridge.settings.CredentialStore", lambda: keys)
    b = SettingsBridge(str(tmp_path / "test.db"))
    return b, keys


def test_draft_test_uses_current_model_and_key_without_saving(bridge, monkeypatch):
    b, keys = bridge
    results = []
    b.api_test_result.connect(lambda raw: results.append(json.loads(raw)))
    captured = []
    monkeypatch.setattr("ui_bridge.settings.threading.Thread", lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)))
    def complete(client, prompt, **kwargs):
        captured.append((client.protocol, client.base_url, client.model, client.api_key, prompt))
        return "OK"
    monkeypatch.setattr(LLMClient, "_complete", complete)
    b.api_test("claude", {"claude_base_url": "https://draft.test/v1", "claude_model": "draft-model"}, "draft-key", "draft-1")
    assert captured == [("anthropic", "https://draft.test/v1", "draft-model", "draft-key", "Reply with exactly OK.")]
    assert results[0]["ok"] and results[0]["request_id"] == "draft-1"
    assert b.api_get()["settings"]["claude_model"] == "" and keys.get("claude") == "saved-claude"


def test_test_does_not_send_saved_key_to_new_address(bridge):
    b, _ = bridge
    with pytest.raises(ValueError, match="key_required"):
        b.api_test("deepseek", {"deepseek_base_url": "https://other.test"})


def test_model_test_failure_never_exposes_key(bridge, monkeypatch):
    b, _ = bridge
    results = []
    b.api_test_result.connect(lambda raw: results.append(json.loads(raw)))
    monkeypatch.setattr(LLMClient, "_complete", Mock(side_effect=LLMRequestError("bad draft-secret", status_code=401)))
    b._run_test("deepseek", SETTINGS_DEFAULTS, "draft-secret", "failure")
    assert not results[0]["ok"] and "draft-secret" not in results[0]["message"]
    assert "API Key" in results[0]["message"]


def test_save_and_runtime_use_correct_database_and_no_deepseek_hard_model(bridge, tmp_path, monkeypatch):
    b, keys = bridge
    monkeypatch.setattr("main.CredentialStore", lambda: keys)
    monkeypatch.setenv("MEDEXPLAIN_DATABASE_PATH", str(tmp_path / "other.db"))
    b.api_save({"llm_provider": "gemini", "gemini_model": "chosen-model"}, {"gemini": "new-key"})
    from main import resolved_config, llm_client
    from worker import _llm
    config = resolved_config(str(Path(__file__).parent / "config.yaml"), b._database_path)
    assert config["llm"]["model"] == "chosen-model" and config["llm"]["api_key"] == "new-key"
    assert not config["generation"]["hard_use_pro"]
    assert config["generation"]["hard_model"] == "chosen-model"
    assert llm_client(config).protocol == _llm(config).protocol == "gemini"
    assert not (tmp_path / "other.db").exists()
    with DatabaseManager(b._database_path) as db:
        assert "new-key" not in str([tuple(row) for row in db.conn.execute("SELECT * FROM app_settings")])


def test_missing_parser_dependencies_are_readable(monkeypatch, tmp_path):
    monkeypatch.setattr("parser_health.importlib.util.find_spec", lambda name: None)
    health = parser_health(tmp_path)
    assert not health["ok"] and health["missing"] == ["docling", "rapidocr", "onnxruntime"]
    assert "run_desktop.ps1" in health["message"]
    message = index_error_summary("docling: No module named 'docling'; docling: No module named 'docling'; rapidocr: No module named 'rapidocr'")
    assert message.count("docling") == 1 and "重试异常索引" in message


def test_warning_index_is_never_shown_as_ready():
    assert _index_state({"file_count": 1, "file_status": "warning", "index_fingerprint": "same"}, "same") == "partial"
    assert _index_state({"file_count": 1, "file_status": "indexing", "index_fingerprint": "same"}, "same") == "unknown"


def test_warning_rescan_keeps_ready_fast_path_and_backs_up(tmp_path, monkeypatch):
    import fitz
    from services import TextbookService
    pdf = tmp_path / "book.pdf"
    with fitz.open() as doc:
        doc.new_page()
        doc.save(pdf)
    config = {"retrieval": {"chunk_size": 100, "chunk_overlap": 10}, "memory_guard": {"max_memory_mb": 2048, "batch_process_size": 50}}
    with DatabaseManager(str(tmp_path / "db.sqlite")) as db:
        book = db.add_library("book", "s", str(pdf))
        service = TextbookService(db, config, SimpleNamespace(get_embeddings=lambda texts: [[1., 0.] for _ in texts]))
        parser = Mock(side_effect=lambda *args, **kwargs: (batch for batch in [[{"page_number": 1, "text": "Textbook content sufficient for indexing with useful clinical context.", "extraction_method": "docling", "error_message": ""}]]))
        service.parser.iter_page_batches = parser
        monkeypatch.setattr("parser_health.parser_health", lambda *args: {"missing": []})
        backup = Mock()
        monkeypatch.setattr(db, "backup_database", backup)
        assert service.scan_library(book)["changed"] == 1
        assert service.scan_library(book)["skipped"] == 1
        db.conn.execute("UPDATE textbook_files SET status='warning', error_message='old dependency error'")
        db.conn.commit()
        assert service.scan_library(book)["changed"] == 1
        assert parser.call_count == 2 and backup.call_count == 1
        assert db.get_file_by_path(str(pdf.resolve()))["status"] == "ready"


def test_rapidocr_new_output_format_and_temp_cleanup(tmp_path):
    from pdf_parser import PDFParser
    p = PDFParser()
    image = tmp_path / "page.png"
    image.write_bytes(b"test")
    p._render_page = lambda page: str(image)
    p._rapidocr = lambda path: SimpleNamespace(txts=("one", "two"))
    assert p._rapid_ocr(None) == "one\ntwo"
    assert not image.exists()
