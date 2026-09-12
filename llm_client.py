"""DeepSeek explanation generation using its OpenAI-compatible API.

Dependencies: openai.
"""

from __future__ import annotations

import json
import re
import threading
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from model_config import normalized_url, safe_error
from model_gateway import complete_native, GatewayError
from question_format_v2 import (
    blocks_to_markdown, normalize_blocks, normalize_evidence,
    normalize_knowledge_points, normalize_memory_cards, normalize_study_points, normalize_tags,
)
from structured_models import (
    ExplanationPackageResponse, MemoryCardBatchResponse, StudyPointBatchResponse,
    SubjectSelectionResponse, TagBatchResponse, QuestionOrganizeItem, instructor_schema,
)


class LLMRequestError(RuntimeError):
    """Normalized provider failure used by the generation rate controller."""

    def __init__(
        self, message: str, *, status_code: int = 0, retry_after: float = 0,
        retryable: bool = False, fatal_global: bool = False,
    ):
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.retry_after = max(0.0, float(retry_after or 0))
        self.retryable = bool(retryable)
        self.fatal_global = bool(fatal_global)


class ExplanationPackageError(ValueError):
    """The provider replied, but the content cannot become a reviewable explanation."""

    def __init__(self, message: str, *, code: str = "schema_validation_failed", partial_content: str = "", request_meta: dict | None = None):
        super().__init__(message)
        self.code = str(code or "schema_validation_failed")
        self.partial_content = str(partial_content or "")[:12000]
        self.request_meta = dict(request_meta or {})


def validate_explanation_package(package: dict, evidence: list[dict], mode: str) -> None:
    """Reject packages that would create a misleading normal-review item."""
    blocks = package.get("explanationBlocks") if isinstance(package, dict) else None
    if not isinstance(blocks, list) or not blocks:
        raise ExplanationPackageError("结构化解析区块为空")
    text = blocks_to_markdown(blocks).strip()
    if not text or "待人工整理" in text or "模型输出无法解析" in text:
        raise ExplanationPackageError("解析包含无效占位内容")
    sections = {str(block.get("section") or "") for block in blocks if isinstance(block, dict)}
    required = {"answerBasis", "pitfalls"}
    if mode != "general_knowledge":
        required.add("analysis")
    if not required.issubset(sections):
        raise ExplanationPackageError("解析缺少必要的考点、答案依据或易错点区块")
    if not any(
        isinstance(block, dict) and block.get("section") == "pitfalls" and block.get("type") == "table"
        for block in blocks
    ):
        raise ExplanationPackageError("易错点必须使用表格")
    if mode in {"textbook", "textbook_reasoning"} and not evidence:
        raise ExplanationPackageError("教材解析缺少教材证据")
    if not normalize_tags([*(package.get("tags") or []), *(package.get("suggestedTags") or [])]):
        raise ExplanationPackageError("题目标签为空")
    if not re.sub(r"\s+", " ", str(package.get("briefExplanation") or "")).strip():
        raise ExplanationPackageError("一句话简析为空")


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def _answer_focus(question: str) -> tuple[list[str], str]:
    """Extract the correct option text and the actual stem from a generation prompt."""
    text = str(question or "").replace("\r", "")
    answers = re.findall(r"正确答案\s*[：:]\s*([A-E]+)", text, flags=re.I)
    letters = set("".join(answers).upper())
    options: dict[str, str] = {}
    for match in re.finditer(r"(?m)^\s*([A-E])[.、．:]\s*(.+?)\s*$", text, flags=re.I):
        options[match.group(1).upper()] = match.group(2).strip()
    focus = [options[letter] for letter in "ABCDE" if letter in letters and options.get(letter)]
    stem_lines = []
    for line in text.splitlines():
        value = line.strip()
        if not value or re.match(r"^(?:题型\s*[：:]|[A-E][.、．:]|正确答案\s*[：:])", value, flags=re.I):
            continue
        stem_lines.append(value)
    return focus, " ".join(stem_lines[:3])


def _bigrams(value: str) -> set[str]:
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "").casefold())
    return {clean[index:index + 2] for index in range(max(0, len(clean) - 1))}


def evidence_sentence_score(question: str, sentence: str) -> float:
    """Score whether a sentence directly supports the keyed answer, not just the topic."""
    sentence = re.sub(r"\s+", " ", str(sentence or "")).strip()
    if not sentence:
        return 0.0
    focus, stem = _answer_focus(question)
    score = 0.0
    for answer in focus:
        if answer and answer in sentence:
            score += 120.0
        answer_units = _bigrams(answer)
        if answer_units:
            score += 90.0 * len(answer_units & _bigrams(sentence)) / len(answer_units)
    stem_terms = [
        value for value in re.split(r"[\s，。；：、？！“”‘’（）()]+", stem)
        if 2 <= len(value) <= 24
    ]
    score += sum(min(8, len(term)) for term in stem_terms if term in sentence)
    if re.search(r"(?:是研究|是指|定义|属于|称为)", stem) and re.search(r"(?:研究|是指|称为|即|定义)", sentence):
        score += 18.0
    if re.search(r"本章数字资源|目录|第一章|第二章|第三章", sentence) and not any(
        answer and answer in sentence for answer in focus
    ):
        score -= 12.0
    return score


def build_evidence_excerpt(
    question: str, evidence: dict, model_excerpt: str = "", model_highlights: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Return a verbatim, reviewable excerpt while retaining the full quote separately."""
    quote = re.sub(r"\s+", " ", str(evidence.get("quote") or evidence.get("text") or "")).strip()
    supplied = re.sub(r"\s+", " ", str(model_excerpt or "")).strip()
    if supplied and supplied not in quote:
        supplied = ""
    focus, _stem = _answer_focus(question)
    terms = []
    for value in focus:
        if value and value not in terms:
            terms.append(value)
    for value in re.split(r"[\s，。；：、？！“”‘’（）()]+", str(question or "")):
        value = re.sub(r"^[A-E][.、．:]?", "", value).strip()
        if 2 <= len(value) <= 24 and value not in terms:
            terms.append(value)
    sentences = [value.strip() for value in re.findall(r"[^。！？；]+[。！？；]?", quote) if value.strip()]
    if sentences:
        ranked = sorted(enumerate(sentences), key=lambda item: (-evidence_sentence_score(question, item[1]), item[0]))
        best_sentence = ranked[0][1].strip()
        if not supplied or evidence_sentence_score(question, best_sentence) > evidence_sentence_score(question, supplied):
            supplied = best_sentence[:240]
    if not supplied:
        supplied = quote[:240]
    highlights = []
    for value in [*(model_highlights or []), *terms]:
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if 2 <= len(value) <= 120 and value in supplied and value in quote and value not in highlights:
            highlights.append(value)
        if len(highlights) >= 8:
            break
    return supplied, highlights


def build_local_context_summary(question: str, evidence: dict, max_chars: int = 320) -> str:
    """Create a conservative readable context without changing the raw textbook quote."""
    quote = str(evidence.get("quote") or evidence.get("text") or "").replace("\r", "\n")
    lines = []
    for raw_line in quote.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" \t|·")
        if not line or re.fullmatch(r"(?:第?[一二三四五六七八九十百0-9]+[章节页]?|本章数字资源\s*\d*)", line):
            continue
        lines.append(line)
    clean = re.sub(r"\s+", " ", " ".join(lines) or quote).strip()
    if not clean:
        return ""
    excerpt, _ = build_evidence_excerpt(question, {**evidence, "quote": clean})
    sentences = [value.strip() for value in re.findall(r"[^。！？；]+[。！？；]?", clean) if value.strip()]
    if len(sentences) > 1:
        best = max(range(len(sentences)), key=lambda index: evidence_sentence_score(question, sentences[index]))
        chosen = sentences[max(0, best - 1):min(len(sentences), best + 2)]
        summary = "".join(chosen)
    else:
        position = clean.find(excerpt)
        if position < 0:
            summary = clean[:max_chars]
        else:
            start = max(0, position - 80)
            summary = clean[start:position + len(excerpt) + 160]
    return re.sub(r"\s+", " ", summary).strip()[:max_chars]


def grounded_context_summary(question: str, evidence: dict, proposed: str = "") -> str:
    """Accept an AI-cleaned context only when it remains grounded in the raw quote."""
    raw = re.sub(r"\s+", " ", str(evidence.get("quote") or evidence.get("text") or "")).strip()
    candidate = re.sub(r"\s+", " ", str(proposed or "")).strip()[:500]
    if candidate and raw:
        candidate_units = _bigrams(candidate)
        raw_units = _bigrams(raw)
        coverage = len(candidate_units & raw_units) / max(1, len(candidate_units))
        numbers_are_grounded = all(token in raw for token in re.findall(r"\d+(?:\.\d+)?%?", candidate))
        if len(candidate) >= 20 and coverage >= 0.55 and numbers_are_grounded:
            return candidate
    return build_local_context_summary(question, evidence)


def select_best_evidence(question: str, evidence: list[dict]) -> list[dict]:
    """Keep the best answer-supporting page from each useful textbook.

    Multiple pages from one book add noise, while collapsing every textbook to a
    single global page defeats explicit multi-textbook generation.  The strongest
    page is always retained; additional books must remain close in retrieval score
    and contain locally measurable answer/stem support.
    """
    candidates = [dict(item) for item in (evidence or []) if isinstance(item, dict)]
    if not candidates:
        return []
    ranked = []
    for index, item in enumerate(candidates):
        excerpt, _ = build_evidence_excerpt(question, item)
        support = evidence_sentence_score(question, excerpt)
        retrieval = float(item.get("score") or 0)
        ranked.append((support, retrieval, -index, item))
    ranked.sort(key=lambda row: row[:3], reverse=True)
    strongest = ranked[0]
    strongest_retrieval = max(row[1] for row in ranked)
    retrieval_floor = max(0.55, strongest_retrieval - 0.15)
    selected: list[dict] = []
    seen_sources: set[tuple[str, str]] = set()
    for row in ranked:
        support, retrieval, _index, item = row
        source_key = (
            str(item.get("textbook") or item.get("source_file") or ""),
            str(item.get("textbook_version") or ""),
        )
        if source_key in seen_sources:
            continue
        if row is not strongest and (retrieval < retrieval_floor or support <= 0):
            continue
        selected.append(item)
        seen_sources.add(source_key)
        if len(selected) >= 4:
            break
    return selected


class LLMClient:
    def __init__(
        self, api_key: str, base_url: str, model: str = "deepseek-v4-flash",
        temperature: float = 0.3, max_tokens: int = 2200, timeout_seconds: float = 180,
        *, provider: str = "deepseek", protocol: str = "openai",
    ):
        if not api_key:
            raise ValueError("请在设置中配置所选模型服务的 API Key")
        if not model.strip():
            raise ValueError("请在设置中填写模型名称")
        self.provider, self.protocol = provider, protocol
        self.api_key = api_key
        self.base_url = normalized_url(base_url, protocol)
        self.model = model
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout_seconds = float(timeout_seconds)
        self._local = threading.local()

    def _thread_client(self):
        """One HTTP client per worker thread, preserving connection reuse safely."""
        client = getattr(self._local, "client", None)
        if client is None:
            client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                # Retries are coordinated across workers by GenerationService.
                max_retries=0,
            )
            self._local.client = client
        return client

    def _complete_with_meta(
        self, prompt: str, *, model: str | None = None,
        thinking: bool | None = None, max_tokens: int | None = None,
    ) -> tuple[str, dict]:
        if self.protocol != "openai":
            try:
                return complete_native(
                    protocol=self.protocol, base_url=self.base_url, api_key=self.api_key,
                    model=str(model or self.model), system="你是医学题库解析编辑。必须严格区分教材证据与一般医学知识，不得捏造教材内容。",
                    prompt=prompt, temperature=self.temperature, max_tokens=int(max_tokens or self.max_tokens),
                    timeout=self.timeout_seconds,
                )
            except GatewayError as exc:
                status = exc.status_code
                raise LLMRequestError(safe_error(exc, self.api_key), status_code=status,
                    retry_after=_retry_after_seconds(exc.retry_after), retryable=status in {0, 429, 500, 502, 503, 504},
                    fatal_global=status in {401, 402, 403, 404} or (status in {400, 422} and "model" in str(exc).lower())) from exc
        # ``client`` is retained as a compatibility seam for existing provider tests.
        client = getattr(self, "client", None) or self._thread_client()
        json_mode = any(marker in prompt for marker in ("只输出 JSON", "合法 JSON", "JSON 对象"))
        request = {
            "model": str(model or self.model),
            "messages": [
                {"role": "system", "content": "你是医学题库解析编辑。必须严格区分教材证据与一般医学知识，不得捏造教材内容。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": int(max_tokens or self.max_tokens),
        }
        if thinking is not None and self.provider == "deepseek":
            request["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
        # DeepSeek JSON Output prevents otherwise valid packages from being
        # rejected because of stray prose or broken delimiters.
        if json_mode and self.provider == "deepseek":
            request["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        try:
            response = client.chat.completions.create(**request)
        except APIStatusError as exc:
            status = int(getattr(exc, "status_code", 0) or 0)
            headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
            message = safe_error(exc, self.api_key)
            retryable = status in {429, 500, 502, 503, 504}
            lowered = message.lower()
            fatal = status in {401, 402, 403, 404} or (status in {400, 422} and "model" in lowered)
            raise LLMRequestError(
                message, status_code=status,
                retry_after=_retry_after_seconds(headers.get("retry-after")),
                retryable=retryable, fatal_global=fatal,
            ) from exc
        except (APITimeoutError, APIConnectionError) as exc:
            raise LLMRequestError(safe_error(exc, self.api_key), retryable=True) from exc
        if not response.choices or not (response.choices[0].message.content or "").strip():
            raise LLMRequestError("模型未返回可用文本，请检查所选协议、模型及输出限制")
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None) if usage else None
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) if usage else 0
        meta = {
            "requestId": str(getattr(response, "id", "") or ""),
            "httpStatus": 200,
            "finishReason": str(getattr(choice, "finish_reason", "") or ""),
            "model": str(getattr(response, "model", "") or request["model"]),
            "thinking": bool(thinking) if self.provider == "deepseek" else False,
            "elapsedMs": int((time.monotonic() - started) * 1000),
            "promptTokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completionTokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "reasoningTokens": int(getattr(details, "reasoning_tokens", 0) or 0),
            "cachedPromptTokens": int(cached or 0),
            "cacheStatus": "hit" if int(cached or 0) > 0 else "miss",
        }
        return (choice.message.content or "").strip(), meta

    def _complete(
        self, prompt: str, *, model: str | None = None,
        thinking: bool | None = None, max_tokens: int | None = None,
    ) -> str:
        if thinking is None:
            thinking = False
        content, meta = self._complete_with_meta(
            prompt, model=model, thinking=thinking, max_tokens=max_tokens,
        )
        if meta.get("finishReason") == "length":
            raise ExplanationPackageError(
                "模型输出达到长度上限，结构化解析不完整", code="output_truncated",
                partial_content=content, request_meta=meta,
            )
        return content

    def _structured_completion(
        self, prompt: str, *, model: str, thinking: bool, max_tokens: int,
        response_model=None,
    ) -> tuple[str, dict]:
        """Retain the small `_complete` override seam used by provider integrations."""
        if response_model is not None:
            prompt = (
                f"{prompt}\n\n必须严格匹配以下 JSON Schema；不得输出未声明字段：\n"
                f"{json.dumps(instructor_schema(response_model), ensure_ascii=False)}"
            )
        if type(self)._complete is not LLMClient._complete:
            started = time.monotonic()
            content = self._complete(prompt)
            return content, {
                "finishReason": "stop", "model": model, "thinking": thinking,
                "elapsedMs": int((time.monotonic() - started) * 1000),
                "promptTokens": 0, "completionTokens": 0, "reasoningTokens": 0,
                "cachedPromptTokens": 0,
                "cacheStatus": "unknown",
            }
        return self._complete_with_meta(
            prompt, model=model, thinking=thinking, max_tokens=max_tokens,
        )

    @staticmethod
    def _json_object(content: str, response_model=None) -> dict:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content or "").strip(), flags=re.I | re.S).strip()
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            value = None
            for match in re.finditer(r"\{", cleaned):
                try:
                    candidate, _end = decoder.raw_decode(cleaned[match.start():])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    value = candidate
                    break
            if value is None:
                raise ValueError("模型未返回完整 JSON 对象")
        if not isinstance(value, dict):
            raise ValueError("模型返回结果不是 JSON 对象")
        if response_model is not None:
            value = response_model.model_validate(value).model_dump(mode="json", exclude_none=True)
        return value

    @staticmethod
    def _canonical_evidence(
        matched_chunks: list[dict], question_text: str = "", selections: object = None,
    ) -> list[dict]:
        matched_chunks = select_best_evidence(question_text, matched_chunks)
        selection_map = {}
        if isinstance(selections, list):
            selection_map = {str(item.get("evidenceId") or ""): item for item in selections if isinstance(item, dict)}
        elif isinstance(selections, dict):
            if selections.get("evidenceId"):
                selection_map = {str(selections.get("evidenceId")): selections}
            else:
                selection_map = {str(key): value for key, value in selections.items() if isinstance(value, dict)}
        rows = normalize_evidence([{
            "evidenceId": f"E{index}",
            "textbook": item.get("textbook") or item.get("source_file", ""),
            "version": item.get("version") or item.get("textbook_version", ""),
            "fileName": item.get("source_file", ""),
            "sourcePage": item.get("source_page"), "pdfPage": item.get("pdf_page"),
            "quote": item.get("quote") or item.get("text", ""), "score": item.get("score", 0),
            "extractionMethod": item.get("extraction_method", "text"),
            "supportedClues": item.get("supportedClues") or item.get("supported_clues") or [],
        } for index, item in enumerate(matched_chunks, 1)])
        for index, row in enumerate(rows, 1):
            row["evidenceId"] = f"E{index}"
            selected = selection_map.get(row["evidenceId"], {})
            row["excerpt"], row["highlights"] = build_evidence_excerpt(
                question_text, row, selected.get("excerpt", ""), selected.get("highlights") or [],
            )
            row["contextSummary"] = grounded_context_summary(
                question_text, row, selected.get("contextSummary", ""),
            )
        return rows

    @staticmethod
    def _normalize_package(
        value: dict, evidence: list[dict], official_tags: list[str] | None = None,
        mode: str = "textbook",
    ) -> dict:
        # HTML is never a storage format. Any literal tags are reduced to text.
        def strip_html(raw):
            if isinstance(raw, str): return re.sub(r"<[^>]*>", "", raw)
            if isinstance(raw, list): return [strip_html(item) for item in raw]
            if isinstance(raw, dict): return {key:strip_html(item) for key,item in raw.items()}
            return raw
        value = strip_html(value)
        blocks = normalize_blocks(value.get("explanationBlocks") or value.get("blocks"), "")
        if not blocks:
            raise ValueError("结构化解析区块为空")
        proposed = normalize_tags([*(value.get("tags") or []), *(value.get("suggestedTags") or [])])
        if not proposed:
            raise ValueError("题目标签为空")
        brief = re.sub(r"\s+", " ", str(
            value.get("briefExplanation") or value.get("shortExplanation") or ""
        )).strip()
        if not brief:
            preferred = next((item for item in blocks if item.get("section") == "answerBasis"), blocks[0])
            if preferred.get("type") in {"paragraph", "callout"}:
                brief = str(preferred.get("text") or "")
            elif preferred.get("type") == "list":
                brief = "；".join(preferred.get("items") or [])
            elif preferred.get("type") == "table":
                brief = "；".join(str(cell) for cell in (preferred.get("rows") or [[""]])[0] if cell)
            brief = re.sub(r"\s+", " ", brief).strip()
        brief = brief[:160]
        if not brief:
            raise ValueError("一句话简析为空")
        official_by_key = {
            re.sub(r"[\s·._-]+", "", str(tag).strip().casefold()): str(tag).strip()
            for tag in normalize_tags(official_tags or [])
        }
        formal, candidates = [], []
        for tag in proposed:
            key = re.sub(r"[\s·._-]+", "", str(tag).strip().casefold())
            target = official_by_key.get(key)
            if target and target not in formal and len(formal) < 3:
                formal.append(target)
            elif not target and tag not in candidates and len(candidates) < 3:
                candidates.append(tag)
        package = {
            "explanationBlocks": blocks,
            "briefExplanation": brief,
            # Kept as an empty compatibility field. New releases no longer generate knowledge points.
            "knowledgePoints": [],
            "tags": formal, "suggestedTags": candidates,
            "explanationMeta": {"mode":mode, "evidence":evidence},
        }
        package["explanation"] = blocks_to_markdown(blocks, evidence)
        validate_explanation_package(package, evidence, mode)
        return package

    def generate_explanation_package(
        self, question_text: str, matched_chunks: list[dict], official_tags: list[str] | None = None,
        *, route=None,
    ) -> dict:
        matched_chunks = select_best_evidence(question_text, matched_chunks)
        evidence_grade = str((matched_chunks[0].get("evidence_grade") if matched_chunks else "") or "B").upper()
        generation_mode = "textbook" if evidence_grade == "A" else "textbook_reasoning"
        is_case = bool(re.search(r"(?:题型：A[234]|患者|病人|主诉|现病史|查体|入院)", question_text))
        def citation(item: dict) -> str:
            page = item.get("source_page")
            return f"课本第{page}页" if page else "课本前置页"

        evidence = "\n\n".join(
            f"【证据 E{index}｜教材：{item.get('textbook') or item['source_file']}"
            f"{' · ' + item.get('textbook_version', '') if item.get('textbook_version') else ''}"
            f"｜文件：{item['source_file']}｜{citation(item)}】\n{item['text']}"
            for index, item in enumerate(matched_chunks, 1)
        )
        prompt = f"""请仅基于以下教材原文为题目生成结构化解析，不要补充原文未支持的结论。

教材原文：
{evidence}

题目：
{question_text}

题型适配要求：
1. A1 型只论证唯一最佳答案，不把“可能正确”当作“最佳”。
2. A2 型必须把病例中的症状、体征或检验线索与答案对应。
3. X 型/多选题必须逐项说明每个已选答案为何成立，并指出少选、多选均不得分。
4. B 型配伍题要说明本小题与共用备选项的最匹配关系，允许同一选项在同组其他小题重复使用。
5. A3/A4 型必须基于病例组当前已给出的信息作答；A4 不得倒推题干尚未给出的后续病情。

只输出 JSON 对象，不要代码围栏。格式：
{{
  "explanationBlocks": [
    {{"section":"analysis|answerBasis|pitfalls|clinicalNotes","type":"paragraph|list|table|callout","title":"标题","text":"段落或提示正文","items":["列表项"],"columns":["列名"],"rows":[["单元格"]],"tone":"info|important|warning"}}
  ],
  "briefExplanation": "25到80字的一句话简析，只说正确答案成立的核心原因",
  "tags": ["必须生成1到3个可复用的题目标签"]
}}
必须同时包含 analysis、answerBasis、pitfalls 三个部分，三者缺一不可。题目标签不得为空。优先复用已有正式标签，也允许创建准确的新标签。不要输出 knowledgePoints 或 suggestedTags。
易错点提示必须使用 table，建议列为“选项或易错点 / 错误原因 / 正确辨析”；比较、鉴别、方法选择也必须使用 table。简单内容用 paragraph/list/callout。通常只生成3个核心区块，总正文控制在600到1400个中文字符；最多6个区块，表格最多5列20行，列表最多20项。不得输出 HTML，不得使用 ** 之类 Markdown 强调符号。
可优先复用的本题目集正式标签：{json.dumps(official_tags or [], ensure_ascii=False)}
不要生成记忆口诀。不要生成教材出处区块或证据原文；来源卡片和关键原句由程序依据证据生成。"""
        prompt += """
另外输出 evidenceContext，用于学生展开查看的整理上下文。格式为：
"evidenceContext":{"evidenceId":"E1","contextSummary":"120至300字的忠实整理文本"}
只整理证据 E1：删除页眉、页码、重复片段和 OCR 噪声，补齐必要标点并改善句序；不得新增、推断或改写原文没有的医学事实，必须保留关键术语、数值和限定条件。不要把完整教材原文重复到解析区块中。
"""
        if generation_mode == "textbook_reasoning":
            prompt += """
本题为 B 级“教材原则 + 病例推理”。必须明确写出：最终结论包含基于题干线索的病例推理，并非教材原文直接结论。教材依据只陈述证据原文支持的事实，不得把病例结论伪装成教材原话。
"""
        if is_case:
            prompt += """
病例题必须按顺序组织为：病例要点、诊断或用药推理链、逐项选项分析表、教材原则、易错点表；最后由 briefExplanation 给出一句话简析。病例要点和推理链分别使用 analysis 区块，选项分析使用 answerBasis 表格，教材原则使用 answerBasis 段落，易错点使用 pitfalls 表格。
"""
        route_model = str(getattr(route, "model", "") or self.model)
        route_thinking = bool(getattr(route, "thinking", False))
        route_tokens = int(getattr(route, "max_tokens", 0) or (6000 if route_thinking else 3200))
        raw, first_meta = self._structured_completion(
            prompt, model=route_model, thinking=route_thinking, max_tokens=route_tokens,
            response_model=ExplanationPackageResponse,
        )
        traces = [{**first_meta, "request": 1}]
        try:
            decoded = self._json_object(raw, ExplanationPackageResponse)
            evidence_meta = self._canonical_evidence(
                matched_chunks, question_text, decoded.get("evidenceContext") or decoded.get("evidenceExcerpts"),
            )
            package = self._normalize_package(decoded, evidence_meta, official_tags, mode=generation_mode)
        except Exception as first_error:
            repair = f"""重新生成一份精简且完整的结构化解析，只输出合法 JSON。不要复述任务说明，不要代码围栏或 HTML。必须包含 explanationBlocks、briefExplanation、tags；必须有 analysis、answerBasis、pitfalls，pitfalls 使用表格；tags 有1到3个。只使用下面的题目和教材证据，不增添其他医学事实。
题目：{question_text}
可优先复用的标签：{json.dumps(official_tags or [], ensure_ascii=False)}
教材证据：{evidence[:6000]}"""
            try:
                repaired_raw, repair_meta = self._structured_completion(
                    repair, model=route_model, thinking=False, max_tokens=3200,
                    response_model=ExplanationPackageResponse,
                )
                traces.append({**repair_meta, "request": 2, "compactRegeneration": True})
                decoded = self._json_object(repaired_raw, ExplanationPackageResponse)
                evidence_meta = self._canonical_evidence(
                    matched_chunks, question_text, decoded.get("evidenceContext") or decoded.get("evidenceExcerpts"),
                )
                package = self._normalize_package(decoded, evidence_meta, official_tags, mode=generation_mode)
            except Exception as repair_error:
                code = "output_truncated" if first_meta.get("finishReason") == "length" else (
                    "invalid_json" if isinstance(first_error, (ValueError, json.JSONDecodeError)) else "schema_validation_failed"
                )
                raise ExplanationPackageError(
                    f"模型输出无法转换为完整解析：{first_error}；精简重生成失败：{repair_error}",
                    code=code, partial_content=raw, request_meta={"requests": traces},
                ) from repair_error
        meta = package.setdefault("explanationMeta", {})
        meta.update({
            "mode": generation_mode,
            "evidenceGrade": evidence_grade,
            "reasoningType": "case_chain" if is_case else "direct_answer",
            "model": str(traces[-1].get("model") or route_model),
            "thinking": bool(traces[-1].get("thinking")),
            "complexityScore": int(getattr(route, "complexity_score", 0) or 0),
            "routeReasons": list(getattr(route, "reasons", []) or []),
            "requestCount": len(traces),
            "generationTrace": traces,
        })
        return package

    def generate_explanation(self, question_text: str, matched_chunks: list[dict]) -> str:
        """Compatibility wrapper retained for CLI/provider integrations."""
        return self.generate_explanation_package(question_text, matched_chunks)["explanation"]

    def generate_fallback_explanation(self, question_text: str) -> str:
        prompt = f"""请基于通用医学知识为下面题目写一段简短解析。
只说明正确答案为什么正确，以及最关键的一处易错点；总长度控制在 120～260 个中文字符，不展开背景知识，不写教材出处，不写免责声明。

题目：
{question_text}"""
        return self._complete(prompt)

    def generate_fallback_package(self, question_text: str, *, route=None) -> dict:
        prompt = f"""请基于通用医学知识为下面题目生成精简结构化解析，只输出 JSON 对象，不要代码围栏。
要求：最多 2 个区块；只保留“结论与依据”和“易错点”；全部正文合计 120～260 个中文字符；不展开背景知识，不写教材出处、免责声明或 HTML。题目标签必须有1到3个；易错点必须使用表格。
按题干中的题型标注适配：A1 选唯一最佳项；A2 联系病例线索；X 型逐项核对所有正确项；B 型说明本小题与共用选项的匹配；A3/A4 只使用当前已给出的病例信息。
格式：
{{"explanationBlocks":[{{"section":"answerBasis","type":"paragraph","title":"结论与依据","text":"..."}},{{"section":"pitfalls","type":"table","title":"易错点","columns":["易错点","辨析"],"rows":[["...","..."]]}}],"briefExplanation":"25到80字的一句话简析","tags":["1到3个题目标签"]}}

题目：
{question_text}"""
        route_model = str(getattr(route, "model", "") or self.model)
        raw, first_meta = self._structured_completion(
            prompt, model=route_model, thinking=False, max_tokens=3200,
            response_model=ExplanationPackageResponse,
        )
        traces = [{**first_meta, "request": 1}]
        try:
            package = self._normalize_package(self._json_object(raw, ExplanationPackageResponse), [], mode="general_knowledge")
        except Exception as first_error:
            repair = f"""重新生成精简的通识解析，只输出合法 JSON。仅包含 explanationBlocks、briefExplanation、tags；tags 必须有1到3个，易错点使用 table，不写教材出处。题目：{question_text}"""
            try:
                repaired, repair_meta = self._structured_completion(
                    repair, model=route_model, thinking=False, max_tokens=3200,
                    response_model=ExplanationPackageResponse,
                )
                traces.append({**repair_meta, "request": 2, "compactRegeneration": True})
                package = self._normalize_package(
                    self._json_object(repaired, ExplanationPackageResponse), [], mode="general_knowledge",
                )
            except Exception as exc:
                code = "output_truncated" if first_meta.get("finishReason") == "length" else (
                    "invalid_json" if isinstance(first_error, (ValueError, json.JSONDecodeError)) else "schema_validation_failed"
                )
                raise ExplanationPackageError(
                    f"通识解析精简重生成失败：{exc}", code=code,
                    partial_content=raw, request_meta={"requests": traces},
                ) from exc
        package["explanationBlocks"] = package["explanationBlocks"][:2]
        package["knowledgePoints"] = []
        package["tags"] = package.get("tags", [])[:3]
        package["suggestedTags"] = package.get("suggestedTags", [])[:3]
        package["explanationMeta"].update({
            "mode": "general_knowledge", "evidenceGrade": "C",
            "model": str(traces[-1].get("model") or route_model), "thinking": False,
            "requestCount": len(traces), "generationTrace": traces,
        })
        package["explanation"] = blocks_to_markdown(package["explanationBlocks"])
        return package

    def generate_tag_batch(
        self, questions: list[dict], official_tags: dict[str, list[str]] | None = None,
    ) -> dict[str, dict]:
        """Generate only reusable tags and optional missing briefs without rewriting explanations."""
        items = []
        for question in questions[:20]:
            raw = question.get("raw") if isinstance(question.get("raw"), dict) else {}
            items.append({
                "id": str(question.get("id") or question.get("external_id") or ""),
                "subject": str(question.get("subject") or raw.get("subject") or ""),
                "question": str(question.get("prompt_text") or raw.get("question") or "")[:1800],
                "answer": str(raw.get("answer") or "")[:40],
                "briefExplanation": str(raw.get("briefExplanation") or "")[:160],
                "existingExplanation": str(question.get("explanation") or raw.get("explanation") or "")[:1600],
            })
        if not items:
            return {}
        catalog = {
            str(subject): normalize_tags(labels)
            for subject, labels in (official_tags or {}).items()
        }
        prompt = f"""只为下面题目补充题目标签，不重写解析，不修改医学结论。只输出 JSON 对象：
{{"items":[{{"id":"原ID","tags":["1到3个标签"],"briefExplanation":"仅在原简析为空时补充25到80字简析"}}]}}

规则：
1. 每题必须返回1到3个可复用的医学归类标签，每个2到24字；不要把完整题干、答案字母或题目ID当标签。
2. 优先复用相同学科的正式标签；确无合适标签时才创建准确、可复用的新标签。
3. 不输出 knowledgePoints、suggestedTags、Markdown、HTML 或额外说明。
4. 输入已有一句话简析时原样返回；为空时才依据现有解析提炼，禁止补充解析没有的医学事实。

各学科正式标签：{json.dumps(catalog, ensure_ascii=False)}
题目：{json.dumps(items, ensure_ascii=False)}"""
        prompt += f"\n\nJSON Schema: {json.dumps(instructor_schema(TagBatchResponse), ensure_ascii=False)}"
        payload = self._json_object(self._complete(prompt), TagBatchResponse)
        rows = payload.get("items")
        if not isinstance(rows, list):
            raise ExplanationPackageError("标签批量响应缺少 items 数组")
        expected = {item["id"] for item in items}
        result: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("id") or "")
            if item_id not in expected or item_id in result:
                continue
            tags = normalize_tags(row.get("tags"))
            if not tags:
                continue
            brief = re.sub(r"\s+", " ", str(row.get("briefExplanation") or "")).strip()[:160]
            result[item_id] = {"tags": tags[:3], "briefExplanation": brief}
        return result

    def generate_study_point_batch(self, questions: list[dict]) -> dict[str, list[dict]]:
        """Generate review-grade study points without rewriting explanations."""
        items = []
        for question in questions[:20]:
            raw = question.get("raw") if isinstance(question.get("raw"), dict) else {}
            items.append({
                "id": str(question.get("id") or question.get("external_id") or ""),
                "subject": str(question.get("subject") or raw.get("subject") or ""),
                "question": str(question.get("prompt_text") or raw.get("question") or "")[:1800],
                "answer": str(raw.get("answer") or "")[:40],
                "existingExplanation": str(question.get("explanation") or raw.get("explanation") or "")[:1600],
            })
        if not items:
            return {}
        prompt = f"""为下面每道题提炼复习用考点，不重写解析，不修改医学结论。只输出 JSON 对象：
{{"items":[{{"id":"原ID","studyPoints":[{{"title":"考点标题","body":"复习正文"}}]}}]}}

规则：
1. 每题 1 到 3 条考点；title 不超过 20 字，是该考点跨题可复用的标准名称（例如"胆囊结石首选检查"），同一考点在不同题目中必须使用完全相同的 title。
2. body 为 100 到 300 字的完整复习资料，讲清机制、鉴别要点与记忆点；可提炼自现有解析，禁止补充解析没有的医学事实。
3. 不输出 knowledgePoints、tags、suggestedTags、Markdown、HTML 或额外说明。
题目：{json.dumps(items, ensure_ascii=False)}"""
        prompt += f"\n\nJSON Schema: {json.dumps(instructor_schema(StudyPointBatchResponse), ensure_ascii=False)}"
        payload = self._json_object(self._complete(prompt), StudyPointBatchResponse)
        rows = payload.get("items")
        if not isinstance(rows, list):
            raise ExplanationPackageError("考点批量响应缺少 items 数组")
        expected = {item["id"] for item in items}
        result: dict[str, list[dict]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("id") or "")
            if item_id not in expected or item_id in result:
                continue
            points = normalize_study_points(row.get("studyPoints"))
            if points:
                result[item_id] = points
        return result

    def generate_memory_card_batch(self, questions: list[dict]) -> dict[str, list[dict]]:
        """Generate a separate memorization artifact instead of repackaging the answer explanation."""
        items = []
        sources: dict[str, dict] = {}
        for question in questions[:20]:
            raw = question.get("raw") if isinstance(question.get("raw"), dict) else {}
            item_id = str(question.get("id") or question.get("external_id") or "")
            source = {
                "knowledgePoints": raw.get("knowledgePoints") or [],
                "tags": raw.get("tags") or [],
                "suggestedTags": raw.get("suggestedTags") or [],
                "studyPoints": raw.get("studyPoints") or [],
                "briefExplanation": raw.get("briefExplanation") or "",
                "explanation": question.get("explanation") or raw.get("explanation") or "",
                "explanationBlocks": raw.get("explanationBlocks") or [],
                "mnemonic": raw.get("mnemonic") or "",
            }
            sources[item_id] = source
            items.append({
                "id": item_id,
                "subject": str(question.get("subject") or raw.get("subject") or ""),
                "question": str(question.get("prompt_text") or raw.get("question") or "")[:1800],
                "answer": str(raw.get("answer") or "")[:40],
                "oldTags": [*(raw.get("knowledgePoints") or []), *(raw.get("tags") or [])],
                "oldExplanation": str(source["explanation"])[:1800],
            })
        if not items:
            return {}
        prompt = f"""为下面每道医学题额外创作“独立背诵知识卡”。这是一套新内容，不是答案解析摘要，也不是旧标签扩写。只输出 JSON：
{{"items":[{{"id":"原ID","memoryCards":[{{"title":"独立主题","content":"可脱离原题直接背诵的知识正文","memoryCue":"可选记忆提示","contrast":"可选对比辨析"}}]}}]}}

规则：
1. 每题 1 到 3 张卡；title 不超过 30 字，content 100 到 500 字。
2. content 必须写成独立教材式知识说明，不得出现“本题、正确答案、选项A/B”等答题解析措辞。
3. 禁止逐句复述或改写 oldTags、oldExplanation；不要把标签、一句话简析、答案解析、旧 studyPoints 或口诀搬入卡片。内容结构与措辞必须重新组织。
4. memoryCue 只写新的压缩记忆线索；contrast 仅在确有易混概念时填写。不得输出 knowledgePoints、studyPoints、tags、explanationBlocks、Markdown、HTML 或额外说明。
5. 医学事实必须可靠，不确定的内容不要生成。
题目：{json.dumps(items, ensure_ascii=False)}"""
        prompt += f"\n\nJSON Schema: {json.dumps(instructor_schema(MemoryCardBatchResponse), ensure_ascii=False)}"
        payload = self._json_object(self._complete(prompt), MemoryCardBatchResponse)
        rows = payload.get("items")
        if not isinstance(rows, list):
            raise ExplanationPackageError("背诵知识卡批量响应缺少 items 数组")
        expected = {item["id"] for item in items}
        result: dict[str, list[dict]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("id") or "")
            if item_id not in expected or item_id in result:
                continue
            cards = normalize_memory_cards(row.get("memoryCards"), sources.get(item_id) or {})
            if cards:
                result[item_id] = cards
        return result

    def select_supporting_subjects(
        self, question_text: str, primary_subject: str, available_subjects: list[str],
    ) -> list[str]:
        """Let the model choose whether another indexed discipline is genuinely useful."""
        available = list(dict.fromkeys(str(item).strip() for item in available_subjects if str(item).strip()))[:40]
        if not available:
            return []
        prompt = f"""判断下面医学题目在“{primary_subject}”教材未检索到充分依据后，是否需要从其他学科教材补充检索。
只能从给定列表选择最多 3 个确实可能提供直接知识依据的学科；没有必要则返回空数组。只输出 JSON：{{"subjects":["学科名"]}}。

可选学科：{json.dumps(available, ensure_ascii=False)}
题目：{question_text}"""
        try:
            prompt += f"\n\nJSON Schema: {json.dumps(instructor_schema(SubjectSelectionResponse), ensure_ascii=False)}"
            selected = self._json_object(self._complete(prompt), SubjectSelectionResponse).get("subjects")
        except Exception:
            return []
        if not isinstance(selected, list):
            return []
        allowed = {item.casefold(): item for item in available}
        result = []
        for item in selected:
            matched = allowed.get(str(item).strip().casefold())
            if matched and matched not in result:
                result.append(matched)
            if len(result) >= 3:
                break
        return result

    def restructure_explanation_package(self, old_explanation: str, official_tags: list[str] | None = None) -> dict:
        prompt = f"""把下面已有医学解析重排为结构化 JSON。只能重排、拆分、制表和提炼原文已经明确包含的信息，不得新增任何医学事实，不得生成记忆口诀或教材出处。
输出字段 explanationBlocks、briefExplanation、tags；标签必须1到3个，易错点必须为 table。不要输出 knowledgePoints；禁止 HTML、Markdown 强调符号和代码围栏。优先复用正式标签：{json.dumps(official_tags or [],ensure_ascii=False)}

旧解析：
{old_explanation}"""
        prompt += f"\n\nJSON Schema: {json.dumps(instructor_schema(ExplanationPackageResponse), ensure_ascii=False)}"
        raw = self._complete(prompt)
        package = self._normalize_package(
            self._json_object(raw, ExplanationPackageResponse), [], official_tags, mode="restructured_legacy",
        )
        package["explanationMeta"]["mode"] = "restructured_legacy"
        return package

    def organize_questions(self, raw_text: str, default_subject: str = "") -> list[dict]:
        """Convert raw question text to standard rows without solving missing answers."""
        prompt = f"""请把下面的原始医学题目整理为 JSON 数组，只做结构化整理，不解题、不猜测缺失答案、不补写原文没有的信息。

默认学科：{default_subject or '未提供'}

每个对象使用这些字段：
id, bank, type, subject, system, difficulty, caseInfo, question, options, answer, explanation, knowledgePoint, year

规则：
1. type 只能是 A1、A2、A3、multiple、judge、fill；A1型题/单选题/B型题映射为 A1，A2型题映射为 A2，A3/A4型题映射为 A3，X型题映射为 multiple，填空题映射为 fill，无法判断时用 A1。
2. options 必须是字符串数组，并保留 A./B./C. 等标签。
3. 原文没有答案时 answer 必须是空字符串，绝不能推断答案。
4. 原文没有 ID 时 id 使用空字符串；程序会生成稳定 ID。
5. 原文没有 subject 时使用默认学科；默认学科也为空则保持空字符串。
6. 不输出 Markdown 代码围栏、解释或数组以外的文字。
7. fill 题的 options 为 []，题干空位按【1】【2】连续编号；answer 为二维数组，每空一个候选答案数组，保留原文大小写与同义答案，不自行补充候选。缺答案时 answer 为 []。

原始题目：
{raw_text}"""
        prompt += f"\n\n数组中的每个对象必须匹配 JSON Schema：{json.dumps(instructor_schema(QuestionOrganizeItem), ensure_ascii=False)}"
        content = self._complete(prompt).strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.S).strip()
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            start = content.find("[")
            end = content.rfind("]")
            if start < 0 or end <= start:
                raise ValueError("DeepSeek 未返回可识别的题目 JSON")
            value = json.loads(content[start:end + 1])
        if isinstance(value, dict):
            value = value.get("questions", value.get("data", []))
        if not isinstance(value, list):
            raise ValueError("DeepSeek 返回结果不是题目数组")
        return [
            QuestionOrganizeItem.model_validate(item).model_dump(mode="json")
            for item in value if isinstance(item, dict)
        ]
