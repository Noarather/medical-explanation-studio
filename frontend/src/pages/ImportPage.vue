<script setup lang="ts">
import { computed, inject, nextTick, onMounted, ref } from "vue"
import { PhFileArrowUp, PhFolderOpen } from "@phosphor-icons/vue"
import type { BridgeError } from "../lib/bridge"
import { useImportsStore } from "../stores/imports"
import OrganizePanel from "../components/OrganizePanel.vue"
import UpgradeCard from "../components/UpgradeCard.vue"
import BatchImportPanel from "../components/BatchImportPanel.vue"
import FileImportReview from "../components/FileImportReview.vue"

const store = useImportsStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})
const importError = ref("")
const picking = ref(false)

onMounted(() => { store.refreshSets().catch((error) => toast((error as BridgeError).message ?? String(error), "destructive")) })

const isExcel = computed(() => store.inspect?.kind === "excel")
const mappingFields = computed(() => store.inspect?.fields ?? [])

async function onPick() {
  importError.value = ""
  picking.value = true
  try {
    await store.pickFile()
  } catch (error) {
    importError.value = (error as BridgeError).message ?? String(error)
  } finally {
    picking.value = false
  }
}

async function onImport() {
  importError.value = ""
  try {
    if (await store.importFile()) toast(`已导入 ${store.lastImported?.question_count ?? 0} 道题`, "success")
    else {
      await nextTick()
      document.querySelector('[aria-label="导入题目即时处理"]')?.scrollIntoView?.({behavior: 'smooth', block: 'start'})
    }
  } catch (error) {
    importError.value = (error as BridgeError).message ?? String(error)
  }
}

async function resolveImport(action: "edit" | "remove" | "restore", index: number, row?: Record<string, unknown>) {
  importError.value = ""
  try { await store.resolveFileImport(action, index, row) }
  catch (error) { importError.value = (error as BridgeError).message ?? String(error) }
}
async function commitImport() {
  importError.value = ""
  try {
    await store.commitFileImport()
    toast(`已导入 ${store.lastImported?.question_count ?? 0} 道题`, "success")
  } catch (error) { importError.value = (error as BridgeError).message ?? String(error) }
}

const inputClass =
  "rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:ring-2 focus:ring-primary"
</script>

<template>
  <div>
    <h1 class="mb-6 text-xl font-semibold">题目导入</h1>
    <BatchImportPanel />

    <section class="mb-6 rounded-lg border border-border bg-background p-4 shadow-card">
      <h2 class="mb-3 text-base font-semibold">从文件导入（JSON / Excel）</h2>
      <div class="flex flex-wrap items-center gap-3">
        <button
          class="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm transition-colors duration-fast hover:bg-muted"
          :disabled="picking || store.importing || !!store.fileReview"
          @click="onPick"
        >
          <PhFolderOpen :size="16" /> 选择题目文件
        </button>
        <span class="min-w-0 flex-1 truncate text-sm text-foreground-secondary">
          {{ store.filePath || "支持标准 JSON、章节 JSON 与 .xlsx/.xlsm" }}
        </span>
        <input v-model="store.setName" :disabled="store.importing || !!store.fileReview" :class="inputClass" placeholder="题目集名称" style="width: 220px" />
        <button
          class="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary transition-colors duration-fast hover:bg-primary-hover disabled:opacity-50"
          :disabled="!store.inspect || store.importing || !!store.fileReview"
          @click="onImport"
        >
          <PhFileArrowUp :size="16" /> {{ store.importing ? "正在导入…" : "导入" }}
        </button>
      </div>

      <div v-if="isExcel && mappingFields.length" class="mt-4">
        <h3 class="mb-2 text-sm font-medium">Excel 字段映射</h3>
        <div class="grid grid-cols-2 gap-x-6 gap-y-2 lg:grid-cols-3 2xl:grid-cols-4">
          <label v-for="field in mappingFields" :key="field" class="flex items-center gap-2 text-sm">
            <span class="w-28 shrink-0 truncate text-foreground-secondary">{{ field }}</span>
            <select v-model="store.mapping[field]" :disabled="store.importing || !!store.fileReview" :class="inputClass" class="min-w-0 flex-1">
              <option value="">不导入</option>
              <option v-for="header in store.inspect?.headers ?? []" :key="header" :value="header">
                {{ header }}
              </option>
            </select>
          </label>
        </div>
      </div>

      <pre
        v-if="importError"
        class="mt-4 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive"
      >{{ importError }}</pre>

      <FileImportReview v-if="store.fileReview" :review="store.fileReview" :busy="store.importing"
        @resolve="resolveImport" @commit="commitImport" @discard="store.fileReview = null; importError = ''" />

      <div
        v-if="store.lastImported"
        class="mt-4 rounded-md border border-success/40 bg-success/5 p-3 text-sm text-success"
      >
        已导入「{{ store.lastImported.name }}」共 {{ store.lastImported.question_count }} 道题。
        <span v-if="store.lastImported.removed_count">已排除 {{ store.lastImported.removed_count }} 道题，原文件未修改。</span>
        可在“题目管理”浏览，或前往“审核工作台”生成并审核解析。
      </div>
    </section>

    <OrganizePanel />
    <UpgradeCard />
  </div>
</template>
