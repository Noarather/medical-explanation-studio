"""Offline cloud-only parser, migration and provider safeguards."""
import json
from types import SimpleNamespace
from pathlib import Path
import fitz
import httpx
import pytest
from openai import OpenAI
from ocr_client import QwenOCRClient, compatible_url, normalize_ocr_text, effective_ocr_model
from pdf_parser import PDFParser
from index_profile import build_index_profile, index_fingerprint, retrieval_compatible

@pytest.mark.parametrize('status,retries', [(401,1),(403,1),(404,1),(429,2),(500,2)])
def test_cloud_http_failure_is_bounded_and_redacted(tmp_path, monkeypatch, status, retries):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(status, json={'error':{'message':'private response body'}})
    monkeypatch.setattr('ocr_client.time.sleep', lambda _: None)
    client = QwenOCRClient('synthetic')
    client.client = OpenAI(api_key='synthetic', max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    path = tmp_path/'image.png'; path.write_bytes(b'png')
    with pytest.raises(RuntimeError) as caught:
        client.recognize_image(path)
    assert len(calls) == retries
    assert 'private response body' not in str(caught.value)

@pytest.mark.parametrize('finish,text', [('length','partial'),('stop',''),('content_filter','blocked')])
def test_incomplete_ocr_is_not_saved_as_success(tmp_path, finish, text):
    client = QwenOCRClient('synthetic')
    result = SimpleNamespace(model='qwen3.5-ocr', id='request', usage=None,
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=text))])
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: result)))
    path=tmp_path/'page.png'; path.write_bytes(b'png')
    with pytest.raises(RuntimeError): client.recognize_image(path)

def test_existing_docling_index_remains_usable_in_same_vector_space():
    config={'dashscope':{'model':'text-embedding-v4','dimension':1024,'base_url':'https://example.test/api/v1'},
            'retrieval':{'chunk_size':500,'chunk_overlap':50}}
    profile=build_index_profile(config)
    old={**profile, 'parser':'docling','parser_version':'2.117.0','ocr_engine':'rapidocr-onnx-ch'}
    row={'index_profile_json':json.dumps(old),'index_fingerprint':index_fingerprint(old)}
    assert retrieval_compatible(row,profile)
    assert not retrieval_compatible(row,{**profile,'dimension':512})
    assert not retrieval_compatible(row,{**profile,'model':'another-model'})
    assert not retrieval_compatible({**row,'index_fingerprint':'wrong'},profile)

def test_text_and_formula_normalization_is_conservative():
    assert normalize_ocr_text(r'$\begin{aligned} & \text { 血压 } 120/80 \\ & \mathrm{mmHg}\end{aligned}$') == '血压  120/80 \nmmHg'
    assert r'\frac{a}{b}' in normalize_ocr_text(r'$\frac{a}{b}$')
    assert effective_ocr_model('qwen-vl-ocr') == 'qwen3.5-ocr'
    assert effective_ocr_model('custom-model') == 'custom-model'
    assert normalize_ocr_text('$ 123 $') == '123'
    assert '| --- | --- |' in normalize_ocr_text('指标\t数值\n钠\t140')
    assert compatible_url('https://dashscope.aliyuncs.com/api/v1') == 'https://dashscope.aliyuncs.com/compatible-mode/v1'

def test_force_ocr_does_not_silently_use_text_layer(tmp_path):
    path=tmp_path/'text.pdf'
    with fitz.open() as doc:
        doc.new_page().insert_text((72,72),'Readable source text')
        doc.save(path)
    with pytest.raises(RuntimeError, match='需要云端 OCR'):
        PDFParser().extract_pages(path, force_ocr=True)
    assert PDFParser().extract_pages(path)[0]['extraction_method']=='text'

def test_true_blank_page_does_not_call_cloud(tmp_path):
    path=tmp_path/'blank.pdf'
    with fitz.open() as doc:
        doc.new_page(); doc.save(path)
    ocr=SimpleNamespace(recognize_image=lambda _: pytest.fail('blank page must not cost tokens'))
    assert PDFParser().extract_pages(path,ocr)[0]['extraction_method']=='blank'

def test_bundle_recipe_excludes_local_ai_weights():
    root=Path(__file__).parent
    spec=(root/'med_explain.spec').read_text(encoding='utf-8-sig')
    assert '("models", "models")' not in spec
    assert 'collect_data_files("rapidocr")' not in spec
    assert '"torch"' in spec.split('excludes=')[1]
    assert 'docling[' not in (root/'requirements.txt').read_text()
