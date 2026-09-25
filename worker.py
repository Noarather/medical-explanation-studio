"""Persistent single-job worker invoked by the desktop program in a separate process."""

from __future__ import annotations

import sys
import json

from db_manager import DatabaseManager
from embedding_client import EmbeddingClient
from llm_client import LLMClient
from ocr_client import QwenOCRClient
from runtime_config import runtime_config
from memory_guard import MemoryLimitExceeded
from services import (
    CancelledError, GenerationService, JobControl, MemoryCardBackfillService, StudyPointBackfillService,
    TagBackfillService, TextbookService, V2UpgradeService,
)


def _embedding(config: dict) -> EmbeddingClient:
    section = config["dashscope"]
    return EmbeddingClient(
        section["api_key"], section["model"], section["dimension"],
        section["batch_size"], section.get("base_url", ""),
    )


def _llm(config: dict) -> LLMClient:
    from main import llm_client
    return llm_client(config)


def run_job(job_id: str) -> int:
    config = runtime_config()
    with DatabaseManager(config["database"]["path"]) as database:
        job = database.get_job(job_id)
        if not job:
            return 2
        control = JobControl(database, job_id)
        database.update_job(job_id, status="running", message="正在启动")
        database.add_job_event(job_id, "任务开始")
        try:
            completion_message = "已完成"
            if job["job_type"] == "scan":
                embedding = _embedding(config)
                dash = config["dashscope"]
                ocr = QwenOCRClient(
                    dash["api_key"], dash.get("ocr_model", "qwen-vl-ocr"), dash.get("base_url", ""),
                )
                result = TextbookService(database, config, embedding, ocr).scan_library(
                    int(job["payload"]["library_id"]), bool(job["payload"].get("force_ocr")), control,
                )
                completion_message = result.get("message", completion_message)
            elif job["job_type"] == "generate":
                result = GenerationService(database, config, _embedding(config), _llm(config)).generate_set(
                    job["payload"]["set_id"], control, job["payload"].get("question_ids"),
                    job["payload"].get("library_ids"),
                    bool(job["payload"].get("reuse_saved_evidence", False)),
                    content_types=set(job["payload"]["content_types"]) if "content_types" in job["payload"] else None,
                )
                completion_message = (
                    f"生成 {result['generated']}，未匹配/缺教材 {result['unmatched']}，失败 {result['failed']}，"
                    f"峰值并发 {result['peak_concurrency']}"
                )
                if result.get("auto_general_generated"):
                    completion_message += f"，其中自动通识 {result['auto_general_generated']}"
                routes = result.get("route_counts") or {}
                if routes:
                    completion_message += (
                        f"；路由 Flash {routes.get('flash', 0)} / Flash思考 {routes.get('flash_thinking', 0)}"
                        f" / Pro思考 {routes.get('pro_thinking', 0)}"
                    )
                subject_routes = result.get("subject_routes") or {}
                if subject_routes:
                    route_text = "、".join(
                        f"{label} {count}题" for label, count in subject_routes.items()
                    )
                    completion_message += (
                        f"；学科路由 {route_text}，实际检索 {result.get('searched_library_count', 0)} 本教材"
                    )
                perf = result.get("performance") or {}
                if perf:
                    completion_message += (
                        f"；{perf.get('questions_per_minute', 0)} 题/分钟，"
                        f"P50/P95 {perf.get('p50_ms', 0)}/{perf.get('p95_ms', 0)} ms，"
                        f"结构失败率 {float(perf.get('structured_failure_rate', 0)) * 100:.1f}%"
                    )
            elif job["job_type"] == "general":
                control.progress(0, 1, "生成通识解析")
                GenerationService(database, config, None, _llm(config)).generate_general(
                    int(job["payload"]["question_pk"])
                )
                control.progress(1, 1, "生成完成")
            elif job["job_type"] == "general_batch":
                result = GenerationService(database, config, None, _llm(config)).generate_general_batch(
                    job["payload"]["set_id"], control, job["payload"].get("question_ids"),
                )
                completion_message = (
                    f"通识生成 {result['generated']}，失败 {result['failed']}，"
                    f"峰值并发 {result['peak_concurrency']}"
                )
            elif job["job_type"] == "tag_backfill":
                result = TagBackfillService(database, _llm(config)).run(
                    job["payload"]["set_id"], control,
                    bool(job["payload"].get("include_approved", True)),
                )
                completion_message = (
                    f"标签补齐 {result['updated']}，失败 {result['failed']}，"
                    f"需重新生成 {result['requires_regeneration']}"
                )
            elif job["job_type"] == "study_point_backfill":
                result = StudyPointBackfillService(database, _llm(config)).run(
                    job["payload"]["set_id"], control,
                    bool(job["payload"].get("include_approved", True)),
                )
                completion_message = (
                    f"考点补齐 {result['updated']}，失败 {result['failed']}"
                )
            elif job["job_type"] == "memory_card_backfill":
                result = MemoryCardBackfillService(database, _llm(config)).run(
                    job["payload"]["set_id"], control,
                    bool(job["payload"].get("include_approved", True)),
                )
                completion_message = (
                    f"独立背诵知识卡生成 {result['updated']}，失败 {result['failed']}"
                )
            elif job["job_type"] == "upgrade_v2":
                result=V2UpgradeService(database,config,_llm(config)).run(job["payload"]["set_id"],control)
                completion_message=f"v2 升级 {result['updated']}，跳过 {result['skipped']}，失败 {result['failed']}"
            else:
                raise ValueError(f"未知任务类型：{job['job_type']}")
            if job["job_type"] != "scan":
                from auto_review import run as auto_review
                scope = job["payload"].get("set_id", "")
                if job["payload"].get("question_pk"):
                    scope = (database.get_imported_question(int(job["payload"]["question_pk"])) or {}).get("set_id", scope)
                summary = auto_review(database, scope)
                completion_message += f"；自动通过 {summary['approved']}，待复核 {summary['pending']}"
            database.update_job(job_id, status="completed", message=completion_message)
            database.add_job_event(job_id, "任务完成")
            return 0
        except MemoryLimitExceeded as exc:
            # Native inference allocators can retain RSS after models are freed.
            # A fresh worker releases that memory; the durable stage avoids
            # replaying parsed pages or committed, paid embedding batches.
            current = database.get_job(job_id)
            payload = dict(current.get("payload") or {})
            point = [exc.stage, int(exc.page_range[0]) if exc.page_range else current["progress_current"]]
            if job["job_type"] == "scan" and payload.get("_memory_restart_point") != point:
                payload["_memory_restart_point"] = point
                with database.conn:
                    database.conn.execute("UPDATE jobs SET payload_json=? WHERE id=?",
                                          (json.dumps(payload, ensure_ascii=False), job_id))
                database.update_job(job_id, status="queued", message="正在释放解析进程内存，将从已保存断点继续")
                database.add_job_event(job_id, f"内存回收：退出工作进程后从断点继续；{exc}", "warning")
                return 75
            database.update_job(job_id, status="failed", message="同一断点重启后仍超出内存上限", error=str(exc))
            database.add_job_event(job_id, str(exc), "error")
            return 1
        except CancelledError:
            database.update_job(job_id, status="cancelled", message="已取消")
            database.add_job_event(job_id, "任务取消", "warning")
            return 0
        except Exception as exc:
            database.update_job(job_id, status="failed", message="执行失败", error=str(exc))
            database.add_job_event(job_id, str(exc), "error")
            return 1


if __name__ == "__main__":
    raise SystemExit(run_job(sys.argv[1]))
