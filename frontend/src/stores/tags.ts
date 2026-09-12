import { defineStore } from "pinia"
import { invoke } from "../lib/bridge"

export interface TagRow {
  id: number; set_id: string; subject: string; label: string
  status: string; usage_count: number; merged_into: string; aliases: string[]
}

export const useTagsStore = defineStore("tags", {
  state: () => ({
    sets: [] as { id: string; name: string; question_count: number }[],
    setId: "",
    subjects: [] as string[],
    subject: "",
    status: "",
    search: "",
    tags: [] as TagRow[],
    loading: false,
    selectedId: null as number | null,
  }),
  getters: {
    selected(): TagRow | null {
      return this.tags.find((row) => row.id === this.selectedId) ?? null
    },
    mergeTargets(): TagRow[] {
      const current = this.selected
      if (!current) return []
      return this.tags.filter(
        (row) => row.id !== current.id && row.subject === current.subject && row.status === "active")
    },
  },
  actions: {
    async init() {
      const options = await invoke<{ sets: { id: string; name: string; question_count: number }[] }>(
        "questions", "filters")
      this.sets = options.sets
      if (!this.sets.length) return
      this.setId = this.sets[0].id
      await this.loadSubjects()
      await this.load()
    },
    async loadSubjects() {
      if (!this.setId) return
      const response = await invoke<{ subjects: string[] }>("tags", "subjects", { set_id: this.setId })
      this.subjects = response.subjects
      if (this.subject && !this.subjects.includes(this.subject)) this.subject = ""
    },
    async load() {
      if (!this.setId) return
      this.loading = true
      try {
        const response = await invoke<{ tags: TagRow[] }>("tags", "list", {
          set_id: this.setId, subject: this.subject, status: this.status, search: this.search,
        })
        this.tags = response.tags
      } finally {
        this.loading = false
      }
    },
    async applySet(setId: string) {
      this.setId = setId
      this.subject = ""
      this.selectedId = null
      await this.loadSubjects()
      await this.load()
    },
    async rename(row: TagRow, label: string) {
      await invoke("tags", "rename", { tag_id: row.id, label })
      await this.load()
    },
    async activate(row: TagRow) {
      await invoke("tags", "activate", { tag_id: row.id })
      await this.load()
    },
    async merge(sourceId: number, targetId: number): Promise<number> {
      const response = await invoke<{ changed: number }>(
        "tags", "merge", { source_id: sourceId, target_id: targetId })
      this.selectedId = null
      await this.load()
      return response.changed
    },
    async remove(row: TagRow, confirmLabel: string) {
      const result = await invoke<{ deleted: string; changed: number }>(
        "tags", "delete", { tag_id: row.id, confirm_label: confirmLabel })
      this.selectedId = null
      await this.load()
      return result
    },
  },
})
