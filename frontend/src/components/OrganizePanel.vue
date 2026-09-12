<script setup lang="ts">
import { computed, inject, ref } from "vue"
import { PhMagicWand, PhTrash } from "@phosphor-icons/vue"
import { useOrganizeStore } from "../stores/organize"

const store = useOrganizeStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})

const bulkField = ref("subject")
const bulkValue = ref("")

const inputClass =
  "rounded-md border border-border bg-background px-2 py-1 text-sm outline-none focus:ring-2 focus:ring-primary"

const bulkValueSuggestions = computed(() => {
  if (bulkField.value === "bank") return ["school", "kaoyan"]
  if (bulkField.value === "type") return ["A1", "A2", "A3", "multiple", "judge", "fill"]
  if (bulkField.value === "difficulty") return ["easy", "medium", "hard"]
  const values = new Set<string>()
  for (const row of store.rows) {
    const raw = row[bulkField.value]
    const text = Array.isArray(raw) ? raw.join("、") : String(raw ?? "").trim()
    if (text) values.add(text)
  }
  if (bulkField.value === "subject" && store.subject.trim()) values.add(store.subject.trim())
  return [...values].sort()
})

function cellText(row: Record<string, unknown>, field: string): string {
  const value = row[field]
  if (field === "answer" && row.type === "fill" && Array.isArray(value)) return JSON.stringify(value)
  return Array.isArray(value) ? value.join(field === "options" ? " | " : "、") : String(value ?? "")
}

function onCellEdit(index: number, field: string, event: Event) {
  store.updateCell(index, field, (event.target as HTMLInputElement).value)
}

function run(action: () => unknown) {
  try {
    const result = action()
    if (result instanceof Promise) result.catch((error) => toast(error?.message ?? String(error), "destructive"))
  } catch (error) {
    toast((error as Error).message, "destructive")
  }
}

const columns = [
  { field: "id", label: "ID", width: "w-32" },
  { field: "subject", label: "学科", width: "w-24" },
  { field: "bank", label: "题库", width: "w-24" },
  { field: "type", label: "题型", width: "w-20" },
  { field: "question", label: "题干", width: "w-72" },
  { field: "options", label: "选项", width: "w-56" },
  { field: "answer", label: "答案", width: "w-16" },
  { field: "system", label: "章节", width: "w-28" },
  { field: "tags", label: "题目标签", width: "w-36" },
]
</script>

<template>
  <section class="mb-6 rounded-lg border border-border bg-background p-4 shadow-card">
    <h2 class="mb-3 text-base font-semibold">智能整理题目</h2>

    <div class="flex flex-col gap-4 lg:flex-row">
      <div class="flex min-w-0 flex-col gap-2 lg:w-2/5">
        <textarea
          v-model="store.rawText"
          class="h-64 rounded-md border border-border bg-background p-2 text-sm outline-none focus:ring-2 focus:ring-primary"
          :readonly="!!store.sourcePath"
          placeholder="在此粘贴题干、选项、答案和已有解析"
        />
        <div class="flex flex-wrap items-center gap-2">
          <input v-model="store.name" :class="inputClass" placeholder="题目集名称" :disabled="!!store.partialSetId" />
          <input v-model="store.subject" :class="inputClass" placeholder="默认学科" />
          <select v-model="store.bank" :class="inputClass" aria-label="整理缺省题库"><option value="">请选择题库</option><option value="school">校内</option><option value="kaoyan">考研</option></select>
          <label class="flex items-center gap-1 text-sm" :class="store.sourcePath ? 'opacity-50' : ''">
            <input type="checkbox" v-model="store.useAI" :disabled="!!store.sourcePath" />
            本地规则无法识别时使用所选模型
          </label>
          <button class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="run(() => store.pickSource())">
            读取 TXT / Markdown / Word / XLSX
          </button>
        </div>
      </div>

      <div class="min-w-0 flex-1">
        <div class="mb-2 flex flex-wrap items-center gap-2 text-sm">
          <span class="font-medium">结构化预览</span>
          <span class="text-foreground-secondary">共 {{ store.rows.length }} 道 · 待调整 {{ store.invalidCount }}</span>
          <span class="flex-1" />
          <select v-model="store.filter" :class="inputClass" @change="store.page = 0">
            <option value="invalid">待调整</option>
            <option value="valid">可导入</option>
            <option value="all">全部</option>
          </select>
          <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="store.page === 0" @click="store.page -= 1">上一页</button>
          <span>第 {{ store.page + 1 }} / {{ store.pageCount }} 页</span>
          <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="store.page >= store.pageCount - 1" @click="store.page += 1">下一页</button>
        </div>

        <div class="mb-2 flex flex-wrap items-center gap-2 text-sm">
          <select v-model="bulkField" :class="inputClass">
            <option value="subject">学科</option>
            <option value="bank">题库</option>
            <option value="type">题型</option>
            <option value="system">章节</option>
            <option value="difficulty">难度</option>
            <option value="tags">题目标签</option>
          </select>
          <input v-model="bulkValue" :class="inputClass" list="organize-bulk-values" placeholder="批量设置值" />
          <datalist id="organize-bulk-values">
            <option v-for="value in bulkValueSuggestions" :key="value" :value="value" />
          </datalist>
          <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" @click="run(() => store.applyBulk(bulkField, bulkValue, 'selected'))">应用到选中</button>
          <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" @click="run(() => store.applyBulk(bulkField, bulkValue, 'filtered'))">应用到当前列表</button>
          <button class="rounded-md border border-border px-2 py-1 hover:bg-muted disabled:opacity-50" :disabled="!store.selected.length" @click="run(() => store.acknowledgeSourceIssues())">已核对选中题原文，重新校验</button>
        </div>

        <div class="max-h-96 overflow-auto rounded-md border border-border">
          <table class="w-full border-collapse text-sm">
            <thead class="sticky top-0 bg-muted">
              <tr>
                <th class="w-8 px-2 py-1.5" />
                <th class="px-2 py-1.5 text-left font-medium">校验</th>
                <th v-for="column in columns" :key="column.field" class="px-2 py-1.5 text-left font-medium">{{ column.label }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="index in store.pageIndices" :key="index" class="border-t border-border">
                <td class="px-2 py-1">
                  <input type="checkbox" :checked="store.selected.includes(index)" @change="store.toggleSelected(index)" />
                </td>
                <td class="px-2 py-1" :title="(store.warnings[index] ?? []).join('\n')">
                  <span :class="(store.warnings[index]?.length ?? 0) > 0 ? 'text-warning' : 'text-success'">
                    {{ (store.warnings[index]?.length ?? 0) > 0 ? "需补全" : "可导入" }}
                  </span>
                </td>
                <td v-for="column in columns" :key="column.field" class="px-1 py-1">
                  <input
                    :class="[inputClass, column.width]"
                    :value="cellText(store.rows[index], column.field)"
                    @change="onCellEdit(index, column.field, $event)"
                  />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="mt-3 flex flex-wrap items-center gap-2">
      <span class="min-w-0 flex-1 truncate text-sm text-foreground-secondary">{{ store.status }}</span>
      <div v-if="store.running" class="h-1.5 w-40 overflow-hidden rounded bg-muted">
        <div
          class="h-full bg-primary transition-all duration-base"
          :style="{ width: store.progressTotal > 0 ? `${Math.round((store.progressCurrent / store.progressTotal) * 100)}%` : '40%' }"
        />
      </div>
      <button
        class="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover disabled:opacity-50"
        :disabled="store.running"
        @click="run(() => store.organize())"
      >
        <PhMagicWand :size="16" /> {{ store.running ? "正在整理…" : "自动整理" }}
      </button>
      <button class="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" :disabled="!store.selected.length" @click="run(() => store.deleteSelected())">
        <PhTrash :size="16" /> 删除选中
      </button>
      <button class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="run(() => store.exportJson())">导出可用 JSON</button>
      <button class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="run(() => store.exportIssues())">导出待调整报告</button>
      <button
        class="rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover disabled:opacity-50"
        :disabled="!store.validCount || store.importing"
        @click="run(() => store.importValid())"
      >
        导入全部可用
      </button>
    </div>
  </section>
</template>
