import { defineStore } from "pinia"
import { invoke } from "../lib/bridge"

export const useDashboardStore = defineStore("dashboard", {
  state: () => ({
    counts: null as Record<string, number> | null,
    apiStatus: null as { deepseek: boolean; dashscope: boolean; llm?: boolean } | null,
  }),
  actions: {
    async refresh() {
      const [counts, apiStatus] = await Promise.all([
        invoke<Record<string, number>>("dashboard", "counts"),
        invoke<{ deepseek: boolean; dashscope: boolean; llm?: boolean }>("dashboard", "api_status"),
      ])
      this.counts = counts
      this.apiStatus = apiStatus
    },
  },
})
