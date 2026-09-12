<script setup lang="ts">
import { inject, ref, watch } from "vue"
import type { BridgeError } from "../../lib/bridge"
import { useReviewStore } from "../../stores/review"

interface PreviewData {
  total: number
  eligible: { id: number; external_id: string; subject: string; match_score: number; evidence_count: number }[]
  excluded: Record<string, number>
  minimum_score: number
}

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ "update:open": [value: boolean]; approved: [result: { approved: number; skipped: number }] }>()

const store = useReviewStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})

const threshold = ref(0.75)
const preview = ref<PreviewData | null>(null)
const loading = ref(false)
const approving = ref(false)

async function refresh() {
  loading.value = true
  try {
    preview.value = await store.quickPreview(threshold.value) as PreviewData
  } catch (error) {
    toast((error as BridgeError).message ?? String(error), "destructive")
  } finally {
    loading.value = false
  }
}

watch(() => props.open, (open) => { if (open) void refresh() })

const excludedText = () => {
  if (!preview.value) return ""
  const parts = Object.entries(preview.value.excluded).map(([reason, count]) => `${reason} ${count} 题`)
  return parts.length ? `未进入快速审核：${parts.join(" · ")}` : ""
}

async function approve() {
  if (!preview.value) return
  approving.value = true
  try {
    const result = await store.quickApprove(
      threshold.value, preview.value.eligible.map((row) => row.id))
    emit("approved", result)
    emit("update:open", false)
  } catch (error) {
    toast((error as BridgeError).message ?? String(error), "destructive")
  } finally {
    approving.value = false
  }
}

const inputClass =
  "rounded-md border border-border bg-background px-2 py-1 text-sm outline-none focus:ring-2 focus:ring-primary"
</script>

<template>
  <div v-if="props.open" class="fixed inset-0 z-40 bg-foreground/20" @click="emit('update:open', false)" />
  <div v-if="props.open" class="fixed left-1/2 top-1/2 z-50 flex max-h-[84vh] w-[640px] -translate-x-1/2 -translate-y-1/2 flex-col rounded-lg border border-border bg-background p-5 shadow-overlay">
    <h2 class="mb-1 text-base font-semibold">快速批量审核</h2>
    <p class="mb-3 text-xs text-warning">
      程序只筛选结构和教材证据均完整的结果；这不能替代对医学内容正确性的人工判断。
    </p>
    <div class="mb-3 flex items-center gap-2 text-sm">
      <span>教材相似度门槛</span>
      <input
        v-model.number="threshold" type="number" min="0.5" max="0.99" step="0.01"
        :class="inputClass" style="width: 90px" @change="refresh"
      />
      <span class="text-foreground-secondary">以上</span>
    </div>
    <p class="mb-3 text-xs text-foreground-secondary">
      固定安全门槛：教材模式 · 无错误/格式警告 · 解析不少于 80 字 · 考点/答案依据/易错点三部分齐全 · 一句话简析和题目标签齐全 · 教材名称、印刷页、证据摘录齐全
    </p>
    <div v-if="preview" class="mb-2 text-sm">
      待审核 {{ preview.total }} 题　·　符合门槛 {{ preview.eligible.length }} 题　·　需人工处理 {{ preview.total - preview.eligible.length }} 题
    </div>
    <div class="min-h-0 flex-1 overflow-y-auto rounded-md border border-border">
      <table v-if="preview" class="w-full border-collapse text-sm">
        <thead class="sticky top-0 bg-muted">
          <tr>
            <th class="px-2 py-1 text-left font-medium">题目 ID</th>
            <th class="px-2 py-1 text-left font-medium">学科</th>
            <th class="px-2 py-1 text-left font-medium">相似度</th>
            <th class="px-2 py-1 text-left font-medium">证据</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in preview.eligible.slice(0, 300)" :key="row.id" class="border-t border-border">
            <td class="px-2 py-1">{{ row.external_id }}</td>
            <td class="px-2 py-1">{{ row.subject }}</td>
            <td class="px-2 py-1">{{ Number(row.match_score).toFixed(3) }}</td>
            <td class="px-2 py-1">证据 {{ row.evidence_count }} 条</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="preview" class="mt-2 text-xs text-foreground-secondary">
      {{ excludedText() }}{{ preview.eligible.length > 300 ? "；列表仅预览前 300 题" : "" }}
    </div>
    <p class="mt-2 text-xs text-foreground-secondary">
      批准后题目会立即进入可导出状态。未通过门槛的题目不会被修改，仍留在人工审核队列。
    </p>
    <div class="mt-4 flex justify-end gap-2">
      <button class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="emit('update:open', false)">取消</button>
      <button
        class="rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover disabled:opacity-50"
        :disabled="!preview || !preview.eligible.length || approving" @click="approve"
      >{{ approving ? "正在批准…" : `批准 ${preview?.eligible.length ?? 0} 道符合门槛的题目` }}</button>
    </div>
  </div>
</template>
