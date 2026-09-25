import { defineStore } from "pinia"
import { invoke } from "../lib/bridge"

export interface LibraryRow {
  id: number; name: string; subject: string; version: string; root_path: string
  page_offset: number; file_count: number; page_count: number | null
  ocr_page_count: number | null; file_status: string | null; file_error: string | null
  file_name: string | null; index_fingerprint: string | null; index_state: string
  file_error_summary?: string
  calibration?: { status?: string; mapped_pages?: number; unknown_pages?: number; segments?: unknown[] }
}

export interface LibraryForm {
  name: string; subject: string; root_path: string; version: string
  pdf_anchor: number; textbook_anchor: number
}

export const useLibrariesStore = defineStore("libraries", {
  state: () => ({
    libraries: [] as LibraryRow[],
    currentFingerprint: "",
    loading: false,
    loadRequest: 0,
    forceOcr: false,
    dialogOpen: false,
    editing: null as LibraryRow | null,
  }),
  actions: {
    async load() {
      const request = ++this.loadRequest
      this.loading = true
      try {
        const response = await invoke<{ libraries: LibraryRow[]; current_fingerprint: string }>(
          "library", "list")
        if (request === this.loadRequest) {
          this.libraries = response.libraries
          this.currentFingerprint = response.current_fingerprint
        }
      } finally {
        if (request === this.loadRequest) this.loading = false
      }
    },
    openAdd() {
      this.editing = null
      this.dialogOpen = true
    },
    openEdit(row: LibraryRow) {
      this.editing = row
      this.dialogOpen = true
    },
    async submitDialog(editing: LibraryRow | null, form: LibraryForm) {
      const params = {
        name: form.name, subject: form.subject, root_path: form.root_path,
        version: form.version, page_offset: form.pdf_anchor - form.textbook_anchor,
      }
      if (editing) {
        await invoke("library", "update", { library_id: editing.id, ...params })
      } else {
        await invoke("library", "add", params)
      }
      this.dialogOpen = false
      await this.load()
    },
    async remove(row: LibraryRow) {
      await invoke("library", "remove", { library_id: row.id })
      await this.load()
    },
    async inferOffset(row: LibraryRow): Promise<number | null> {
      const response = await invoke<{ offset: number | null }>(
        "library", "infer_page_offset", { library_id: row.id })
      await this.load()
      return response.offset
    },
    async startScan(row: LibraryRow): Promise<"enqueued" | "needs_confirm"> {
      const { accepted } = await invoke<{ accepted: boolean }>("library", "cloud_notice")
      if (!accepted) return "needs_confirm"
      await this.enqueueScan(row)
      return "enqueued"
    },
    async confirmCloudNotice(row: LibraryRow) {
      await invoke("library", "accept_cloud_notice")
      await this.enqueueScan(row)
    },
    async enqueueScan(row: LibraryRow) {
      await invoke("jobs", "enqueue", {
        job_type: "scan",
        title: `索引：${row.name}`,
        payload: { library_id: row.id, force_ocr: this.forceOcr },
      })
    },
  },
})
