<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue"
import { PhMagnifyingGlass, PhTrash } from "@phosphor-icons/vue"
import { invoke, type BridgeError } from "../lib/bridge"
import LibraryPickerModal from "../components/review/LibraryPickerModal.vue"
import { STATUS_OPTIONS, useQuestionsStore } from "../stores/questions"
import QuestionDetailDrawer from "../components/QuestionDetailDrawer.vue"

const store = useQuestionsStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})

const statusLabel = computed({
  get: () => {
    const option = STATUS_OPTIONS.find(
      (item) => item.review_status === store.filters.review_status
        && item.pipeline_status === store.filters.pipeline_status,
    )
    return option?.label ?? "全部状态"
  },
  set: (label: string) => {
    const option = STATUS_OPTIONS.find((item) => item.label === label) ?? STATUS_OPTIONS[0]
    run(() => store.applyStatusOption(option))
  },
})
const bulkSubject = ref("")
const confirmDelete = ref(false)
const confirmPurge = ref(false)
interface GenerationPreview { set_id: string; name: string; total: number; count: number; skipped: number; previous: number; reviewed: number; active: boolean; token: string }
const generationPreview = ref<GenerationPreview | null>(null)
const generationBusy = ref(false)
const generationConsent = ref(false)
const generationPicker = ref(false)

async function previewGeneration() {
  generationBusy.value = true
  generationPreview.value = null
  generationConsent.value = false
  try {
    generationPreview.value = await invoke<GenerationPreview>("questions", "first_generation_preview", { set_id: store.filters.set_id })
  } catch (error) {
    toast((error as BridgeError).message ?? String(error), "destructive")
  } finally { generationBusy.value = false }
}

async function startGeneration(libraryIds: number[]) {
  const preview = generationPreview.value
  if (!preview || generationBusy.value || !generationConsent.value) return
  generationBusy.value = true
  try {
    const result = await invoke<{ count: number }>("questions", "first_generation_start", {
      set_id: preview.set_id, token: preview.token, library_ids: libraryIds, consent: generationConsent.value,
    })
    generationPreview.value = null
    toast(`已提交 ${result.count} 道题，请在右上角“任务”查看进度，在“审核工作台”查看解析。`, "success")
  } catch (error) {
    toast((error as BridgeError).message ?? String(error), "destructive")
  } finally { generationBusy.value = false }
}

onMounted(() => { store.init().catch((error) => toast((error as BridgeError).message ?? String(error), "destructive")) })

const inputClass =
  "rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:ring-2 focus:ring-primary"

const PIPELINE_LABELS: Record<string, string> = {
  queued: "等待生成", generated: "已生成", unmatched: "未匹配",
  no_library: "缺少教材", error: "处理失败",
}
const REVIEW_LABELS: Record<string, string> = {
  pending: "待审核", approved: "已批准", rejected: "已驳回",
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

function onBulkSubject() {
  if (!bulkSubject.value.trim()) {
    toast("请填写学科名称", "destructive")
    return
  }
  run(async () => {
    const updated = await store.bulkSetSubject(bulkSubject.value)
    toast(`已更新 ${updated} 道题的学科（题目已回到等待生成）`, "success")
  })
}

function onDelete() {
  run(async () => {
    await store.deleteSelected()
    confirmDelete.value = false
    toast("已永久删除所选题目", "success")
  })
}

function onRestore() {
  run(async () => {
    const result = await store.restoreSelected()
    if (result.conflicts.length) {
      toast(`恢复 ${result.restored} 道；${result.conflicts.length} 道冲突保留在回收站（${result.conflicts.join("、")}）`, "default")
    } else {
      toast(`已恢复 ${result.restored} 道题`, "success")
    }
  })
}

function onPurge() {
  run(async () => {
    await store.purgeSelected()
    confirmPurge.value = false
    toast("已彻底清除所选题目", "success")
  })
}
</script>

<template>
  <div>
    <h1 class="mb-6 text-xl font-semibold">题目管理</h1>

    <div class="mb-4 flex gap-1 border-b border-border">
      <button
        class="rounded-t-md px-4 py-2 text-sm transition-colors duration-fast"
        :class="store.tab === 'questions' ? 'bg-background font-medium text-primary shadow-card' : 'text-foreground-secondary hover:bg-muted'"
        @click="store.tab = 'questions'"
      >全部题目</button>
      <button
        class="rounded-t-md px-4 py-2 text-sm transition-colors duration-fast"
        :class="store.tab === 'trash' ? 'bg-background font-medium text-primary shadow-card' : 'text-foreground-secondary hover:bg-muted'"
        @click="store.tab = 'trash'; run(() => store.loadTrash())"
      >回收站</button>
    </div>

    <template v-if="store.tab === 'questions'">
      <div class="mb-4 rounded-lg border border-border bg-background p-3 text-sm">
        <button data-testid="first-generation" class="rounded-md bg-primary px-3 py-2 text-onprimary disabled:opacity-50"
          :disabled="!store.filters.set_id || generationBusy" @click="previewGeneration">整批生成／重新生成解析</button>
        <span class="ml-3 text-foreground-secondary">先选择下方导入批次；覆盖该批次全部分页，不受搜索、筛选或勾选范围影响。</span>
        <div v-if="generationPreview" class="mt-3 space-y-2">
          <p>批次：{{ generationPreview.name }} · 共 {{ generationPreview.total }} 题；本次将重新生成 {{ generationPreview.count }} 题，跳过 {{ generationPreview.skipped }} 题。</p>
          <p>其中 {{ generationPreview.previous }} 题已有解析、{{ generationPreview.reviewed }} 题已审核。新结果将替换原解析，审核状态将重置为待审核。</p>
          <p v-if="generationPreview.active" class="text-destructive">本批次已有生成任务，请先在任务中心处理。</p>
          <label class="flex items-center gap-2"><input v-model="generationConsent" type="checkbox" data-testid="generation-consent" />我确认重新生成整个批次，已有解析及审核状态会更新；题目与候选教材内容将发送至云端模型并产生费用。</label>
          <button data-testid="generation-choose" class="rounded-md bg-primary px-3 py-2 text-onprimary disabled:opacity-50"
            :disabled="generationBusy || !generationConsent || !generationPreview.count || generationPreview.active"
            @click="generationPicker = true">选择教材并重新生成</button>
          <button class="ml-2 rounded-md border border-border px-3 py-2" :disabled="generationBusy" @click="generationPreview = null">取消</button>
        </div>
      </div>
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <select v-model="store.filters.set_id" :class="inputClass" @change="run(() => store.applyFilters())">
          <option value="">全部批次</option>
          <option v-for="set in store.filterOptions.sets" :key="set.id" :value="set.id">
            {{ set.name }}（{{ set.question_count }}）
          </option>
        </select>
        <select v-model="statusLabel" :class="inputClass">
          <option v-for="option in STATUS_OPTIONS" :key="option.label" :value="option.label">{{ option.label }}</option>
        </select>
        <select v-model="store.filters.subject" :class="inputClass" @change="run(() => store.applyFilters())">
          <option value="">全部学科</option>
          <option v-for="subject in store.filterOptions.subjects" :key="subject" :value="subject">{{ subject }}</option>
        </select>
        <select v-model="store.filters.tag" :class="inputClass" @change="run(() => store.applyFilters())">
          <option value="">全部标签</option>
          <option v-for="tag in store.filterOptions.tags" :key="tag" :value="tag">{{ tag }}</option>
        </select>
        <select v-model="store.filters.question_source" :class="inputClass" @change="run(() => store.applyFilters())">
          <option value="">全部来源</option>
          <option v-for="source in store.filterOptions.sources" :key="source" :value="source">{{ source }}</option>
        </select>
        <input
          v-model="store.filters.search" :class="inputClass" placeholder="搜索题目 ID 或题干"
          style="width: 200px" @keyup.enter="run(() => store.applyFilters())"
        />
        <button class="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="run(() => store.applyFilters())">
          <PhMagnifyingGlass :size="16" /> 搜索
        </button>
      </div>

      <div class="mb-3 flex flex-wrap items-center gap-2 text-sm">
        <span class="text-foreground-secondary">已选 {{ store.selected.length }} 题 / 共 {{ store.total }} 题</span>
        <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" @click="store.selectPage()">全选本页</button>
        <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" @click="run(() => store.selectAllFiltered())">选择全部筛选结果</button>
        <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="!store.selected.length" @click="store.clearSelection()">清除选择</button>
        <span class="flex-1" />
        <input v-model="bulkSubject" :class="inputClass" list="questions-subject-options" placeholder="批量学科" />
        <datalist id="questions-subject-options">
          <option v-for="subject in store.filterOptions.subjects" :key="subject" :value="subject" />
        </datalist>
        <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="!store.selected.length" @click="onBulkSubject">设置学科</button>
        <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="!store.selected.length" @click="run(() => store.exportSelected())">导出选中</button>
        <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="!store.selected.length" @click="run(() => store.moveSelectedToTrash())">移入回收站</button>
        <button
          class="flex items-center gap-1 rounded-md border border-destructive/50 px-2 py-1 text-destructive hover:bg-destructive/5"
          :disabled="!store.selected.length" @click="confirmDelete = true"
        ><PhTrash :size="16" /> 永久删除</button>
      </div>

      <div v-if="confirmDelete" class="mb-3 flex items-center gap-3 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
        <span>确定永久删除所选 {{ store.selected.length }} 道题及其解析、证据和审核记录吗？此操作无法恢复。</span>
        <button class="rounded-md bg-destructive px-3 py-1 text-onprimary" @click="onDelete">永久删除</button>
        <button class="rounded-md border border-border px-3 py-1" @click="confirmDelete = false">取消</button>
      </div>

      <div class="overflow-auto rounded-lg border border-border bg-background shadow-card" style="max-height: 60vh">
        <table class="w-full border-collapse text-sm">
          <thead class="sticky top-0 bg-muted">
            <tr>
              <th class="w-8 px-2 py-1.5" />
              <th class="px-2 py-1.5 text-left font-medium">ID</th>
              <th class="px-2 py-1.5 text-left font-medium">学科</th>
              <th class="px-2 py-1.5 text-left font-medium">题型</th>
              <th class="px-2 py-1.5 text-left font-medium">题干</th>
              <th class="px-2 py-1.5 text-left font-medium">来源</th>
              <th class="px-2 py-1.5 text-left font-medium">状态</th>
              <th class="px-2 py-1.5 text-left font-medium">更新时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in store.rows" :key="row.id" class="cursor-pointer border-t border-border hover:bg-muted/60" @click="run(() => store.openDetail(row.id))">
              <td class="px-2 py-1.5" @click.stop>
                <input type="checkbox" :checked="store.selected.includes(row.id)" @change="store.toggleSelected(row.id)" />
              </td>
              <td class="max-w-40 truncate px-2 py-1.5">{{ row.external_id }}</td>
              <td class="px-2 py-1.5">{{ row.subject }}</td>
              <td class="px-2 py-1.5">{{ row.question_type }}</td>
              <td class="max-w-md truncate px-2 py-1.5">{{ row.prompt_text }}</td>
              <td class="max-w-32 truncate px-2 py-1.5 text-foreground-secondary">{{ row.question_source || "—" }}</td>
              <td class="whitespace-nowrap px-2 py-1.5">
                <span>{{ PIPELINE_LABELS[row.pipeline_status] ?? row.pipeline_status }}</span>
                <span
                  class="ml-1 rounded px-1 text-xs"
                  :class="row.review_status === 'approved' ? 'bg-success/10 text-success' : row.review_status === 'rejected' ? 'bg-destructive/10 text-destructive' : 'bg-warning/10 text-warning'"
                >{{ REVIEW_LABELS[row.review_status] ?? row.review_status }}</span>
              </td>
              <td class="whitespace-nowrap px-2 py-1.5 text-foreground-secondary">{{ row.updated_at }}</td>
            </tr>
            <tr v-if="!store.rows.length">
              <td colspan="8" class="px-4 py-8 text-center text-foreground-secondary">
                {{ store.loading ? "加载中…" : "没有符合筛选条件的题目。" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="mt-3 flex items-center gap-3 text-sm">
        <span class="text-foreground-secondary">共 {{ store.total }} 条</span>
        <span class="flex-1" />
        <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="store.page === 0" @click="run(() => store.changePage(-1))">上一页</button>
        <span>第 {{ store.page + 1 }} / {{ store.pageCount }} 页</span>
        <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="store.page >= store.pageCount - 1" @click="run(() => store.changePage(1))">下一页</button>
      </div>
    </template>

    <template v-else>
      <p class="mb-3 text-sm text-foreground-secondary">删除的题目保留 30 天，到期自动清除；恢复会还原题目、解析、证据与审核记录。</p>
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <input
          v-model="store.trashSearch" :class="inputClass" placeholder="搜索题目 ID、学科或题目集"
          style="width: 220px" @keyup.enter="run(() => { store.trashPage = 0; return store.loadTrash() })"
        />
        <button class="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="run(() => { store.trashPage = 0; return store.loadTrash() })">
          <PhMagnifyingGlass :size="16" /> 搜索
        </button>
        <span class="flex-1" />
        <span class="text-sm text-foreground-secondary">已选 {{ store.trashSelected.length }} 题 / 共 {{ store.trashTotal }} 题</span>
        <button class="rounded-md border border-border px-2 py-1 text-sm hover:bg-muted" :disabled="!store.trashSelected.length" @click="onRestore">恢复所选</button>
        <button
          class="flex items-center gap-1 rounded-md border border-destructive/50 px-2 py-1 text-sm text-destructive hover:bg-destructive/5"
          :disabled="!store.trashSelected.length" @click="confirmPurge = true"
        ><PhTrash :size="16" /> 彻底清除</button>
      </div>

      <div v-if="confirmPurge" class="mb-3 flex items-center gap-3 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
        <span>彻底清除所选 {{ store.trashSelected.length }} 道题？将从回收站移除且无法恢复。</span>
        <button class="rounded-md bg-destructive px-3 py-1 text-onprimary" @click="onPurge">彻底清除</button>
        <button class="rounded-md border border-border px-3 py-1" @click="confirmPurge = false">取消</button>
      </div>

      <div class="overflow-auto rounded-lg border border-border bg-background shadow-card" style="max-height: 60vh">
        <table class="w-full border-collapse text-sm">
          <thead class="sticky top-0 bg-muted">
            <tr>
              <th class="w-8 px-2 py-1.5" />
              <th class="px-2 py-1.5 text-left font-medium">题目 ID</th>
              <th class="px-2 py-1.5 text-left font-medium">学科</th>
              <th class="px-2 py-1.5 text-left font-medium">题干</th>
              <th class="px-2 py-1.5 text-left font-medium">所属题目集</th>
              <th class="px-2 py-1.5 text-left font-medium">删除时间</th>
              <th class="px-2 py-1.5 text-left font-medium">自动清除</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in store.trashRows" :key="row.id" class="border-t border-border">
              <td class="px-2 py-1.5">
                <input type="checkbox" :checked="store.trashSelected.includes(row.id)" @change="store.toggleTrashSelected(row.id)" />
              </td>
              <td class="max-w-40 truncate px-2 py-1.5">{{ row.external_id }}</td>
              <td class="px-2 py-1.5">{{ row.subject }}</td>
              <td class="max-w-md truncate px-2 py-1.5">{{ row.prompt_text }}</td>
              <td class="px-2 py-1.5">{{ row.set_name }}</td>
              <td class="whitespace-nowrap px-2 py-1.5 text-foreground-secondary">{{ String(row.deleted_at ?? "").slice(0, 19).replace("T", " ") }}</td>
              <td class="whitespace-nowrap px-2 py-1.5 text-foreground-secondary">{{ String(row.purge_after ?? "").slice(0, 10) }}</td>
            </tr>
            <tr v-if="!store.trashRows.length">
              <td colspan="7" class="px-4 py-8 text-center text-foreground-secondary">回收站为空。</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="mt-3 flex items-center gap-3 text-sm">
        <span class="text-foreground-secondary">共 {{ store.trashTotal }} 条</span>
        <span class="flex-1" />
        <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="store.trashPage === 0" @click="run(() => { store.trashPage -= 1; return store.loadTrash() })">上一页</button>
        <span>第 {{ store.trashPage + 1 }} / {{ store.trashPageCount }} 页</span>
        <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="store.trashPage >= store.trashPageCount - 1" @click="run(() => { store.trashPage += 1; return store.loadTrash() })">下一页</button>
      </div>
    </template>

    <QuestionDetailDrawer v-model:open="store.detailOpen" />
    <LibraryPickerModal v-model:open="generationPicker" @confirm="startGeneration" />
  </div>
</template>
