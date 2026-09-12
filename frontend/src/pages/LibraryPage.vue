<script setup lang="ts">
import { inject, onMounted, ref } from "vue"
import { PhBookOpen, PhPlus, PhTrash } from "@phosphor-icons/vue"
import { invoke, type BridgeError } from "../lib/bridge"
import { useJobsStore } from "../stores/jobs"
import { useLibrariesStore } from "../stores/libraries"
import type { LibraryRow } from "../stores/libraries"
import LibraryDialog from "../components/LibraryDialog.vue"

const store = useLibrariesStore()
const jobs = useJobsStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})
const openJobs = inject<() => void>("openJobs", () => {})

onMounted(() => {
  store.load().catch((error) => toast((error as BridgeError).message ?? String(error), "destructive"))
  jobs.init().catch((error) => toast((error as BridgeError).message ?? String(error), "destructive"))
})

const cloudNoticeRow = ref<LibraryRow | null>(null)
const health = ref<{ ok: boolean; message: string } | null>(null)
const checkingHealth = ref(false)
async function checkHealth() {
  checkingHealth.value = true
  try { health.value = await invoke("settings", "parser_health") }
  catch (e: any) { toast(e?.message ?? String(e), "destructive") }
  finally { checkingHealth.value = false }
}

const STATUS_LABELS: Record<string, string> = {
  ready: "可用", warning: "部分页面异常", indexing: "未完成",
  error: "失败", missing: "文件丢失",
}
const INDEX_LABELS: Record<string, string> = {
  none: "未建立索引", unknown: "索引未就绪", compatible: "索引可用", stale: "需重建", partial: "索引有异常", error: "索引失败",
}

function run(action: () => unknown) {
  try {
    const result = action()
    if (result instanceof Promise) {
      result.catch((error) => toast((error as BridgeError).message ?? String(error), "destructive"))
    }
  } catch (error) {
    toast((error as BridgeError).message ?? String(error), "destructive")
  }
}

function calibration(row: LibraryRow): string {
  return row.page_offset ? `课本页 = PDF页 - ${row.page_offset}` : "页码一致"
}

function scanJob(row: LibraryRow) {
  return jobs.jobs.find((job) => {
    if (job.job_type !== "scan" || !["queued", "running", "paused"].includes(job.status)) return false
    try {
      return JSON.parse((job as unknown as { payload_json?: string }).payload_json ?? "{}").library_id === row.id
    } catch {
      return false
    }
  }) ?? null
}

function scanPercent(row: LibraryRow): number {
  const job = scanJob(row)
  return job && job.progress_total > 0
    ? Math.round((job.progress_current / job.progress_total) * 100)
    : 0
}

function onScan(row: LibraryRow) {
  run(async () => {
    const outcome = await store.startScan(row)
    if (outcome === "needs_confirm") {
      cloudNoticeRow.value = row
    } else {
      toast("索引任务已加入队列", "success")
      openJobs()
    }
  })
}

function onCloudNoticeConfirm() {
  const row = cloudNoticeRow.value
  cloudNoticeRow.value = null
  if (!row) return
  run(async () => {
    await store.confirmCloudNotice(row)
    toast("索引任务已加入队列", "success")
    openJobs()
  })
}

function onRemove(row: LibraryRow) {
  if (!window.confirm(`移除“${row.name}”？已有审核证据仍会保留。`)) return
  run(async () => {
    await store.remove(row)
    toast("教材已移除", "success")
  })
}

function onInfer(row: LibraryRow) {
  run(async () => {
    const offset = await store.inferOffset(row)
    if (offset === null) {
      toast("索引文本中没有找到足够一致的课本页码。可点击“编辑”手动填写一个对应页。", "destructive")
    } else {
      toast(`已识别页码差值：PDF 页 - ${offset} = 课本页。已有证据中的页码也已同步校正。`, "success")
    }
  })
}
</script>

<template>
  <div>
    <p v-if="health" class="mb-3 rounded-md border border-border p-3 text-sm" :class="health.ok ? 'text-success' : 'text-destructive'">{{ health.message }}</p>
    <div class="mb-6 flex items-center gap-3">
      <h1 class="text-xl font-semibold">教材库</h1>
      <span class="flex-1" />
      <button class="rounded-md border border-border px-3 py-1.5 text-sm" :disabled="checkingHealth" @click="checkHealth">{{ checkingHealth ? '检查中…' : '检查解析环境' }}</button>
      <label class="flex items-center gap-1 text-sm text-foreground-secondary">
        <input type="checkbox" v-model="store.forceOcr" /> 强制重新 OCR 全部页面
      </label>
      <button
        class="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover"
        @click="store.openAdd()"
      ><PhPlus :size="16" /> 添加教材</button>
    </div>

    <div v-if="!store.libraries.length" class="rounded-lg border border-border bg-background p-10 text-center text-sm text-foreground-secondary shadow-card">
      {{ store.loading ? "加载中…" : "还没有教材。点击右上角“添加教材”导入一本 PDF。" }}
    </div>

    <div class="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
      <div
        v-for="row in store.libraries" :key="row.id"
        class="rounded-lg border border-border bg-background p-4 shadow-card"
      >
        <div class="mb-1 flex items-start justify-between gap-2">
          <div class="flex min-w-0 items-center gap-2">
            <PhBookOpen :size="20" class="shrink-0 text-primary" />
            <span class="truncate text-base font-medium">{{ row.name }}</span>
          </div>
          <span
            class="shrink-0 rounded px-1.5 py-0.5 text-xs"
            :class="['stale', 'partial', 'error'].includes(row.index_state) ? 'bg-warning/10 text-warning'
              : row.index_state === 'compatible' ? 'bg-success/10 text-success'
              : 'bg-muted text-foreground-secondary'"
          >{{ INDEX_LABELS[row.index_state] ?? row.index_state }}</span>
        </div>
        <div class="text-sm text-foreground-secondary">
          {{ row.subject }} · {{ row.version || "未填写版本" }}
        </div>
        <div class="mt-1 text-sm text-foreground-secondary">
          <template v-if="row.file_count">
            {{ row.page_count ?? 0 }} 页 · 备用 OCR {{ row.ocr_page_count ?? 0 }} 页 · {{ row.file_name }}
          </template>
          <template v-else>尚未建立索引</template>
        </div>
        <div class="mt-1 text-sm">
          <span>{{ STATUS_LABELS[row.file_status ?? ""] ?? "未建立" }}</span>
          <span class="text-foreground-secondary"> · {{ calibration(row) }}</span>
        </div>
        <div v-if="row.file_error" class="mt-1 rounded-md bg-destructive/5 p-2 text-xs text-destructive">
          <p>{{ row.file_error_summary || row.file_error }}</p>
          <details class="mt-1"><summary class="cursor-pointer">查看上次索引的技术详情</summary><p class="break-words">{{ row.file_error }}</p></details>
        </div>
        <div v-if="scanJob(row)" class="mt-2">
          <div class="h-1.5 overflow-hidden rounded bg-muted">
            <div class="h-full bg-primary transition-all duration-base" :style="{ width: `${scanPercent(row)}%` }" />
          </div>
          <div class="mt-0.5 text-xs text-foreground-secondary">索引 {{ scanPercent(row) }}%</div>
        </div>
        <div class="mt-3 flex flex-wrap gap-2 text-sm">
          <button
            class="rounded-md bg-primary px-2.5 py-1 text-onprimary hover:bg-primary-hover disabled:opacity-50"
            :disabled="!!scanJob(row)" @click="onScan(row)"
          >{{ ['partial', 'error'].includes(row.index_state) ? '重试异常索引' : row.file_count ? '建立 / 更新索引' : '建立索引' }}</button>
          <button class="rounded-md border border-border px-2.5 py-1 hover:bg-muted" @click="onInfer(row)">识别实际页码</button>
          <button class="rounded-md border border-border px-2.5 py-1 hover:bg-muted" @click="store.openEdit(row)">编辑</button>
          <button
            class="flex items-center gap-1 rounded-md border border-destructive/50 px-2.5 py-1 text-destructive hover:bg-destructive/5"
            @click="onRemove(row)"
          ><PhTrash :size="14" /> 移除</button>
        </div>
      </div>
    </div>

    <LibraryDialog v-model:open="store.dialogOpen" :editing="store.editing" />

    <div v-if="cloudNoticeRow" class="fixed inset-0 z-40 bg-foreground/20" @click="cloudNoticeRow = null" />
    <div
      v-if="cloudNoticeRow"
      class="fixed left-1/2 top-1/2 z-50 w-[440px] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-background p-5 shadow-overlay"
    >
      <h2 class="mb-2 text-base font-semibold">云端处理说明</h2>
      <p class="text-sm">
        教材文本将发送至 DashScope 生成向量；本地解析失败的页面可能发送至 Qwen OCR。题目与教材证据会发送至设置中选中的模型服务。是否继续？
      </p>
      <div class="mt-4 flex justify-end gap-2">
        <button class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="cloudNoticeRow = null">取消</button>
        <button class="rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover" @click="onCloudNoticeConfirm">继续</button>
      </div>
    </div>
  </div>
</template>
