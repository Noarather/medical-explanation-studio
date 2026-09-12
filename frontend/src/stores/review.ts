import { defineStore } from "pinia"
import { invoke } from "../lib/bridge"
import type { EditorBlock } from "../lib/blocks"

export interface ReviewListRow {
  id: number; external_id: string; subject: string; question_type: string
  prompt_text: string; pipeline_status: string; review_status: string
  match_score: number | null; evidence_grade: string; updated_at: string
  review_reason?: string
}

export interface ReviewFields {
  tags: string[]; knowledgePoints: string[]; suggestedTags: string[]
  mnemonic: string; briefExplanation: string
}

export const useReviewStore = defineStore("review", {
  state: () => ({
    sets: [] as { id: string; name: string; question_count: number }[],
    setId: "",
    reviewStatus: "pending",
    pipelineStatus: "generated",
    search: "",
    rows: [] as ReviewListRow[],
    total: 0,
    page: 0,
    pageSize: 200,
    listLoading: false,
    currentId: null as number | null,
    detail: null as Record<string, any> | null,
    blocks: [] as EditorBlock[],
    fields: { tags: [], knowledgePoints: [], suggestedTags: [], mnemonic: "", briefExplanation: "" } as ReviewFields,
    subject: "",
    dirty: false,
    saving: false,
    skipRejectConfirm: false,
    selectedEvidenceIndex: 0,
    selected: [] as number[],
    automatic: {enabled: true, approved: 0, pending: 0},
  }),
  getters: {
    pageCount(): number { return Math.max(1, Math.ceil(this.total / this.pageSize)) },
    raw(): Record<string, any> { return this.detail?.raw ?? {} },
    evidence(): Record<string, any>[] { return this.detail?.evidence ?? [] },
    selectedEvidence(): Record<string, any> | null {
      return this.evidence[this.selectedEvidenceIndex] ?? this.evidence[0] ?? null
    },
  },
  actions: {
    async init() {
      const options = await invoke<{ sets: { id: string; name: string; question_count: number }[] }>(
        "questions", "filters")
      this.sets = options.sets
      await this.runAutomatic()
      await this.load()
    },
    async runAutomatic(enabled?: boolean) {
      this.automatic = await invoke("review", "auto_review", {
        set_id: this.setId, ...(enabled === undefined ? {} : {enabled}),
      })
    },
    async load(preferredId: number | null = null) {
      this.listLoading = true
      try {
        const response = await invoke<{ rows: ReviewListRow[]; total: number }>("review", "list", {
          set_id: this.setId, review_status: this.reviewStatus,
          pipeline_status: this.pipelineStatus, search: this.search,
          limit: this.pageSize, offset: this.page * this.pageSize,
        })
        this.rows = response.rows
        this.total = response.total
        if (preferredId !== null && this.rows.some((row) => row.id === preferredId)) {
          await this.open(preferredId)
        } else if (this.currentId !== null && !this.rows.some((row) => row.id === this.currentId)) {
          this.clearCurrent()
        }
      } finally {
        this.listLoading = false
      }
    },
    async applyFilters() {
      this.page = 0
      // 筛选/切换题目集后列表内容变化，清空选择避免跨集合批量操作误伤其他题目集的题
      this.clearSelection()
      await this.load()
    },
    async changePage(delta: number) {
      this.page = Math.min(this.pageCount - 1, Math.max(0, this.page + delta))
      await this.load()
    },
    clearCurrent() {
      this.currentId = null
      this.detail = null
      this.blocks = []
      this.fields = { tags: [], knowledgePoints: [], suggestedTags: [], mnemonic: "", briefExplanation: "" }
      this.subject = ""
      this.dirty = false
      this.selectedEvidenceIndex = 0
    },
    async open(questionPk: number) {
      const detail = await invoke<Record<string, any>>("review", "open", { question_pk: questionPk })
      this.currentId = questionPk
      this.detail = detail
      const raw = detail.raw ?? {}
      this.blocks = (raw.explanationBlocks ?? []).map((block: Record<string, any>) => ({ ...block }))
      if (!this.blocks.length) {
        this.blocks = [{ blockId: "b01-init", section: "analysis", type: "paragraph", title: "考点解析", text: "" }]
      }
      this.fields = {
        tags: [...(raw.tags ?? [])],
        knowledgePoints: [...(raw.knowledgePoints ?? [])],
        suggestedTags: [...(raw.suggestedTags ?? [])],
        mnemonic: raw.mnemonic ?? "",
        briefExplanation: raw.briefExplanation ?? "",
      }
      this.subject = detail.subject ?? ""
      this.dirty = false
      this.selectedEvidenceIndex = 0
    },
    markDirty() {
      this.dirty = true
    },
    async save() {
      if (this.currentId === null) return
      this.saving = true
      try {
        const response = await invoke<{ saved: boolean; question: Record<string, any> }>("review", "save", {
          question_pk: this.currentId,
          explanation_blocks: this.blocks,
          knowledge_points: this.fields.knowledgePoints,
          tags: this.fields.tags,
          suggested_tags: this.fields.suggestedTags,
          mnemonic: this.fields.mnemonic,
          brief_explanation: this.fields.briefExplanation,
          subject: this.subject,
        })
        this.dirty = false
        // 保存后解析 markdown 已重建，就地同步，供 review() 提交给 review_question
        if (this.detail && response.question?.explanation != null) {
          this.detail.explanation = response.question.explanation
        }
        await this.load()
      } finally {
        this.saving = false
      }
    },
    currentExplanation(): string {
      return String(this.detail?.explanation ?? "")
    },
    async review(action: "approved" | "rejected") {
      if (this.currentId === null) return
      if (this.dirty) await this.save()
      const finishedId = this.currentId
      await invoke("review", "review", {
        question_pk: finishedId, action, explanation: this.currentExplanation(),
      })
      await this.advanceAfterAction(finishedId)
    },
    async advanceAfterAction(finishedId: number) {
      const index = this.rows.findIndex((row) => row.id === finishedId)
      const next = this.rows[index + 1] ?? this.rows[index - 1] ?? null
      this.clearCurrent()
      await this.load()
      if (next && this.rows.some((row) => row.id === next.id)) {
        await this.open(next.id)
      } else if (this.rows.length) {
        await this.open(this.rows[0].id)
      }
    },
    async regenerate() {
      if (this.currentId === null) return
      await invoke("review", "regenerate", { question_pk: this.currentId })
      this.clearCurrent()
      await this.load()
    },
    toggleSelected(id: number) {
      this.selected = this.selected.includes(id)
        ? this.selected.filter((item) => item !== id)
        : [...this.selected, id]
    },
    selectPageRows() {
      this.selected = [...new Set([...this.selected, ...this.rows.map((row) => row.id)])]
    },
    clearSelection() {
      this.selected = []
    },
    async batchSetSubject(subject: string) {
      await invoke("questions", "bulk_set_subject", { question_pks: this.selected, subject })
      this.clearSelection()
      await this.load()
    },
    async batchRegenerate(libraryIds: number[]) {
      await invoke("review", "regenerate_batch", {
        set_id: this.setId, question_ids: this.selected, library_ids: libraryIds,
      })
      this.clearSelection()
      this.clearCurrent()
      await this.load()
    },
    async batchGeneral(): Promise<number> {
      const preview = await invoke<{ count: number }>("review", "general_batch_preview", {
        set_id: this.setId, question_ids: this.selected.length ? this.selected : null,
      })
      if (!preview.count) return 0
      await invoke("jobs", "enqueue", {
        job_type: "general_batch",
        title: `批量通识生成：${preview.count} 题`,
        payload: { set_id: this.setId, question_ids: this.selected.length ? this.selected : null },
      })
      this.clearSelection()
      return preview.count
    },
    async quickPreview(minimumScore: number) {
      return await invoke("review", "quick_review_preview", {
        set_id: this.setId, minimum_score: minimumScore,
      })
    },
    async quickApprove(minimumScore: number, expectedIds: number[]) {
      const result = await invoke<{ approved: number; skipped: number }>(
        "review", "quick_review_approve", {
          set_id: this.setId, minimum_score: minimumScore, expected_ids: expectedIds,
        })
      this.clearCurrent()
      await this.load()
      return result
    },
    async authorizeGeneral() {
      if (this.currentId === null) return
      await invoke("review", "general_authorize", { question_pk: this.currentId })
      this.clearCurrent()
      await this.load()
    },
    async backfillEstimate(kind: string) {
      return await invoke<Record<string, number>>("review", "backfill_estimate", {
        kind, set_id: this.setId,
      })
    },
    async enqueueBackfill(kind: string, title: string) {
      await invoke("jobs", "enqueue", {
        job_type: `${kind === "tags" ? "tag" : kind === "study_points" ? "study_point" : "memory_card"}_backfill`,
        title,
        payload: { set_id: this.setId, include_approved: true },
      })
    },
  },
})
