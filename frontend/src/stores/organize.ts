import { defineStore } from "pinia"
import { invoke, onOrganizeProgress, onOrganizeResult } from "../lib/bridge"
import type { BridgeError, OrganizeSignal } from "../lib/bridge"

export interface OrganizeRow { [key: string]: unknown }

interface ReadSourceResult {
  mode: "text" | "structured"
  kind?: "pharmacology-xlsx" | "structured-docx"
  path?: string; name: string; subject?: string; text?: string
}

const METHOD_LABELS: Record<string, string> = {
  deepseek: "DeepSeek",
  claude: "Claude", gemini: "Gemini", custom: "自定义模型",
  hybrid: "本地识别 + 异常题 AI 整理",
  "structured-docx": "Word 结构",
  "pharmacology-xlsx": "药理学 XLSX",
  local: "本地规则",
}

const TYPE_VALUES = ["A1", "A2", "A3", "multiple", "judge", "fill"]
const DIFFICULTY_VALUES = ["easy", "medium", "hard"]

export const useOrganizeStore = defineStore("organize", {
  state: () => ({
    name: "",
    subject: "",
    bank: "",
    useAI: true,
    rawText: "",
    sourcePath: "",
    sourceName: "",
    structuredKind: "" as "" | "pharmacology-xlsx" | "structured-docx",
    running: false,
    progressCurrent: 0,
    progressTotal: 0,
    status: "粘贴原始题目后点击“自动整理”。",
    rows: [] as OrganizeRow[],
    warnings: [] as string[][],
    method: "",
    filter: "invalid" as "invalid" | "valid" | "all",
    page: 0,
    pageSize: 200,
    selected: [] as number[],
    partialSetId: "",
    importing: false,
    importedCount: 0,
    importedIds: [] as string[],
    listening: false,
    tokenSeq: 0,
    activeToken: "",
  }),
  getters: {
    invalidCount: (s) => s.warnings.filter((item) => item.length > 0).length,
    validCount(): number { return this.rows.length - this.invalidCount },
    filteredIndices(s): number[] {
      return s.rows
        .map((_, index) => index)
        .filter((index) => {
          const invalid = (s.warnings[index]?.length ?? 0) > 0
          if (s.filter === "invalid") return invalid
          if (s.filter === "valid") return !invalid
          return true
        })
    },
    pageCount(): number {
      return Math.max(1, Math.ceil(this.filteredIndices.length / this.pageSize))
    },
    pageIndices(): number[] {
      const start = this.page * this.pageSize
      return this.filteredIndices.slice(start, start + this.pageSize)
    },
  },
  actions: {
    async init() {
      if (this.listening) return
      this.listening = true
      await onOrganizeResult((payload) => { void this.handleResult(payload) })
      await onOrganizeProgress((payload) => {
        if (payload.token !== this.activeToken) return
        this.progressCurrent = payload.current
        this.progressTotal = payload.total
      })
    },
    async pickSource() {
      const { path } = await invoke<{ path: string }>("imports", "pick_organize_file")
      if (path) await this.readSource(path)
    },
    async readSource(path: string) {
      const result = await invoke<ReadSourceResult>("imports", "read_organize_source", { path })
      if (!this.name) this.name = result.name
      if (result.mode === "structured") {
        this.sourcePath = result.path ?? ""
        this.sourceName = result.name
        this.structuredKind = result.kind ?? ""
        if (result.subject && !this.subject) this.subject = result.subject
        this.useAI = false
        this.rawText = result.kind === "pharmacology-xlsx"
          ? `已选择药理学 XLSX 题库\n\n文件：${result.name}\n\n点击“自动整理”后，程序会检测表头并仅在乱码修复能显著提升识别率时转码。\n原解析只保存在审计扩展字段中，不会作为教材证据或默认发给生成模型。`
          : `已识别为结构化医学题目集\n\n文件：${result.name}\n学科：${result.subject || "将从 Word 标题识别"}\n\n点击“自动整理”后，程序会按 Word 的章节、题号和字段标题直接读取，不会调用在线模型。`
        this.status = "等待读取文件；有效题可先导入，缺失内容会留在待调整。"
      } else {
        this.sourcePath = ""
        this.structuredKind = ""
        this.rawText = result.text ?? ""
        this.status = "已读取文本，点击“自动整理”。"
      }
    },
    async organize() {
      if (!this.rawText.trim() && !this.sourcePath) {
        this.status = "请粘贴题目文本或读取题目文件。"
        return
      }
      // 不 await：保证 invoke("organize") 在 organize() 同步阶段发出，
      // 使异步返回的整理结果（可能紧随其后的下一个微任务）按序消费。
      void this.init()
      this.tokenSeq += 1
      this.activeToken = `organize-${this.tokenSeq}`
      this.running = true
      this.progressCurrent = 0
      this.progressTotal = 0
      this.status = this.sourcePath ? "正在读取结构化题目文件…" : "正在识别题干、选项和答案…"
      try {
        await invoke("imports", "organize", {
          token: this.activeToken,
          raw_text: this.sourcePath ? "" : this.rawText,
          default_subject: this.subject.trim(),
          use_ai: this.useAI,
          source_path: this.sourcePath,
        })
      } catch (error) {
        this.running = false
        this.status = (error as BridgeError).message ?? String(error)
      }
    },
    async handleResult(payload: OrganizeSignal) {
      if (payload.token !== this.activeToken) return
      this.running = false
      if (!payload.ok) {
        this.status = payload.error?.message ?? "整理失败"
        return
      }
      const result = payload.data ?? { rows: [], method: "" }
      this.rows = result.rows.map(row => ({...row, bank: row.bank || this.bank}))
      this.method = result.method
      this.selected = []
      this.page = 0
      this.partialSetId = ""
      this.importedCount = 0
      this.importedIds = []
      await this.revalidate()
      const label = METHOD_LABELS[this.method] ?? this.method
      this.status = `整理完成（${label}）：共 ${this.rows.length} 道，待调整 ${this.invalidCount} 道。`
    },
    async revalidate() {
      if (!this.rows.length) {
        this.warnings = []
        return
      }
      const response = await invoke<{ warnings: string[][]; valid_count: number }>(
        "imports", "validate_rows",
        { rows: this.rows, imported_ids: this.importedIds },
      )
      this.warnings = response.warnings
      if (this.page >= this.pageCount) this.page = this.pageCount - 1
      if (this.invalidCount > 0 && this.filter === "valid") this.filter = "invalid"
    },
    updateCell(index: number, field: string, value: string) {
      const row = this.rows[index]
      if (!row) return
      if (field === "options") {
        row.options = value.split(/[|\n]+/).map((item) => item.trim()).filter(Boolean)
      } else if (field === "answer" && row.type === "fill") {
        try { row.answer = JSON.parse(value) } catch { row.answer = value }
      } else if (field === "tags") {
        row.tags = value.split(/[、,，;；|]+/).map((item) => item.trim()).filter(Boolean).slice(0, 3)
      } else {
        row[field] = value
      }
      void this.revalidate()
    },
    applyBulk(field: string, value: string, scope: "selected" | "filtered") {
      const trimmed = value.trim()
      if (!trimmed) throw new Error("请填写要批量设置的值。")
      if (field === "type" && !TYPE_VALUES.includes(trimmed)) {
        throw new Error("题型只能是 A1、A2、A3、multiple、judge 或 fill。")
      }
      if (field === "bank" && !["school", "kaoyan"].includes(trimmed)) throw new Error("题库只能是 school 或 kaoyan。")
      if (field === "difficulty" && !DIFFICULTY_VALUES.includes(trimmed)) {
        throw new Error("难度只能是 easy、medium 或 hard。")
      }
      const indexes = scope === "filtered" ? [...this.filteredIndices] : [...this.selected]
      if (!indexes.length) throw new Error("请先选择需要批量设置的题目。")
      for (const index of indexes) {
        this.rows[index][field] = field === "tags"
          ? trimmed.split(/[、,，;；|]+/).map((item) => item.trim()).filter(Boolean).slice(0, 3)
          : trimmed
      }
      void this.revalidate()
      this.status = `已为 ${indexes.length} 道题批量设置。`
    },
    deleteSelected() {
      for (const index of [...this.selected].sort((a, b) => b - a)) this.rows.splice(index, 1)
      this.selected = []
      void this.revalidate()
    },
    async acknowledgeSourceIssues() {
      for (const index of this.selected) {
        const row = this.rows[index]
        if (!row) continue
        const extensions = {...(row.extensions as Record<string, unknown> ?? {})}
        if (extensions.importIssues) {
          extensions.resolvedImportIssues = extensions.importIssues
          extensions.importIssues = []
          extensions.sourceCheckedAt = new Date().toISOString()
          row.extensions = extensions
        }
      }
      await this.revalidate()
      this.status = "已记录原文核对；字段错误仍需修正。"
    },
    toggleSelected(index: number) {
      this.selected = this.selected.includes(index)
        ? this.selected.filter((item) => item !== index)
        : [...this.selected, index]
    },
    async importValid() {
      if (this.importing) return
      const indexes = this.rows
        .map((_, index) => index)
        .filter((index) => !(this.warnings[index]?.length))
      if (!indexes.length) {
        this.status = "当前没有可导入的题目。"
        return
      }
      if (!this.partialSetId && !this.name.trim()) {
        this.status = "请填写题目集名称。"
        return
      }
      const rows: OrganizeRow[] = indexes.map((index) => ({...this.rows[index], bank: this.rows[index].bank || this.bank}))
      if (rows.some(row => !["school", "kaoyan"].includes(String(row.bank)))) {
        this.status = "请先选择题库，或为题目批量设置 school / kaoyan。"
        return
      }
      this.importing = true
      try {
        const response = await invoke<{ set_id: string; imported: number }>(
          "imports", "import_rows", {
            name: this.name.trim(),
            rows,
            source_path: this.sourcePath || "pasted://manual",
            set_id: this.partialSetId,
          },
        )
        this.partialSetId = response.set_id
        this.importedCount += response.imported
        this.importedIds.push(...rows.map((row) => String(row.id)))
        for (const index of [...indexes].sort((a, b) => b - a)) this.rows.splice(index, 1)
        this.selected = []
        await this.revalidate()
        this.status = this.rows.length
          ? `已导入 ${this.importedCount} 道；剩余 ${this.rows.length} 道待调整。`
          : `已导入全部 ${this.importedCount} 道题。`
      } finally {
        this.importing = false
      }
    },
    async exportJson() {
      const rows = this.rows.filter((_, index) => !(this.warnings[index]?.length))
      if (!rows.length) {
        this.status = "当前没有可导出的题目。"
        return
      }
      const { path } = await invoke<{ path: string }>(
        "imports", "pick_save_path",
        { default_name: "整理后的题目.json", file_filter: "JSON 文件 (*.json)" },
      )
      if (!path) return
      const saved = await invoke<{ path: string; count: number }>(
        "imports", "save_rows_json", { path, rows })
      this.status = `已保存 ${saved.count} 道题。`
    },
    async exportIssues() {
      if (!this.invalidCount) {
        this.status = "当前所有题目均可导入。"
        return
      }
      const { path } = await invoke<{ path: string }>(
        "imports", "pick_save_path",
        { default_name: "题目导入问题报告.csv", file_filter: "CSV 文件 (*.csv)" },
      )
      if (!path) return
      const saved = await invoke<{ path: string; count: number }>(
        "imports", "save_issue_report", { path, rows: this.rows })
      this.status = `待调整报告已导出：${saved.path}`
    },
  },
})
