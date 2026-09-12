import copy
import json

import pytest

from db_manager import DatabaseManager
from file_import_review import FileImportReview
from importers import normalize_question, recover_trailing_option


def question(**changes):
    return {"id": "synthetic-1", "subject": "测试学科", "type": "A1", "question": "以下哪项符合条件",
            "options": ["A. 甲", "B. 乙", "D. 丁", "E. 戊"], "answer": "D", **changes}


@pytest.mark.parametrize("missing", list("ABCDE"))
def test_recovers_each_missing_label_without_mutation(missing):
    row = question(question=f"以下哪项符合条件 {missing}待恢复内容",
                   options=[f"{label}. 已有内容" for label in "ABCDE" if label != missing])
    before = copy.deepcopy(row)
    fixed, errors = normalize_question(row, 1)
    assert not errors
    assert row == before
    assert fixed["question"] == "以下哪项符合条件"
    assert fixed["options"][ord(missing) - 65] == f"{missing}. 待恢复内容"
    assert fixed["answer"] == "D"
    assert fixed["extensions"]["importRepairs"][0]["originalQuestion"] == row["question"]
    assert normalize_question(fixed)[0] == fixed


@pytest.mark.parametrize("changes", [
    {"question": "以下哪项符合条件C无空格边界"},
    {"question": "以下哪项符合条件 C-reactive protein"},
    {"question": "以下哪项符合条件 C测试？还有问题"},
    {"question": "以下哪项符合条件 C测试 E另一个选项"},
    {"question": "以下哪项 C第一次 C第二次"},
    {"question": "以下哪项符合条件 C内容", "options": ["A. 甲", "D. 丁", "E. 戊"]},
    {"question": "以下哪项符合条件 C内容", "options": ["A. 甲", "A. 乙", "D. 丁", "E. 戊"]},
    {"question": "以下哪项符合条件 C内容", "options": ["甲", "乙", "丁", "戊"]},
])
def test_ambiguous_sources_not_rewritten(changes):
    row = question(**changes)
    assert recover_trailing_option(row) == row


def test_missing_and_blank_options_are_actionable():
    assert any("缺少选项：C" in text for text in normalize_question(question())[1])
    row = question(options=["A. 甲", "B. 乙", "C. ", "D. 丁", "E. 戊"])
    assert any("选项内容为空：C" in text for text in normalize_question(row)[1])


def test_review_preserves_exact_chapter_source_and_does_not_change_file(tmp_path):
    original = {"ID": "s1", "题型": "选择题", "题干": "请选择合适的选项 C原有文字",
                "选项": [{"标号": label, "内容": "测试"} for label in "ABDE"], "答案": "D", "解析": "原始解析"}
    source = tmp_path / "合成学科_按章节.json"
    value = [{"章节": "合成章节", "题目": [original]}]
    source.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    before = source.read_bytes()
    review = FileImportReview.from_file(source, "测试")
    snapshot = review.snapshot()
    assert (snapshot["repaired"], snapshot["issues"], snapshot["ready"]) == (1, 0, 1)
    assert snapshot["items"][0]["original"]["原题"] == original
    with DatabaseManager(tmp_path / "isolated.db") as db:
        result = review.commit(db)
        assert review.commit(db) == result
        assert len(db.list_question_sets()) == 1
    assert source.read_bytes() == before


def test_remove_restore_edit_and_atomic_commit(tmp_path):
    rows = [question(), question(id="s2", question="以下哪项符合条件 C可恢复")]
    review = FileImportReview(tmp_path / "source.json", "测试", rows)
    with DatabaseManager(tmp_path / "isolated.db") as db:
        with pytest.raises(ValueError, match="请先修正"):
            review.commit(db)
        assert not db.list_question_sets()
        assert review.resolve("remove", 0)["removed"] == 1
        assert review.resolve("restore", 0)["issues"] == 1
        fixed = question(options=[f"{label}. 合成选项" for label in "ABCDE"])
        snapshot = review.resolve("edit", 0, fixed)
        assert (snapshot["issues"], snapshot["ready"]) == (0, 2)
        assert snapshot["items"][0]["original"] == rows[0]
        assert review.commit(db)["question_count"] == 2
        with pytest.raises(ValueError, match="已经完成"):
            review.resolve("remove", 0)


def test_removal_excludes_only_this_draft_and_can_commit_remaining(tmp_path):
    review = FileImportReview(tmp_path / "a.json", "新题集", [question(), question(id="q2", options=["A.甲", "B.乙"], answer="A")])
    with DatabaseManager(tmp_path / "isolated.db") as db:
        db.create_question_set("已存在", "old.json", "json", [question(id="old")])
        review.resolve("remove", 0)
        result = review.commit(db)
        assert result["question_count"] == result["removed_count"] == 1
        assert {row["name"] for row in db.list_question_sets()} == {"已存在", "新题集"}


def test_invalid_values_do_not_disappear_on_recheck(tmp_path):
    review = FileImportReview(tmp_path / "a.json", "测试", [question(knowledgePoints=["一", "二", "三", "四"]), "not-an-object"])
    assert review.snapshot()["issues"] == 2
    result = review.resolve("remove", 1)
    assert any("知识点超过" in text for text in result["items"][0]["errors"])
    review.resolve("remove", 0)
    with DatabaseManager(tmp_path / "isolated.db") as db:
        with pytest.raises(ValueError, match="没有保留"):
            review.commit(db)


def test_duplicate_id_rechecked_after_removal_and_invalid_index_rejected(tmp_path):
    row = question(options=["A.甲", "B.乙"], answer="A")
    review = FileImportReview(tmp_path / "a.json", "测试", [row, row])
    assert review.snapshot()["issues"] == 1
    assert review.resolve("remove", 0)["issues"] == 0
    for index in [-1, 2, True, "0"]:
        with pytest.raises(ValueError, match="序号无效"):
            review.resolve("remove", index)


def test_bridge_review_contract_and_stale_token(tmp_path):
    from ui_bridge.imports import ImportsBridge
    source = tmp_path / "a.json"
    source.write_text(json.dumps([question()]), encoding="utf-8")
    bridge = ImportsBridge(str(tmp_path / "db.sqlite"))
    result = bridge.api_import_file(str(source), "合成测试")
    assert result["needs_review"]
    token = result["review"]["draft_id"]
    with pytest.raises(ValueError, match="失效"):
        bridge.api_commit_file_import("stale")
    fixed = question(options=[f"{label}. 合成" for label in "ABCDE"])
    assert bridge.api_resolve_file_import(token, "edit", 0, fixed)["issues"] == 0
    assert bridge.api_commit_file_import(token)["question_count"] == 1
