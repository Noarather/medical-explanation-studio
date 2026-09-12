"""In-memory single-file repair review. Source files and existing questions are never changed."""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from importers import (load_excel, normalize_question, parse_json_questions,
                       recover_trailing_option, validate_questions)


class FileImportReview:
    def __init__(self, path, name, rows, originals=None):
        self.id = uuid.uuid4().hex
        self.path, self.name = str(Path(path).resolve()), name
        self.source_type = "json" if Path(path).suffix.lower() == ".json" else "excel"
        self.originals = copy.deepcopy(originals if originals is not None else rows)
        self.rows = copy.deepcopy(rows)
        self.removed, self.visible = set(), set()
        self.result = None
        self._check()

    @classmethod
    def from_file(cls, path, name, mapping=None):
        source = Path(path)
        if source.suffix.lower() == ".json":
            with source.open(encoding="utf-8-sig") as handle:
                value = json.load(handle)
            rows = parse_json_questions(value, source, validate=False)
            originals = rows
            if isinstance(value, list) and value and all(isinstance(ch, dict) and "章节" in ch and "题目" in ch for ch in value):
                originals = [{"章节": ch["章节"], "章节内题序": number, "原题": question}
                             for ch in value if isinstance(ch["题目"], list)
                             for number, question in enumerate(ch["题目"], 1) if isinstance(question, dict)]
            return cls(path, name, rows, originals)
        if source.suffix.lower() in {".xlsx", ".xlsm"}:
            return cls(path, name, load_excel(source, mapping or {}, validate=False))
        raise ValueError("bad_payload: 仅支持 JSON、XLSX 和 XLSM 文件")

    def _check(self):
        self.errors = {}
        seen = {}
        self.repaired = set()
        for index, row in enumerate(self.rows):
            if index in self.removed:
                continue
            if not isinstance(row, dict):
                self.errors[index] = [f"第 {index + 1} 条不是题目对象，请补全或删除本次导入项"]
                self.visible.add(index)
                continue
            row = self.rows[index] = recover_trailing_option(row)
            normalized, errors = normalize_question(row, index + 1)
            if normalized.get("extensions", {}).get("importRepairs"):
                self.repaired.add(index)
                self.visible.add(index)
            external_id = normalized["id"]
            if external_id and external_id in seen:
                errors.append(f"第 {index + 1} 条 ID 与第 {seen[external_id] + 1} 条重复：{external_id}")
            elif external_id:
                seen[external_id] = index
            if errors:
                self.errors[index] = errors
                self.visible.add(index)

    def snapshot(self):
        return copy.deepcopy({
            "draft_id": self.id, "name": self.name, "path": self.path,
            "total": len(self.rows), "ready": len(self.rows) - len(self.removed) - len(self.errors),
            "issues": len(self.errors), "removed": len(self.removed), "repaired": len(self.repaired),
            "items": [{"index": index, "number": index + 1, "row": self.rows[index] if isinstance(self.rows[index], dict) else {},
                       "original": self.originals[index], "errors": self.errors.get(index, []),
                       "removed": index in self.removed, "repaired": index in self.repaired}
                      for index in sorted(self.visible)],
        })

    def resolve(self, action, index, row=None):
        if self.result is not None:
            raise ValueError("bad_state: 本次导入已经完成，请勿重复编辑")
        if type(index) is not int or not 0 <= index < len(self.rows):
            raise ValueError("bad_payload: 题目序号无效")
        if action == "edit":
            if not isinstance(row, dict):
                raise ValueError("bad_payload: 题目必须是对象")
            self.rows[index] = copy.deepcopy(row)
            self.removed.discard(index)
        elif action == "remove":
            self.removed.add(index)
        elif action == "restore":
            self.removed.discard(index)
        else:
            raise ValueError("bad_payload: 不支持的题目处理方式")
        self.visible.add(index)
        self._check()
        return self.snapshot()

    def commit(self, database):
        if self.result is not None:
            return dict(self.result)
        self._check()
        if self.errors:
            raise ValueError("import_invalid: 请先修正或删除下方待处理题目，再确认导入")
        rows = [row for index, row in enumerate(self.rows) if index not in self.removed]
        if not rows:
            raise ValueError("bad_state: 没有保留的题目，无需导入")
        # Final full validation and one atomic insert; never import a partial set by accident.
        validated = validate_questions(rows)
        set_id = database.create_question_set(self.name, self.path, self.source_type, validated)
        self.result = {"set_id": set_id, "name": self.name, "question_count": len(validated),
                       "repaired_count": len(self.repaired), "removed_count": len(self.removed)}
        return dict(self.result)
