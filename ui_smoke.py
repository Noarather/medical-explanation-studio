"""Offline installed-app UI probe using a fresh database; never opens business data."""
import json
import os
import tempfile
import time
from pathlib import Path


def check_ocr_review_entry(app, window, js):
    """Open the real review UI, exercise its history bridge, never call paid start."""
    js("location.hash='#/library'")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if js("(()=>{const b=Array.from(document.querySelectorAll('button')).find(b=>b.innerText.trim()==='高级 OCR 复核');if(!b)return false;b.click();return true})()"):
            break
        app.processEvents(); time.sleep(.05)
    else:
        raise RuntimeError("Missing advanced OCR review entry")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if js("(()=>{const p=document.querySelector('[aria-label=\"单页高级 OCR 复核\"]');return p && p.querySelector('select') && Array.from(p.querySelectorAll('button')).some(b=>b.innerText==='开始单页复核'&&b.disabled)})()"):
            break
        app.processEvents(); time.sleep(.05)
    else:
        raise RuntimeError("Advanced OCR consent gate missing")
    js("Array.from(document.querySelector('[aria-label=\"单页高级 OCR 复核\"]').querySelectorAll('button')).find(b=>b.innerText==='关闭').click()")
    return {"entry": True, "consentRequired": True, "live_api": False}


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
    js("document.querySelector('[aria-label=导入题目即时处理]').scrollIntoView({block:'start'})")
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


def check_textbook_import(app, window, js, folder, report_path):
    """Actual batch picker, preview editor, commit and recalibration on synthetic PDFs."""
    import fitz
    from PySide6.QtWidgets import QFileDialog
    from db_manager import DatabaseManager
    files = []
    for subject in ("病理生理学", "外科学"):
        path = Path(folder) / f"{subject}（第10版）.pdf"
        with fitz.open() as doc:
            for number in (8, 9, 1, 2, 3, 4, 1, 2):
                page = doc.new_page()
                page.insert_text((60, 100), "Synthetic textbook body for offline UI verification")
                page.insert_text((60, 820), str(number))
            doc.save(path)
        files.append(str(path))
    broken = Path(folder) / "broken.pdf"
    broken.write_bytes(b"synthetic broken PDF")
    files.append(str(broken))

    def until(condition):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if js(condition):
                return
            app.processEvents(); time.sleep(.05)
        raise RuntimeError("Textbook UI check timed out: " + condition)

    def click(text):
        if not js("(()=>{const b=Array.from(document.querySelectorAll('button')).find(e=>e.innerText.trim()==="
                  + json.dumps(text) + ");if(!b||b.disabled)return false;b.click();return true})()"):
            raise RuntimeError("Missing/enabled textbook button: " + text)

    js("location.hash='#/library'")
    until("document.body.innerText.includes('批量导入教材')")
    click("批量导入教材")
    old_picker = QFileDialog.getOpenFileNames
    try:
        QFileDialog.getOpenFileNames = staticmethod(lambda *a, **kw: (files, "PDF"))
        click("选择 PDF（支持多选）")
        until("document.querySelectorAll('[aria-label=教材批量导入与页码校准] article').length === 3")
    finally:
        QFileDialog.getOpenFileNames = old_picker
    if not js("document.body.innerText.includes('PDF 3–6 页 → 课本 1–4 页') && document.body.innerText.includes('本条不导入')"):
        raise RuntimeError("Missing multi-section preview or per-file error")
    js("(()=>{const t=document.querySelector('[aria-label=教材1名称]');t.value='合成病理教材';t.dispatchEvent(new Event('input',{bubbles:true}))})()")
    for _ in range(50):
        app.processEvents(); time.sleep(.02)
    if not window.view.grab().save(str(report_path.parent / "ui-textbook-batch.png")):
        raise RuntimeError("Could not save textbook screenshot")
    click("确认导入 2 本")
    until("document.body.innerText.includes('成功导入 2 本')")
    click("关闭")
    until("document.body.innerText.includes('合成病理教材')")
    click("自动校准页码")
    until("Boolean(Array.from(document.querySelectorAll('button')).find(b=>b.innerText==='确认应用校准'&&!b.disabled))")
    click("确认应用校准")
    until("document.body.innerText.includes('校准已完成')")
    click("关闭")
    with DatabaseManager(window.database_path) as database:
        rows = database.list_libraries()
        if len(rows) != 2 or not any(r["name"] == "合成病理教材" for r in rows):
            raise RuntimeError("Batch import did not persist edited metadata")
        mappings = database.conn.execute("SELECT COUNT(*) FROM library_page_map").fetchone()[0]
        if mappings != 16:
            raise RuntimeError("Printed-page mappings were not persisted")
        if database.conn.execute("SELECT COUNT(*) FROM jobs WHERE job_type='scan'").fetchone()[0]:
            raise RuntimeError("Batch preview/import unexpectedly queued indexing")
    return {"selected":3,"imported":2,"invalidSkipped":1,"editedMetadata":True,
            "mappedPages":mappings,"recalibrated":True,"cloudCalls":0}


def check_index_refresh(app, window, js, report_path):
    """Exercise the real jobs signal and library reload, with synthetic index rows.

    No queued worker is created and no embedding provider is called.
    """
    import hashlib
    from db_manager import DatabaseManager
    from index_profile import build_index_profile, index_fingerprint
    from runtime_config import runtime_config

    def until(condition):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if js(condition):
                return
            app.processEvents(); time.sleep(.05)
        raise RuntimeError("Index refresh check timed out: " + condition)

    with DatabaseManager(window.database_path) as database:
        library = next(row for row in database.list_libraries() if row["name"] == "合成病理教材")
    library_id = library["id"]
    selector = f'[data-library-id="{library_id}"]'
    js("window.__indexCard=()=>document.querySelector(" + json.dumps(selector) + ")")
    until("window.__indexCard()?.innerText.includes('尚未建立索引')")
    job = {"id": "offline-index-refresh", "job_type": "scan", "title": "合成索引状态",
           "status": "running", "progress_current": 0, "progress_total": 8,
           "message": "离线验收：模拟状态事件", "payload_json": json.dumps({"library_id": library_id})}
    # Pause only this isolated test window's empty scheduler while injecting
    # deterministic events; never start a real queued task in the probe.
    timer_active = window.runner._timer.isActive()
    window.runner._timer.stop()
    try:
        window.runner.jobs_changed.emit(json.dumps({"jobs": [job]}, ensure_ascii=False))
        until("window.__indexCard()?.innerText.includes('建立索引中')")
        profile = build_index_profile(runtime_config())
        path = Path(library["root_path"])
        stat = path.stat()
        with DatabaseManager(window.database_path) as database:
            file_id = database.upsert_file_metadata(library_id, str(path), path.name, stat.st_size,
                stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            pages = [{"page_number": n, "text": "Synthetic offline index", "extraction_method": "docling", "error_message": ""} for n in range(1, 9)]
            chunks = [{"page_number": 1, "chunk_index": 0, "chunk_text": "Synthetic offline index",
                       "search_text": "synthetic offline index", "embedding": [1., 0.], "extraction_method": "docling"}]
            database.replace_file_content(file_id, library_id, pages, chunks)
            database.set_file_index_profile(file_id, profile, index_fingerprint(profile))
            database.finish_library_scan(library_id)
        job.update(status="completed", progress_current=8)
        window.runner.jobs_changed.emit(json.dumps({"jobs": [job]}, ensure_ascii=False))
        until("window.__indexCard()?.innerText.includes('索引可用') && !window.__indexCard()?.innerText.includes('尚未建立索引')")
        if not js("location.hash==='#/library' && window.__indexCard()?.innerText.includes('8 页')"):
            raise RuntimeError("Index completion did not update the current library card")
        for _ in range(25):
            app.processEvents(); time.sleep(.02)
        if not window.view.grab().save(str(report_path.parent / "ui-index-refreshed.png")):
            raise RuntimeError("Could not save refreshed index screenshot")
    finally:
        window.runner._emit_snapshot()
        if timer_active:
            window.runner._timer.start()
    return {"liveSignal": True, "runningShown": True, "completedReloaded": True,
            "navigationRequired": False, "syntheticIndex": True, "cloudCalls": 0}


def check_textbook_scroll(app, window, js, folder, report_path):
    """Native wheel, long-list paging and resize checks without forced body repaints."""
    import fitz
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication, QFileDialog
    from db_manager import DatabaseManager
    paths = []
    for index in range(25):
        path = Path(folder) / f"滚动合成教材-病理生理学第10版-{index+1:02d}.pdf"
        with fitz.open() as doc:
            for number in (8, 9, 1, 2, 3, 4, 1, 2):
                page = doc.new_page()
                page.insert_text((60, 100), "Synthetic scroll verification, no copyrighted content")
                page.insert_text((60, 820), str(number))
            doc.save(path)
        paths.append(str(path))

    def settle(seconds=.2):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            app.processEvents(); time.sleep(.01)

    def until(condition):
        end = time.monotonic()+40
        while time.monotonic() < end:
            if js(condition): return
            settle(.05)
        raise RuntimeError("Scroll check timed out: " + condition)

    def click(label):
        if not js("(()=>{const b=Array.from(document.querySelectorAll('button')).find(b=>b.innerText.trim()===" + json.dumps(label) + ");if(!b||b.disabled)return false;b.click();return true})()"):
            raise RuntimeError("Missing scroll test button: " + label)

    click("批量导入教材")
    old_picker = QFileDialog.getOpenFileNames
    try:
        QFileDialog.getOpenFileNames = staticmethod(lambda *a, **kw: (paths, "PDF"))
        click("选择 PDF（支持多选）")
        until("document.querySelectorAll('[role=dialog] article').length===10 && document.body.innerText.includes('已选 25 本')")
    finally:
        QFileDialog.getOpenFileNames = old_picker
    js("window.__textbookScroll=document.querySelector('[data-testid=textbook-scroll]')")
    captures = []
    for size in ((1080, 700), (1600, 960)):
        window.resize(*size)
        settle(.4)
        js("window.__textbookScroll.scrollTop=0")
        center = json.loads(js("JSON.stringify((()=>{const r=window.__textbookScroll.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})())"))
        local = QPointF(center["x"], center["y"])
        global_point = QPointF(window.view.mapToGlobal(QPoint(round(local.x()), round(local.y()))))
        target = window.view.focusProxy() or window.view
        for _ in range(6):
            event = QWheelEvent(local, global_point, QPoint(), QPoint(0,-120), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(target, event)
            settle(.05)
        until("window.__textbookScroll.scrollTop > 0")
        for position, fraction in enumerate((0, .5, 1, 0)):
            js(f"window.__textbookScroll.scrollTop=(window.__textbookScroll.scrollHeight-window.__textbookScroll.clientHeight)*{fraction}")
            settle(.3)
            layout = json.loads(js("""JSON.stringify((()=>{
                const d=document.querySelector('[role=dialog]'), h=d.querySelector('header'), f=d.querySelector('footer'), s=window.__textbookScroll;
                return {dialog:d.getBoundingClientRect().toJSON(),header:h.getBoundingClientRect().toJSON(),footer:f.getBoundingClientRect().toJSON(),scroll:s.getBoundingClientRect().toJSON(),
                    width:innerWidth,height:innerHeight,transform:getComputedStyle(d).transform,count:d.querySelectorAll('article').length};
            })())"""))
            d, h, f, s = (layout[k] for k in ("dialog","header","footer","scroll"))
            if (layout["transform"] != "none" or layout["count"] > 10 or d["top"] < 0 or d["bottom"] > layout["height"]+1
                    or h["bottom"] > s["top"]+1 or s["bottom"] > f["top"]+1 or f["bottom"] > d["bottom"]):
                raise RuntimeError("Modal scroll/header/footer bounds failed")
            capture = window.view.grab().toImage()
            scale = capture.width() / layout["width"]
            # Test actual composited pixels, not only DOM geometry. White dialog
            # padding must remain opaque after wheel scroll and resizing.
            for x, y in ((d["right"]-10, h["top"]+10), (d["left"]+10, f["bottom"]-10)):
                color = capture.pixelColor(round(x*scale), round(y*scale))
                if min(color.red(), color.green(), color.blue()) < 245:
                    raise RuntimeError("Modal background lost during scroll")
            name = f"ui-textbook-scroll-{size[0]}-{position}.png"
            if not capture.save(str(report_path.parent / name)):
                raise RuntimeError("Could not save scroll screenshot")
            captures.append(name)
    js("(()=>{const e=document.querySelector('[aria-label=教材1名称]');e.value='跨页保留编辑';e.dispatchEvent(new Event('input',{bubbles:true}))})()")
    click("下一页教材"); settle()
    click("下一页教材"); settle()
    until("document.querySelector('[aria-label=教材25版本]') !== null")
    js("(()=>{const e=document.querySelector('[aria-label=教材25版本]');e.value='尾页修改';e.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[aria-label=\\\"教材 24\\\"] input[type=checkbox]').click()})()")
    click("上一页教材"); settle()
    click("上一页教材"); settle()
    if js("document.querySelector('[aria-label=教材1名称]').value") != "跨页保留编辑":
        raise RuntimeError("Cross-page edit was lost")
    click("确认导入 24 本")
    until("document.body.innerText.includes('成功导入 24 本')")
    click("关闭")
    with DatabaseManager(window.database_path) as database:
        libraries = database.list_libraries()
        if len(libraries) != 26 or not any(r["version"] == "尾页修改" for r in libraries):
            raise RuntimeError("Cross-page selection/metadata not committed")
        if database.conn.execute("SELECT COUNT(*) FROM jobs WHERE job_type='scan'").fetchone()[0]:
            raise RuntimeError("Scroll test unexpectedly queued indexing")
    return {"previewed":25,"imported":24,"pageSize":10,"nativeWheel":True,
            "crossPageEdits":True,"screenshots":captures,"devicePixelRatio":window.devicePixelRatioF()}


def run(report_path, *, native_scroll=False):
    report_path = Path(report_path).resolve()
    report = {"ok": False, "checks": []}
    with tempfile.TemporaryDirectory(prefix="medexplain-ui-", ignore_cleanup_errors=True) as folder:
        os.environ["MEDEXPLAIN_DATABASE_PATH"] = str(Path(folder) / "isolated.db")
        os.environ["LOCALAPPDATA"] = folder
        os.environ.pop("MEDEXPLAIN_DEV", None)
        if native_scroll:
            os.environ["QT_QPA_PLATFORM"] = "windows"
        else:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        # Use the production renderer policy. A test-only GPU override used to
        # hide differences between regular startup and our acceptance run.
        from PySide6.QtWidgets import QApplication
        from desktop_app import MainWindow
        from build_metadata import build_info
        app = QApplication([])
        window = MainWindow()
        if native_scroll:
            from PySide6.QtCore import Qt
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
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
                # Wait for fonts without forcing a repaint that could hide corruption.
                js("document.fonts.ready.then(()=>{window.__smokeFonts=true})")
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
                if route == "/questions":
                    guarded = js("Boolean(document.querySelector('[data-testid=first-generation]')?.disabled)")
                    if not guarded:
                        raise RuntimeError("First generation must require an explicit batch")
                    report["firstGeneration"] = {"entryVisible": True, "requiresBatch": True, "live_api": False}
            report["importRepair"] = check_import_repair(app, window, js, folder, report_path)
            report["textbookBatch"] = check_textbook_import(app, window, js, folder, report_path)
            report["indexRefresh"] = check_index_refresh(app, window, js, report_path)
            report["ocrReview"] = check_ocr_review_entry(app, window, js)
            if native_scroll:
                report["textbookScroll"] = check_textbook_scroll(app, window, js, folder, report_path)
            report["rendering"] = {"qtQuickBackend":os.environ.get("QT_QUICK_BACKEND", "auto"),
                                   "chromiumGpuDisabled":"--disable-gpu" in os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()}
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
