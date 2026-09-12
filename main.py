"""Automation CLI sharing the same services and SQLite data as the desktop program."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import yaml

from app_paths import database_path
from credentials import CredentialStore
from db_manager import DatabaseManager


ROOT = Path(__file__).resolve().parent


def expand_env(value):
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.getenv(value[2:-1], "")
    return value


def load_config(path: str) -> dict:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        config = expand_env(yaml.safe_load(handle))
    configured = str(config["database"].get("path") or "").strip()
    if configured:
        candidate = Path(configured)
        config["database"]["path"] = str(candidate if candidate.is_absolute() else (config_path.parent / candidate).resolve())
    else:
        config["database"]["path"] = str(database_path())
    return config


def resolved_config(path: str, database_override: str | None = None) -> dict:
    config = load_config(path)
    config["database"]["path"] = str(database_override or database_path())
    store = CredentialStore()
    config["deepseek"]["api_key"] = store.get("deepseek") or config["deepseek"].get("api_key", "")
    config["dashscope"]["api_key"] = store.get("dashscope") or config["dashscope"].get("api_key", "")
    with DatabaseManager(config["database"]["path"]) as database:
        for setting, section, key in (
            ("deepseek_model", "deepseek", "model"), ("deepseek_base_url", "deepseek", "base_url"),
            ("embedding_model", "dashscope", "model"), ("ocr_model", "dashscope", "ocr_model"),
            ("dashscope_base_url", "dashscope", "base_url"),
        ):
            config[section][key] = database.get_setting(setting, config[section][key])
        config["retrieval"]["similarity_threshold"] = float(database.get_setting("similarity_threshold", config["retrieval"]["similarity_threshold"]))
        rerank = config["retrieval"].setdefault("rerank", {})
        enabled = str(database.get_setting("rerank_enabled", rerank.get("enabled", True))).strip().lower()
        rerank["enabled"] = enabled in {"1", "true", "yes", "on"}
        rerank["model"] = str(database.get_setting("rerank_model", rerank.get("model", "qwen3-rerank")))
        rerank["candidate_count"] = min(100, max(1, int(database.get_setting(
            "rerank_candidate_count", rerank.get("candidate_count", 20)
        ))))
        rerank["timeout_seconds"] = min(120, max(1, int(database.get_setting(
            "rerank_timeout_seconds", rerank.get("timeout_seconds", 30)
        ))))
        for setting, key in (("rerank_calibrated_alpha", "calibrated_alpha"), ("rerank_calibrated_threshold", "calibrated_threshold")):
            stored = str(database.get_setting(setting, "")).strip()
            rerank[key] = float(stored) if stored else rerank.get(key)
        config["memory_guard"]["batch_process_size"] = int(database.get_setting("batch_process_size", config["memory_guard"]["batch_process_size"]))
        generation = config.setdefault("generation", {})
        generation["concurrency"] = min(16, max(1, int(database.get_setting(
            "generation_concurrency", generation.get("concurrency", 8)
        ))))
        adaptive = str(database.get_setting(
            "generation_adaptive_concurrency", generation.get("adaptive_concurrency", True)
        )).strip().lower()
        generation["adaptive_concurrency"] = adaptive in {"1", "true", "yes", "on"}
        for setting, key, default in (
            ("generation_smart_routing", "smart_routing", True),
            ("generation_hard_use_pro", "hard_use_pro", True),
            ("generation_force_flash", "force_flash", False),
        ):
            value = str(database.get_setting(setting, generation.get(key, default))).strip().lower()
            generation[key] = value in {"1", "true", "yes", "on"}
        generation["hard_model"] = database.get_setting(
            "generation_hard_model", generation.get("hard_model", "deepseek-v4-pro")
        )
        generation["pro_concurrency"] = min(8, max(1, int(database.get_setting(
            "generation_pro_concurrency", generation.get("pro_concurrency", 2)
        ))))
        fallback = str(database.get_setting(
            "automatic_general_fallback", config["retrieval"].get("generate_fallback", False)
        )).strip().lower()
        config["retrieval"]["generate_fallback"] = fallback in {"1", "true", "yes", "on"}
        from model_config import MODEL_SETTINGS_DEFAULTS, select_model
        model_settings = {key: database.get_setting(key, default) for key, default in MODEL_SETTINGS_DEFAULTS.items()}
        model_settings["generation_hard_model"] = generation["hard_model"]
        config["llm"] = select_model(model_settings, store, config["deepseek"])
        if config["llm"]["provider"] != "deepseek":
            generation["hard_model"] = config["llm"]["hard_model"] or config["llm"]["model"]
            generation["hard_use_pro"] = generation["hard_use_pro"] and bool(config["llm"]["hard_model"])
    return config


def embedding_client(config: dict):
    from embedding_client import EmbeddingClient
    section = config["dashscope"]
    return EmbeddingClient(section["api_key"], section["model"], section["dimension"], section["batch_size"], section.get("base_url", ""))


def llm_client(config: dict):
    from llm_client import LLMClient
    section = config.get("llm", config["deepseek"])
    return LLMClient(
        section["api_key"], section["base_url"], section["model"],
        section["temperature"], section["max_tokens"],
        config.get("generation", {}).get("request_timeout_seconds", 180),
        provider=section.get("provider", "deepseek"), protocol=section.get("protocol", "openai"),
    )


def command_library_add(args, config: dict) -> None:
    with DatabaseManager(config["database"]["path"]) as database:
        library_id = database.add_library(
            args.name, args.subject, args.path, args.version, args.page_offset
        )
    print(json.dumps({"library_id": library_id}, ensure_ascii=False))


def command_scan(args, config: dict) -> None:
    from ocr_client import QwenOCRClient
    from services import TextbookService
    section = config["dashscope"]
    ocr = QwenOCRClient(section["api_key"], section["ocr_model"], section.get("base_url", ""))
    with DatabaseManager(config["database"]["path"]) as database:
        result = TextbookService(database, config, embedding_client(config), ocr).scan_library(args.library_id, args.force_ocr)
    print(json.dumps(result, ensure_ascii=False))


def command_import(args, config: dict) -> None:
    from importers import auto_mapping, excel_headers
    from services import QuestionImportService
    path = Path(args.questions)
    mapping = auto_mapping(excel_headers(path)) if path.suffix.lower() in {".xlsx", ".xlsm"} else None
    with DatabaseManager(config["database"]["path"]) as database:
        set_id = QuestionImportService(database).import_file(path, args.name or path.stem, mapping)
    print(json.dumps({"set_id": set_id}, ensure_ascii=False))


def command_generate(args, config: dict) -> None:
    from services import GenerationService
    selected = {item.strip() for item in str(getattr(args, "only", "") or "").split(",") if item.strip()} or None
    with DatabaseManager(config["database"]["path"]) as database:
        result = GenerationService(database, config, embedding_client(config), llm_client(config)).generate_set(args.set_id, content_types=selected)
    print(json.dumps(result, ensure_ascii=False))


def command_study_points(args, config: dict) -> None:
    from services import StudyPointBackfillService
    with DatabaseManager(config["database"]["path"]) as database:
        result = StudyPointBackfillService(database, llm_client(config)).run(args.set_id)
    print(json.dumps(result, ensure_ascii=False))

def command_memory_cards(args, config: dict) -> None:
    from services import MemoryCardBackfillService
    with DatabaseManager(config["database"]["path"]) as database:
        result = MemoryCardBackfillService(database, llm_client(config)).run(args.set_id)
    print(json.dumps(result, ensure_ascii=False))


def command_export(args, config: dict) -> None:
    from services import ExportService
    with DatabaseManager(config["database"]["path"]) as database:
        paths = ExportService(database).export(
            args.set_id, args.output_dir, split_by_subject=bool(getattr(args, "split_by_subject", False))
        )
    if "files" in paths:
        for path in paths["files"]:
            print(path)
        print(paths["mapping"])
    else:
        print(json.dumps(paths, ensure_ascii=False, indent=2))


def command_report(args, config: dict) -> None:
    with DatabaseManager(config["database"]["path"]) as database:
        rows = database.list_imported_questions(args.set_id, limit=100000)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["pipeline_status"]] = counts.get(row["pipeline_status"], 0) + 1
    print(json.dumps(counts, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="医学题库智能解析独立程序")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("library-add", help="添加单本 PDF 教材")
    add.add_argument("--name", required=True)
    add.add_argument("--subject", required=True)
    add.add_argument("--path", required=True)
    add.add_argument("--version", default="")
    add.add_argument(
        "--page-offset", type=int, default=0,
        help="PDF物理页减去课本印刷页的差值，例如527对应488时填39",
    )
    scan = sub.add_parser("scan", help="增量扫描教材并建立索引")
    scan.add_argument("--library-id", type=int, required=True)
    scan.add_argument("--force-ocr", action="store_true")
    importing = sub.add_parser("import", help="导入 JSON 或 Excel 题目")
    importing.add_argument("--questions", required=True)
    importing.add_argument("--name", default="")
    generate = sub.add_parser("generate", help="为题目集生成教材解析")
    generate.add_argument("--set-id", required=True)
    generate.add_argument("--only", default="", help="逗号分隔：explanation,tags,studyPoints")
    study_points = sub.add_parser("study-points", help="为题目集补齐复习考点")
    study_points.add_argument("--set-id", required=True)
    memory_cards = sub.add_parser("memory-cards", help="为题目集额外生成独立背诵知识卡")
    memory_cards.add_argument("--set-id", required=True)
    export = sub.add_parser("export", help="导出已审核解析")
    export.add_argument("--set-id", required=True)
    export.add_argument("--output-dir", required=True)
    export.add_argument("--split-by-subject", action="store_true")
    report = sub.add_parser("report", help="输出题目集处理状态")
    report.add_argument("--set-id", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = resolved_config(args.config)
    handlers = {
        "library-add": command_library_add, "scan": command_scan, "import": command_import,
        "generate": command_generate, "study-points": command_study_points, "memory-cards": command_memory_cards,
        "export": command_export, "report": command_report,
    }
    handlers[args.command](args, config)


if __name__ == "__main__":
    main()
