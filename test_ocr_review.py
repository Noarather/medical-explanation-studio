"""Offline single-page review tests: no credentials or real textbooks."""
from types import SimpleNamespace
import fitz
import pytest
from db_manager import DatabaseManager
from ocr_review import compare_page, get_report, history
from ocr_client import QwenOCRClient
from ui_bridge.library import LibraryBridge

@pytest.fixture
def source(tmp_path):
    pdf = tmp_path / 'synthetic.pdf'
    with fitz.open() as doc:
        doc.new_page().insert_text((40, 60), 'Synthetic 140 mmol/L')
        doc.save(pdf)
    database = str(tmp_path / 'test.db')
    with DatabaseManager(database) as db:
        library = db.add_library('合成教材', '测试', str(pdf), '', 0)
    return database, library

def config():
    return {'dashscope': {'api_key': 'synthetic', 'ocr_model': 'qwen3.5-ocr'}}

def fake_client(monkeypatch, *, fail=False):
    calls=[]
    class Fake:
        def __init__(self, key, model, *args, **kw):
            self.model=model; self.last_trace={}; self.client=SimpleNamespace(close=lambda: None)
        def recognize_image(self, path, max_retries):
            assert max_retries == 1
            calls.append((self.model, path.read_bytes()))
            if fail and self.model == 'qwen3.8-max':
                raise RuntimeError('云端 OCR 请求失败（HTTP 403）')
            self.last_trace={'model':self.model,'usage':{'total_tokens':12},'requestId':'synthetic'}
            self.last_raw_text='original 140'
            return 'normalized 140'
    monkeypatch.setattr('ocr_review.QwenOCRClient', Fake)
    return calls

def test_review_preserves_image_and_outputs_without_index_changes(source,monkeypatch):
    database, library=source
    calls=fake_client(monkeypatch)
    result=compare_page(database,library,1,'qwen3.8-max',config(),lambda *a:None)
    assert len(calls)==2 and calls[0][1]==calls[1][1]
    assert result['total_tokens']==24 and result['identical']
    assert result['index_modified'] is False
    assert result['image'].startswith('data:image/png;base64,')
    assert result['results'][0]['raw_text']=='original 140'
    assert get_report(database,library,result['id'])==result
    assert len(history(database,library))==1
    with DatabaseManager(database) as db:
        assert db.conn.execute('SELECT COUNT(*) FROM textbook_files').fetchone()[0]==0
        assert db.conn.execute('SELECT COUNT(*) FROM chunks_v2').fetchone()[0]==0
    with pytest.raises(ValueError): get_report(database, library+1, result['id'])

def test_failed_advanced_call_keeps_baseline_and_history(source,monkeypatch):
    database,library=source
    calls=fake_client(monkeypatch,fail=True)
    result=compare_page(database,library,1,'qwen3.8-max',config(),lambda *a:None)
    assert len(calls)==2 and result['results'][0]['ok']
    assert not result['results'][1]['ok'] and result['total_tokens']==12
    assert len(history(database,library))==1

@pytest.mark.parametrize('page,model', [(0,'qwen3.8-max'),(True,'qwen3.8-max'),(2,'qwen3.8-max'),(1,'unknown')])
def test_invalid_request_never_calls_cloud(source,monkeypatch,page,model):
    calls=fake_client(monkeypatch)
    with pytest.raises(ValueError): compare_page(*source,page,model,config(),lambda *a:None)
    assert not calls

def test_no_key_or_same_model_never_calls_cloud(source,monkeypatch):
    calls=fake_client(monkeypatch)
    for dash in ({'api_key':''},{'api_key':'synthetic','ocr_model':'qwen3.8-max'}):
        with pytest.raises(ValueError): compare_page(*source,1,'qwen3.8-max',{'dashscope':dash},lambda *a:None)
    assert not calls

def test_bridge_requires_explicit_consent(source):
    bridge=LibraryBridge(source[0])
    with pytest.raises(ValueError,match='费用'):
        bridge.api_ocr_review_start(source[1],1,'qwen3.8-max')

@pytest.mark.parametrize('model,thinking', [('qwen3.8-max',True),('qwen3.7-plus',True),('qwen3.5-ocr',False)])
def test_general_visual_models_disable_thinking(tmp_path,model,thinking):
    calls=[]
    client=QwenOCRClient('synthetic',model)
    client.client.close()
    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(model=model,id='synthetic',usage=None,choices=[SimpleNamespace(
            finish_reason='stop',message=SimpleNamespace(content='原始输出'))])
    client.client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    path=tmp_path/'page.png';path.write_bytes(b'png')
    assert client.recognize_image(path)=='原始输出'
    assert client.last_raw_text=='原始输出'
    assert ('extra_body' in calls[0]) == thinking
    if thinking: assert calls[0]['extra_body']=={'enable_thinking':False}
