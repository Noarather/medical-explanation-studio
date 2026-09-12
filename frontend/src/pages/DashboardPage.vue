<script setup lang="ts">
import { computed, inject, onMounted } from "vue"
import { PhWarning } from "@phosphor-icons/vue"
import type { BridgeError } from "../lib/bridge"
import { useDashboardStore } from "../stores/dashboard"
import { useJobsStore } from "../stores/jobs"
import StatCard from "../components/StatCard.vue"

const dashboard = useDashboardStore()
const jobs = useJobsStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})
const openSettings = inject<() => void>("openSettings", () => {})
const openJobs = inject<() => void>("openJobs", () => {})

onMounted(() => {
  dashboard.refresh().catch((error) => toast((error as BridgeError).message ?? String(error), "destructive"))
  jobs.init().catch((error) => toast((error as BridgeError).message ?? String(error), "destructive"))
})

const needsSetup = computed(
  () => dashboard.apiStatus !== null && !(dashboard.apiStatus.llm ?? dashboard.apiStatus.deepseek) && !dashboard.apiStatus.dashscope,
)

function n(key: string): number | null {
  return dashboard.counts ? dashboard.counts[key] ?? 0 : null
}
</script>

<template>
  <div>
    <h1 class="mb-6 text-xl font-semibold">工作台</h1>

    <div
      v-if="needsSetup"
      class="mb-6 flex items-center justify-between rounded-lg border border-border border-l-[3px] border-l-warning bg-background p-4 shadow-card"
    >
      <div class="flex items-center gap-2 text-sm">
        <PhWarning :size="18" class="text-warning" />
        尚未配置 API 密钥，请先完成设置
      </div>
      <button
        class="rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary transition-colors duration-fast hover:bg-primary-hover"
        @click="openSettings()"
      >
        前往设置
      </button>
    </div>

    <div class="grid grid-cols-2 gap-4 lg:grid-cols-3 2xl:grid-cols-6">
      <StatCard label="教材" :value="n('libraries')" to="/library" />
      <StatCard label="题目总数" :value="n('questions')" />
      <StatCard label="待生成" :value="n('waiting')" />
      <StatCard label="待审核" :value="n('pending')" tone="warning" to="/review" />
      <StatCard label="未匹配" :value="n('unmatched')" tone="warning" to="/questions" />
      <div class="cursor-pointer" @click="openJobs()">
        <StatCard label="失败任务" :value="n('failed')" tone="destructive" />
      </div>
    </div>
  </div>
</template>
