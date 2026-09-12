import { defineStore } from "pinia"
import { invoke } from "../lib/bridge"

export interface QuestionSetSummary {
  id: string; name: string; question_count: number
  approved_count?: number; unmatched_count?: number; created_at?: string
}

export interface InspectResult {
  kind: "json" | "excel"; name: string
  headers: string[]; mapping: Record<string, string>; fields?: string[]
}

export interface UpgradeEstimate { questions: number; estimated_requests: number; skipped: number }
export interface FileImportItem {
  index: number; number: number; row: Record<string, unknown>; original: unknown
  errors: string[]; removed: boolean; repaired: boolean
}
export interface FileImportReview {
  draft_id: string; name: string; path: string; total: number; ready: number
  issues: number; removed: number; repaired: number; items: FileImportItem[]
}
export interface ImportedResult {
  set_id: string; name: string; question_count: number; repaired_count?: number; removed_count?: number
}

export const useImportsStore = defineStore("imports", {
  state: () => ({
    sets: [] as QuestionSetSummary[],
    filePath: "",
    setName: "",
    inspect: null as InspectResult | null,
    mapping: {} as Record<string, string>,
    importing: false,
    lastImported: null as ImportedResult | null,
    fileReview: null as FileImportReview | null,
    upgradeSetId: "",
    estimate: null as UpgradeEstimate | null,
    estimating: false,
  }),
  actions: {
    async refreshSets() {
      this.sets = (await invoke<{ sets: QuestionSetSummary[] }>("imports", "list_sets")).sets
    },
    async pickFile() {
      const { path } = await invoke<{ path: string }>("imports", "pick_import_file", {})
      if (path) await this.inspectFile(path)
    },
    async inspectFile(path: string) {
      const result = await invoke<InspectResult>("imports", "inspect_file", { path })
      this.filePath = path
      this.inspect = result
      this.setName = result.name
      this.mapping = { ...(result.mapping || {}) }
      this.lastImported = null
      this.fileReview = null
    },
    async importFile() {
      this.importing = true
      try {
        const result = await invoke<ImportedResult | { needs_review: true; review: FileImportReview }>("imports", "import_file", {
          path: this.filePath,
          name: this.setName,
          mapping: this.inspect?.kind === "excel" ? this.mapping : null,
        })
        this.lastImported = null
        if ("needs_review" in result) {
          this.fileReview = result.review
          return false
        }
        this.fileReview = null
        this.lastImported = result
        await this.refreshSets().catch(() => {}) // A committed import must not appear to have failed.
        return true
      } finally {
        this.importing = false
      }
    },
    async resolveFileImport(action: "edit" | "remove" | "restore", index: number, row?: Record<string, unknown>) {
      if (!this.fileReview || this.importing) return
      this.importing = true
      try {
        this.fileReview = await invoke<FileImportReview>("imports", "resolve_file_import", {
          draft_id: this.fileReview.draft_id, action, index, ...(row ? { row } : {}),
        })
      } finally { this.importing = false }
    },
    async commitFileImport() {
      if (!this.fileReview || this.importing) return
      this.importing = true
      try {
        this.lastImported = await invoke<ImportedResult>("imports", "commit_file_import", { draft_id: this.fileReview.draft_id })
        this.fileReview = null
        await this.refreshSets().catch(() => {})
      } finally { this.importing = false }
    },
    async estimateUpgrade(setId: string) {
      this.estimating = true
      try {
        this.upgradeSetId = setId
        this.estimate = await invoke<UpgradeEstimate>("imports", "upgrade_estimate", { set_id: setId })
      } finally {
        this.estimating = false
      }
    },
    async enqueueUpgrade() {
      const target = this.sets.find((item) => item.id === this.upgradeSetId)
      await invoke("jobs", "enqueue", {
        job_type: "upgrade_v2",
        title: `升级 v2：${target?.name ?? this.upgradeSetId}`,
        payload: { set_id: this.upgradeSetId },
      })
      this.estimate = null
    },
  },
})
