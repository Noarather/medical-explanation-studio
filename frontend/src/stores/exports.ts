import { defineStore } from "pinia"
import { invoke } from "../lib/bridge"

export interface ExportRecord {
  id: number; set_id: string; export_type: string
  output_path: string; item_count: number; created_at: string
}

export const TYPE_LABELS: Record<string, string> = {
  json_v2: "JSON v2", xlsx_v2: "XLSX v2", mapping: "解析映射",
  full: "完整题库", issues: "问题报告",
}

export const useExportsStore = defineStore("exports", {
  state: () => ({
    sets: [] as { id: string; name: string; question_count: number; approved_count?: number }[],
    setId: "",
    outputDir: "",
    split: false,
    exporting: false,
    resultLines: [] as string[],
    history: [] as ExportRecord[],
  }),
  actions: {
    async exportIncremental() {
      this.exporting = true
      try {
        const result = await invoke<{files:string[]; report:string; ready:number; pending:number; issues:unknown[]}>("exports", "export_incremental", {
          set_id: this.setId, output_dir: this.outputDir,
        })
        this.resultLines = [`已导出 ${result.ready} 道变更题；待审核 ${result.pending} 道；导出异常 ${result.issues.length} 道`, ...result.files, `检查报告：${result.report}`]
        await this.loadHistory()
      } finally { this.exporting = false }
    },
    async init() {
      const options = await invoke<{ sets: { id: string; name: string; question_count: number; approved_count?: number }[] }>("questions", "filters")
      this.sets = options.sets
      if (this.sets.length && !this.setId) this.setId = this.sets[0].id
      const dir = await invoke<{ path: string }>("exports", "default_dir")
      if (!this.outputDir) this.outputDir = dir.path
      await this.loadHistory()
    },
    async loadHistory() {
      const response = await invoke<{ records: ExportRecord[] }>(
        "exports", "history", { set_id: "", limit: 30 })
      this.history = response.records
    },
    async pickDirectory() {
      const { path } = await invoke<{ path: string }>(
        "exports", "pick_directory", { current: this.outputDir })
      if (path) this.outputDir = path
    },
    async exportData() {
      this.exporting = true
      try {
        const result = await invoke<Record<string, unknown>>("exports", "export", {
          set_id: this.setId, output_dir: this.outputDir, split_by_subject: this.split,
        })
        if (Array.isArray(result.files)) {
          this.resultLines = [
            ...(result.files as string[]).map((path) => `拆分文件：${path}`),
            `兼容解析映射：${result.mapping}`,
          ]
        } else {
          this.resultLines = [
            `JSON v2：${result.json}`,
            `XLSX v2：${result.xlsx}`,
            `兼容解析映射：${result.mapping}`,
          ]
        }
        await this.loadHistory()
      } finally {
        this.exporting = false
      }
    },
    async exportIssues() {
      const result = await invoke<{ path: string }>("exports", "export_issues", {
        set_id: this.setId, output_dir: this.outputDir,
      })
      this.resultLines = [`问题报告：${result.path}`]
    },
    async openFolder() {
      await invoke("exports", "open_folder", { path: this.outputDir })
    },
  },
})
