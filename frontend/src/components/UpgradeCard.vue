<script setup lang="ts">
import { inject, watch } from "vue"
import { PhArrowSquareOut } from "@phosphor-icons/vue"
import type { BridgeError } from "../lib/bridge"
import { useImportsStore } from "../stores/imports"

const store = useImportsStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})
const openJobs = inject<() => void>("openJobs", () => {})

watch(() => store.upgradeSetId, () => { store.estimate = null })

async function onEstimate() {
  try {
    await store.estimateUpgrade(store.upgradeSetId)
  } catch (error) {
    toast((error as BridgeError).message ?? String(error), "destructive")
  }
}

async function onConfirm() {
  try {
    await store.enqueueUpgrade()
    toast("升级任务已加入队列", "success")
    openJobs()
  } catch (error) {
    toast((error as BridgeError).message ?? String(error), "destructive")
  }
}

const inputClass =
  "rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:ring-2 focus:ring-primary"
</script>

<template>
  <section class="rounded-lg border border-border bg-background p-4 shadow-card">
    <h2 class="mb-3 text-base font-semibold">升级现有解析为 v2 交换格式</h2>
    <p class="mb-3 text-sm text-foreground-secondary">
      有达标教材证据的题目重新生成结构化解析，只有旧解析的题目仅重排原有事实；
      执行前自动备份数据库，已批准的审核状态保留。
    </p>
    <div class="flex flex-wrap items-center gap-2">
      <select v-model="store.upgradeSetId" :class="inputClass" class="min-w-56">
        <option value="" disabled>选择题目集</option>
        <option v-for="set in store.sets" :key="set.id" :value="set.id">
          {{ set.name }}（{{ set.question_count }} 题）
        </option>
      </select>
      <button
        data-test="estimate"
        class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
        :disabled="!store.upgradeSetId || store.estimating"
        @click="onEstimate"
      >
        {{ store.estimating ? "估算中…" : "估算工作量" }}
      </button>
      <template v-if="store.estimate">
        <span class="text-sm">
          共 {{ store.estimate.questions }} 题，预计 {{ store.estimate.estimated_requests }} 次模型请求，
          {{ store.estimate.skipped }} 题跳过
        </span>
        <button
          data-test="confirm"
          class="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover"
          @click="onConfirm"
        >
          <PhArrowSquareOut :size="16" /> 确认升级
        </button>
        <button class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="store.estimate = null">
          取消
        </button>
      </template>
    </div>
  </section>
</template>
