import { defineStore } from "pinia"
import { invoke, onJobsChanged } from "../lib/bridge"

export interface JobRow {
  id: string; job_type: string; title: string; status: string
  progress_current: number; progress_total: number; message: string
  error?: string; created_at?: string; updated_at?: string
  payload_json?: string
}

export const useJobsStore = defineStore("jobs", {
  state: () => ({ jobs: [] as JobRow[], listening: false }),
  getters: {
    runningJob: (s) => s.jobs.find((j) => j.status === "running") ?? null,
    progressPercent(): number {
      const job = this.runningJob
      return job && job.progress_total > 0
        ? Math.round((job.progress_current / job.progress_total) * 100)
        : 0
    },
  },
  actions: {
    async init() {
      const snapshot = await invoke<{ jobs: JobRow[] }>("jobs", "list")
      this.jobs = snapshot.jobs
      if (!this.listening) {
        this.listening = true
        await onJobsChanged((s) => { this.jobs = s.jobs as JobRow[] })
      }
    },
    async control(jobId: string, action: "pause" | "resume" | "cancel") {
      await invoke("jobs", "control", { job_id: jobId, action })
    },
    async remove(jobIds: string[]) {
      await invoke("jobs", "delete", { job_ids: jobIds })
      this.jobs = (await invoke<{ jobs: JobRow[] }>("jobs", "list")).jobs
    },
  },
})
