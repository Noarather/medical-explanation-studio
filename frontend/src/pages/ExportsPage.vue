<script setup lang="ts">
import { inject, onMounted } from "vue"
import { PhFolderOpen, PhDownloadSimple, PhWarning } from "@phosphor-icons/vue"
import type { BridgeError } from "../lib/bridge"
import { TYPE_LABELS, useExportsStore } from "../stores/exports"

const store = useExportsStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})

onMounted(() => { store.init().catch((error) => toast((error as BridgeError).message ?? String(error), "destructive")) })

const inputClass =
  "rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:ring-2 focus:ring-primary"

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

function onExport() {
  run(async () => {
    await store.exportData()
    toast("导出完成", "success")
  })
}

function onExportIssues() {
  run(async () => {
    await store.exportIssues()
    toast("问题报告已导出", "success")
  })
}
</script>

<template>
  <div>
    <h1 class="mb-6 text-xl font-semibold">导出中心</h1>

    <section class="mb-6 rounded-lg border border-border bg-background p-4 shadow-card">
      <div class="flex flex-wrap items-center gap-3">
        <select v-model="store.setId" :class="inputClass" class="min-w-56">
          <option v-for="set in store.sets" :key="set.id" :value="set.id">
            {{ set.name }} · 已批准 {{ set.approved_count ?? 0 }}
          </option>
        </select>
        <input :value="store.outputDir" :class="inputClass" class="min-w-0 flex-1" readonly placeholder="导出目录" />
        <button class="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="run(() => store.pickDirectory())">
          <PhFolderOpen :size="16" /> 选择目录
        </button>
        <label class="flex items-center gap-1 text-sm">
          <input type="checkbox" v-model="store.split" /> 按学科拆分导出（每个学科一个文件，便于单独维护上传）
        </label>
      </div>
      <div class="mt-3 flex flex-wrap gap-2">
        <button class="rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary disabled:opacity-50"
          :disabled="!store.setId || store.exporting" @click="run(() => store.exportIncremental())">自动检查并导出变更题</button>
        <button
          class="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover disabled:opacity-50"
          :disabled="!store.setId || store.exporting" @click="onExport"
        ><PhDownloadSimple :size="16" /> {{ store.exporting ? "正在导出…" : "导出完整 JSON、XLSX 与解析映射" }}</button>
        <button
          class="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
          :disabled="!store.setId || store.exporting" @click="onExportIssues"
        ><PhWarning :size="16" /> 导出问题报告</button>
        <button class="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="run(() => store.openFolder())">
          <PhFolderOpen :size="16" /> 打开导出目录
        </button>
      </div>
      <pre
        v-if="store.resultLines.length"
        class="mt-4 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-md border border-success/40 bg-success/5 p-3 text-sm"
      >{{ store.resultLines.join("\n\n") }}</pre>
    </section>

    <section class="rounded-lg border border-border bg-background p-4 shadow-card">
      <h2 class="mb-3 text-base font-semibold">导出历史</h2>
      <ul class="space-y-2 text-sm">
        <li v-for="record in store.history" :key="record.id" class="rounded-md border border-border p-2" :title="record.output_path">
          <div class="flex items-center gap-2">
            <span class="text-foreground-secondary">{{ record.created_at }}</span>
            <span class="font-medium">{{ TYPE_LABELS[record.export_type] ?? record.export_type }}</span>
            <span class="text-foreground-secondary">· {{ record.item_count }} 题</span>
          </div>
          <div class="truncate text-xs text-foreground-secondary">{{ record.output_path }}</div>
        </li>
      </ul>
      <div v-if="!store.history.length" class="py-6 text-center text-sm text-foreground-secondary">
        还没有导出记录。
      </div>
    </section>
  </div>
</template>
