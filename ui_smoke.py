"""Offline installed-app UI probe using a fresh database; never opens business data."""
import json
import os
import tempfile
import time
from pathlib import Path


def check_import_repair(app, window, js, folder, report_path):
    """Exercise the actual Vue/Qt bridge with synthetic data, never business files."""
    sample = Path(folder) / "synthetic-import.json"
    base = {"subject": "合成测试", "type": "A1", "answer": "D",
            "options": ["A. 甲", "B. 乙", "D. 丁", "E. 戊"], "explanation": "原文仅用于离线验收"}
    sample.write_text(json.dumps([
        {**base, "id": "repair", "question": "请选择合成选项 C原有内容"},
        {**base, "id": "manual", "question": "请选择另一合成选项"},
    ], ensure_ascii=False), encoding="utf-8")

    def until(condition):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if js(condition):
                return
            app.processEvents(); time.sleep(.05)
        raise RuntimeError("Import repair UI check timed out: " + condition)

    js("location.hash='#/import'")
    until("document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.has('imports') && document.body.innerText.includes('从文件导入')")
    js("window.__importSmoke=document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('imports');"
       "window.__importSmoke.inspectFile(" + json.dumps(str(sample)) + ").then(()=>window.__importSmoke.importFile()).catch(e=>window.__importSmokeError=String(e.message||e))")
    until("window.__importSmoke.fileReview?.issues === 1 && document.body.innerText.includes('缺少选项：C')")
    if not js("window.__importSmoke.fileReview.repaired === 1 && document.querySelector('details[open] [aria-label=题目原文]').textContent.includes('原文仅用于离线验收')"):
        raise RuntimeError("Missing repaired count or original source in import editor")
    js("document.querySelector('[aria-label=导入题目即时处理]').scrollIntoView({block:'start'});document.body.style.display='none';void document.body.offsetHeight;document.body.style.display=''")
    for _ in range(100):
        app.processEvents(); time.sleep(.02)
    if not window.view.grab().save(str(report_path.parent / "ui-import-repair.png")):
        raise RuntimeError("Could not save import repair screenshot")
    selector = "Array.from(document.querySelectorAll('[aria-label=导入题目即时处理] details')).find(e=>e.querySelector('summary').innerText.startsWith('第 2 题'))"

    def click(text):
        if not js("(()=>{const panel=" + selector + ";panel.open=true;const b=Array.from(panel.querySelectorAll('button')).find(e=>e.innerText.includes("
                  + json.dumps(text) + "));if(!b)return false;b.click();return true})()"):
            raise RuntimeError("Missing import repair button: " + text)

    click("删除本次导入")
    until("window.__importSmoke.fileReview?.removed === 1 && !window.__importSmoke.importing")
    click("撤销删除")
    until("window.__importSmoke.fileReview?.issues === 1 && !window.__importSmoke.importing")
    click("补一个选项")
    js("(()=>{const t=(" + selector + ").querySelector('[aria-label=异常题选项]');t.value=t.value.replace('C. ', 'C. 人工补充的合成选项');t.dispatchEvent(new Event('input',{bubbles:true}))})()")
    click("保存修正")
    until("window.__importSmoke.fileReview?.issues === 0 && !window.__importSmoke.importing")
    js("Array.from(document.querySelectorAll('[aria-label=导入题目即时处理] button')).find(b=>b.innerText.startsWith('确认导入')).click()")
    until("window.__importSmoke.lastImported?.question_count === 2 && !window.__importSmoke.importing")
    return {"syntheticQuestions": 2, "repaired": 1, "manualEdit": True, "removeRestore": True, "committed": 2}


def run(report_path):
    report_path = Path(report_path).resolve()
    report = {"ok": False, "checks": []}
    with tempfile.TemporaryDirectory(prefix="medexplain-ui-", ignore_cleanup_errors=True) as folder:
        os.environ["MEDEXPLAIN_DATABASE_PATH"] = str(Path(folder) / "isolated.db")
        os.environ["LOCALAPPDATA"] = folder
        os.environ.pop("MEDEXPLAIN_DEV", None)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --lang=zh-CN"
        from PySide6.QtWidgets import QApplication
        from desktop_app import MainWindow
        from build_metadata import build_info
        app = QApplication([])
        window = MainWindow()
        loaded = []
        window.view.loadFinished.connect(lambda ok: loaded.append(ok))
        window.show()
        def js(script):
            value = []
            window.view.page().runJavaScript(script, lambda result: value.append(result))
            end = time.monotonic() + 5
            while not value and time.monotonic() < end:
                app.processEvents(); time.sleep(.02)
            return value[0] if value else None
        try:
            deadline = time.monotonic() + 30
            while not loaded and time.monotonic() < deadline:
                app.processEvents(); time.sleep(.02)
            if not loaded or not loaded[-1]:
                raise RuntimeError("Initial frontend load failed")
            for route, expected in [("/", "工作台"), ("/import", "批量导入"), ("/questions", "题目管理"),
                                    ("/review", "审核"), ("/library", "教材库"), ("/tags", "标签"), ("/export", "导出")]:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    js("location.hash=" + json.dumps("#" + route))
                    body = js("document.body ? document.body.innerText : ''") or ""
                    if expected in body and js("Boolean(window.qt && window.QWebChannel)"):
                        break
                    app.processEvents(); time.sleep(.1)
                else:
                    raise RuntimeError(f"UI check failed: {route}")
                # Wait for fonts and bridge calls, then reflow before capturing glyphs.
                js("document.fonts.ready.then(()=>{document.body.style.display='none';void document.body.offsetHeight;document.body.style.display='';window.__smokeFonts=true})")
                settle_deadline = time.monotonic() + 10
                while time.monotonic() < settle_deadline:
                    ready = js("Boolean(window.__smokeFonts) && !Array.from(document.querySelectorAll('[role=status]')).some(x=>x.innerText.includes('处理中'))")
                    if ready:
                        break
                    app.processEvents(); time.sleep(.05)
                else:
                    raise RuntimeError(f"UI initialization did not settle: {route}")
                for _ in range(75):
                    app.processEvents(); time.sleep(.02)
                alerts = js("Array.from(document.querySelectorAll('[role=alert]')).map(x=>x.innerText).filter(Boolean)")
                if alerts:
                    raise RuntimeError(f"UI alerts: {route}: {alerts}")
                image = report_path.parent / ("ui-" + (route.strip("/") or "dashboard") + ".png")
                if not window.view.grab().save(str(image)):
                    raise RuntimeError("Could not save UI screenshot")
                report["checks"].append({"route": route, "expected": expected, "alerts": alerts or []})
            report["importRepair"] = check_import_repair(app, window, js, folder, report_path)
            report.update(ok=True, build=build_info())
        except Exception as exc:
            report["error"] = str(exc)
            report["pageUrl"] = window.view.url().toString()
            report["body"] = str(js("document.body ? document.body.innerText : ''"))[:500]
            raise
        finally:
            window.runner.shutdown()
            window.close()
            app.processEvents()
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0
