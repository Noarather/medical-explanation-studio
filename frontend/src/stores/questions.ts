import { defineStore } from "pinia"
import { invoke } from "../lib/bridge"

export interface QuestionRow {
  id: number; set_id: string; external_id: string; subject: string; question_type: string
  prompt_text: string; pipeline_status: string; review_status: string
  generation_mode: string | null; match_score: number | null; error_message: string | null
  updated_at: string; question_source: string; tags: string[]; evidence_grade: string
}

export interface QuestionFilters {
  set_id: string; review_status: string; pipeline_status: string
  subject: string; tag: string; question_source: string; search: string
}

export interface TrashRow {
  id: number; external_id: string; subject: string; prompt_text: string
  set_name: string; deleted_at: string | null; purge_after: string | null
}

export interface StatusOption { label: string; review_status: string; pipeline_status: string }

export const STATUS_OPTIONS: StatusOption[] = [
  { label: "全部状态", review_status: "", pipeline_status: "" },
  { label: "等待生成", review_status: "pending", pipeline_status: "queued" },
  { label: "已生成待审核", review_status: "pending", pipeline_status: "generated" },
  { label: "已批准", review_status: "approved", pipeline_status: "" },
  { label: "已驳回", review_status: "rejected", pipeline_status: "" },
  { label: "未匹配", review_status: "", pipeline_status: "unmatched" },
  { label: "缺少教材", review_status: "", pipeline_status: "no_library" },
  { label: "处理失败", review_status: "", pipeline_status: "error" },
  { label: "已匹配·生成失败", review_status: "", pipeline_status: "matched_generation_error" },
]

export const EMPTY_FILTERS: QuestionFilters = {
  set_id: "", review_status: "", pipeline_status: "",
  subject: "", tag: "", question_source: "", search: "",
}

export const useQuestionsStore = defineStore("questions", {
  state: () => ({
    tab: "questions" as "questions" | "trash",
    filters: { ...EMPTY_FILTERS } as QuestionFilters,
    filterOptions: { sets: [] as { id: string; name: string; question_count: number }[],
      subjects: [] as string[], sources: [] as string[], tags: [] as string[] },
    rows: [] as QuestionRow[],
    total: 0,
    page: 0,
    pageSize: 200,
    selected: [] as number[],
    loading: false,
    detail: null as Record<string, unknown> | null,
    detailOpen: false,
    lastExportCount: 0,
    // trash tab
    trashRows: [] as TrashRow[],
    trashTotal: 0,
    trashPage: 0,
    trashSearch: "",
    trashSelected: [] as number[],
  }),
  getters: {
    pageCount(): number { return Math.max(1, Math.ceil(this.total / this.pageSize)) },
    trashPageCount(): number { return Math.max(1, Math.ceil(this.trashTotal / this.pageSize)) },
  },
  actions: {
    async init() {
      await Promise.all([this.loadFilters(), this.load()])
    },
    async loadFilters() {
      this.filterOptions = await invoke("questions", "filters")
    },
    async load() {
      this.loading = true
      try {
        const response = await invoke<{ rows: QuestionRow[]; total: number }>(
          "questions", "list", { ...this.filters, limit: this.pageSize, offset: this.page * this.pageSize })
        this.rows = response.rows
        this.total = response.total
      } finally {
        this.loading = false
      }
    },
    async applyStatusOption(option: StatusOption) {
      this.filters.review_status = option.review_status
      this.filters.pipeline_status = option.pipeline_status
      this.page = 0
      await this.load()
    },
    async applyFilters() {
      this.page = 0
      await this.load()
    },
    async changePage(delta: number) {
      this.page = Math.min(this.pageCount - 1, Math.max(0, this.page + delta))
      await this.load()
    },
    toggleSelected(id: number) {
      this.selected = this.selected.includes(id)
        ? this.selected.filter((item) => item !== id)
        : [...this.selected, id]
    },
    selectPage() {
      this.selected = [...new Set([...this.selected, ...this.rows.map((row) => row.id)])]
    },
    async selectAllFiltered() {
      const response = await invoke<{ ids: number[] }>("questions", "ids", { ...this.filters })
      this.selected = response.ids
    },
    clearSelection() {
      this.selected = []
    },
    async bulkSetSubject(subject: string) {
      const response = await invoke<{ updated: number }>(
        "questions", "bulk_set_subject", { question_pks: this.selected, subject })
      this.clearSelection()
      await this.load()
      return response.updated
    },
    async moveSelectedToTrash() {
      await invoke("questions", "move_to_trash", { question_pks: this.selected })
      this.clearSelection()
      await this.load()
    },
    async deleteSelected() {
      await invoke("questions", "delete", { question_pks: this.selected })
      this.clearSelection()
      await this.load()
    },
    async exportSelected() {
      const { path } = await invoke<{ path: string }>("imports", "pick_save_path", {
        default_name: "选中题目.json", file_filter: "JSON 文件 (*.json)",
      })
      if (!path) return
      const saved = await invoke<{ path: string; count: number }>(
        "questions", "export_selected", { question_pks: this.selected, path })
      this.lastExportCount = saved.count
    },
    async openDetail(id: number) {
      this.detail = await invoke("questions", "detail", { question_pk: id })
      this.detailOpen = true
    },
    // --- trash tab ---
    async loadTrash() {
      const response = await invoke<{ rows: TrashRow[]; total: number }>(
        "questions", "trash_list",
        { search: this.trashSearch, limit: this.pageSize, offset: this.trashPage * this.pageSize })
      this.trashRows = response.rows
      this.trashTotal = response.total
    },
    toggleTrashSelected(id: number) {
      this.trashSelected = this.trashSelected.includes(id)
        ? this.trashSelected.filter((item) => item !== id)
        : [...this.trashSelected, id]
    },
    async restoreSelected() {
      const result = await invoke<{ restored: number; conflicts: string[] }>(
        "questions", "trash_restore", { trash_ids: this.trashSelected })
      this.trashSelected = []
      await this.loadTrash()
      return result
    },
    async purgeSelected() {
      await invoke("questions", "trash_purge", { trash_ids: this.trashSelected })
      this.trashSelected = []
      await this.loadTrash()
    },
  },
})
