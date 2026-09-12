<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue"
import { PhArrowClockwise, PhCheck, PhFloppyDisk, PhMagnifyingGlass, PhX } from "@phosphor-icons/vue"
import { invoke, type BridgeError } from "../lib/bridge"
import { useReviewStore } from "../stores/review"
import BlockEditor from "../components/review/BlockEditor.vue"
import EvidencePanel from "../components/review/EvidencePanel.vue"
import LibraryPickerModal from "../components/review/LibraryPickerModal.vue"
import QuickReviewModal from "../components/review/QuickReviewModal.vue"

const store = useReviewStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})
const openJobs = inject<() => void>("openJobs", () => {})

onMounted(() => { store.init().catch((error) => toast((error as BridgeError).message ?? String(error), "destructive")) })

const inputClass =
  "rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:ring-2 focus:ring-primary"

const STATUS_OPTIONS = [
  { label: "已生成待审核", review: "pending", pipeline: "generated" },
  { label: "全部题目", review: "", pipeline: "" },
  { label: "等待生成", review: "pending", pipeline: "queued" },
  { label: "已批准", review: "approved", pipeline: "" },
  { label: "已驳回", review: "rejected", pipeline: "" },
  { label: "未匹配", review: "", pipeline: "unmatched" },
  { label: "缺少教材", review: "", pipeline: "no_library" },
  { label: "处理失败", review: "", pipeline: "error" },
  { label: "已匹配·生成失败", review: "", pipeline: "matched_generation_error" },
]
const statusLabel = computed({
  get: () => STATUS_OPTIONS.find(
    (option) => option.review === store.reviewStatus && option.pipeline === store.pipelineStatus,
  )?.label ?? "全部题目",
  set: (label: string) => {
    const option = STATUS_OPTIONS.find((item) => item.label === label) ?? STATUS_OPTIONS[1]
    store.reviewStatus = option.review
    store.pipelineStatus = option.pipeline
    onApplyFilters()
  },
})

const raw = computed(() => store.raw)
const canApprove = computed(() => store.currentId !== null)

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

function confirmLeaveDirty(next: () => void) {
  if (!store.dirty) {
    next()
    return
  }
  if (window.confirm("当前题目的解析已修改。\n确定 = 保存修改后继续；取消 = 留在当前题。")) {
    run(async () => {
      await store.save()
      next()
    })
  }
}

function onOpen(id: number) {
  if (id === store.currentId) return
  confirmLeaveDirty(() => run(() => store.open(id)))
}

function onApplyFilters() {
  confirmLeaveDirty(() => run(() => store.applyFilters()))
}

function onChangePage(delta: number) {
  confirmLeaveDirty(() => run(() => store.changePage(delta)))
}

function onReject() {
  if (!store.skipRejectConfirm && !window.confirm("确定驳回当前题目的解析并继续下一题吗？")) return
  run(() => store.review("rejected"))
}

function splitTags(value: string): string[] {
  return value.split(/[、,，;；|]+/).map((item) => item.trim()).filter(Boolean).slice(0, 3)
}

const tagsText = computed({
  get: () => store.fields.tags.join("、"),
  set: (value: string) => { store.fields.tags = splitTags(value); store.markDirty() },
})
const knowledgePointsText = computed({
  get: () => store.fields.knowledgePoints.join("、"),
  set: (value: string) => { store.fields.knowledgePoints = splitTags(value); store.markDirty() },
})
const suggestedTagsText = computed({
  get: () => store.fields.suggestedTags.join("、"),
  set: (value: string) => { store.fields.suggestedTags = splitTags(value); store.markDirty() },
})

const pickerOpen = ref(false)
const batchSubject = ref("")

function onBatchSubject() {
  if (!batchSubject.value.trim()) {
    toast("请选择或填写学科名称", "destructive")
    return
  }
  run(async () => {
    await store.batchSetSubject(batchSubject.value)
    toast("已设置学科（题目已回到等待生成）", "success")
  })
}

function onPickerConfirm(libraryIds: number[]) {
  run(async () => {
    await store.batchRegenerate(libraryIds)
    toast("批量重新生成已加入队列", "success")
    openJobs()
  })
}

function onBatchGeneral() {
  run(async () => {
    const count = await store.batchGeneral()
    if (!count) {
      toast("当前选择中没有未匹配或缺少教材的题目。未选择题目时会处理当前题目集的全部未匹配题。", "default")
    } else {
      toast(`批量通识生成已加入队列（${count} 题）`, "success")
      openJobs()
    }
  })
}

function onBatchDelete() {
  if (!window.confirm(`确定永久删除所选 ${store.selected.length} 道题及其解析、证据和审核记录吗？此操作无法恢复。`)) return
  run(async () => {
    await invoke("questions", "delete", { question_pks: store.selected })
    store.clearSelection()
    await store.load()
    toast("已永久删除所选题目", "success")
  })
}

const quickReviewOpen = ref(false)
const backfillOpen = ref(false)
const backfillItems = [
  { kind: "tags", label: "补齐题目标签" },
  { kind: "study_points", label: "补齐复习考点" },
  { kind: "memory_cards", label: "生成背诵知识卡" },
]

const GENERAL_DISCLAIMER =
  "【声明：以下解析未在所选教材中找到达到阈值的直接依据，仅基于通用医学知识生成，请以权威教材复核。】"

const canAuthorizeGeneral = computed(() =>
  store.detail !== null && ["unmatched", "no_library"].includes(store.detail.pipeline_status))

function onQuickApproved(result: { approved: number; skipped: number }) {
  toast(
    `已快速批准 ${result.approved} 道题` +
      (result.skipped ? `；${result.skipped} 道因状态或内容变化未处理` : ""),
    "success",
  )
}

function onAuthorizeGeneral() {
  if (!window.confirm(`解析将基于通用医学知识，并强制保留以下声明：\n\n${GENERAL_DISCLAIMER}\n\n是否继续？`)) return
  run(async () => {
    await store.authorizeGeneral()
    toast("已加入生成队列", "success")
    openJobs()
  })
}

function onBackfill(kind: string, label: string) {
  backfillOpen.value = false
  run(async () => {
    const estimate = await store.backfillEstimate(kind)
    const total = Number(estimate.repairable ?? estimate.total ?? 0)
    if (!total) {
      toast("当前题目集没有可补齐的题目。", "default")
      return
    }
    const lines = [
      `将为 ${total} 道题执行「${label}」，其中已批准 ${estimate.approved ?? 0} 道。`,
      `预计至少 ${estimate.estimated_requests ?? 0} 次模型请求。`,
      "该操作不会重写解析、答案、教材证据或审核状态，执行前会自动备份数据库。是否继续？",
    ]
    if (!window.confirm(lines.join("\n"))) return
    await store.enqueueBackfill(kind, `${label}：${total} 题`)
    toast("补齐任务已加入队列", "success")
    openJobs()
  })
}
</script>

<template>
  <div>
    <div class="mb-4 flex items-center gap-2">
      <h1 class="text-xl font-semibold">审核工作台</h1>
      <span class="text-xs text-foreground-secondary">{{ store.automatic.enabled ? '自动审核已开启；待审核队列仅保留异常题' : '自动审核已关闭' }}</span>
      <button :class="inputClass" @click="run(async () => { await store.runAutomatic(!store.automatic.enabled); await store.load() })">{{ store.automatic.enabled ? '关闭自动审核' : '开启自动审核' }}</button>
      <span class="flex-1" />
      <button
        class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
        :disabled="!store.setId" @click="quickReviewOpen = true"
      >快速审核</button>
      <div class="relative">
        <button
          class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
          :disabled="!store.setId" @click="backfillOpen = !backfillOpen"
        >补齐内容 ▾</button>
        <div v-if="backfillOpen" class="absolute right-0 top-full z-30 mt-1 w-44 rounded-md border border-border bg-background py-1 shadow-overlay">
          <button
            v-for="item in backfillItems" :key="item.kind"
            class="block w-full px-3 py-1.5 text-left text-sm hover:bg-muted"
            @click="onBackfill(item.kind, item.label)"
          >{{ item.label }}</button>
        </div>
      </div>
    </div>
    <div class="flex gap-4" style="height: calc(100vh - 140px)">
      <!-- 左栏：题目队列 -->
      <div class="flex w-72 shrink-0 flex-col rounded-lg border border-border bg-background shadow-card">
        <div class="space-y-2 border-b border-border p-3">
          <select v-model="store.setId" :class="inputClass" class="w-full" @change="onApplyFilters()">
            <option value="">全部题目集</option>
            <option v-for="set in store.sets" :key="set.id" :value="set.id">
              {{ set.name }}（{{ set.question_count }}）
            </option>
          </select>
          <select v-model="statusLabel" :class="inputClass" class="w-full">
            <option v-for="option in STATUS_OPTIONS" :key="option.label" :value="option.label">{{ option.label }}</option>
          </select>
          <div class="flex gap-1">
            <input
              v-model="store.search" :class="inputClass" class="min-w-0 flex-1" placeholder="搜索 ID 或题干"
              @keyup.enter="onApplyFilters()"
            />
            <button class="rounded-md border border-border px-2 hover:bg-muted" @click="onApplyFilters()">
              <PhMagnifyingGlass :size="16" />
            </button>
          </div>
        </div>
        <div class="min-h-0 flex-1 overflow-y-auto">
          <button
            v-for="row in store.rows" :key="row.id"
            class="block w-full border-b border-border px-3 py-2 text-left text-sm transition-colors duration-fast hover:bg-muted"
            :class="row.id === store.currentId ? 'bg-primary-soft border-l-2 border-l-primary' : ''"
            @click="onOpen(row.id)"
          >
            <div class="flex items-center justify-between gap-2">
              <span class="flex min-w-0 items-center gap-1.5">
                <input
                  type="checkbox" :checked="store.selected.includes(row.id)"
                  @click.stop @change="store.toggleSelected(row.id)"
                />
                <span class="truncate font-medium">{{ row.external_id }}</span>
              </span>
              <span class="shrink-0 text-xs text-foreground-secondary">{{ row.subject }}</span>
            </div>
            <div class="mt-0.5 truncate text-xs text-foreground-secondary">{{ row.prompt_text }}</div>
            <div v-if="row.review_reason" class="mt-1 text-xs text-foreground-secondary">{{ row.review_reason }}</div>
          </button>
          <div v-if="!store.rows.length" class="p-4 text-center text-sm text-foreground-secondary">
            {{ store.listLoading ? "加载中…" : "当前筛选下没有题目。" }}
          </div>
        </div>
        <div class="border-t border-border p-2 text-sm">
          <div class="mb-1 flex items-center gap-2 text-xs text-foreground-secondary">
            <span>已选 {{ store.selected.length }}</span>
            <button class="rounded border border-border px-1.5 py-0.5 hover:bg-muted" @click="store.selectPageRows()">全选本页</button>
            <button class="rounded border border-border px-1.5 py-0.5 hover:bg-muted" :disabled="!store.selected.length" @click="store.clearSelection()">清除</button>
          </div>
          <div class="flex flex-wrap gap-1">
            <input v-model="batchSubject" :class="inputClass" class="w-24" placeholder="批量学科" list="review-batch-subjects" />
            <datalist id="review-batch-subjects">
              <option v-for="set in store.sets" :key="set.id" :value="set.name" />
            </datalist>
            <button class="rounded border border-border px-1.5 py-0.5 hover:bg-muted disabled:opacity-50" :disabled="!store.selected.length" @click="onBatchSubject">设学科</button>
            <button class="rounded border border-border px-1.5 py-0.5 hover:bg-muted disabled:opacity-50" :disabled="!store.selected.length || !store.setId" @click="pickerOpen = true">重新生成</button>
            <button class="rounded border border-border px-1.5 py-0.5 hover:bg-muted disabled:opacity-50" :disabled="!store.setId" @click="onBatchGeneral">批量通识</button>
            <button class="rounded border border-destructive/50 px-1.5 py-0.5 text-destructive hover:bg-destructive/5 disabled:opacity-50" :disabled="!store.selected.length" @click="onBatchDelete">永久删除</button>
          </div>
        </div>
        <div class="flex items-center justify-between border-t border-border px-3 py-2 text-xs text-foreground-secondary">
          <span>共 {{ store.total }} 题</span>
          <span>
            <button class="disabled:opacity-40" :disabled="store.page === 0" @click="onChangePage(-1)">‹</button>
            {{ store.page + 1 }} / {{ store.pageCount }}
            <button class="disabled:opacity-40" :disabled="store.page >= store.pageCount - 1" @click="onChangePage(1)">›</button>
          </span>
        </div>
      </div>

      <!-- 中栏：题目与解析编辑 -->
      <div class="min-w-0 flex-1 overflow-y-auto rounded-lg border border-border bg-background p-4 shadow-card">
        <template v-if="store.detail">
          <div class="mb-2 flex flex-wrap items-center gap-2 text-sm text-foreground-secondary">
            <span class="font-medium text-foreground">{{ store.detail.external_id }}</span>
            <span>题型：{{ store.detail.question_type }}</span>
            <label class="flex items-center gap-1">
              学科：
              <input v-model="store.subject" :class="inputClass" style="width: 110px" @change="store.markDirty()" />
            </label>
            <span v-if="store.dirty" class="text-warning">未保存修改</span>
          </div>
          <div v-if="raw.caseInfo" class="mb-3 rounded-md bg-surface p-3 text-sm">
            <div class="mb-1 text-xs text-foreground-secondary">病例背景</div>
            <p class="whitespace-pre-wrap">{{ raw.caseInfo }}</p>
          </div>
          <p class="mb-2 whitespace-pre-wrap text-sm">{{ raw.question }}</p>
          <ul v-if="Array.isArray(raw.options) && raw.options.length" class="mb-2 space-y-1 text-sm">
            <li v-for="(option, index) in raw.options" :key="index">{{ option }}</li>
          </ul>
          <p class="mb-4 text-sm">答案：<span class="font-medium">{{ Array.isArray(raw.answer) ? raw.answer.join("、") : raw.answer }}</span></p>

          <div class="mb-3 grid grid-cols-2 gap-2 text-sm">
            <label class="flex items-center gap-1">标签
              <input v-model="tagsText" :class="inputClass" class="min-w-0 flex-1" placeholder="顿号分隔，最多 3 个" />
            </label>
            <label class="flex items-center gap-1">知识点
              <input v-model="knowledgePointsText" :class="inputClass" class="min-w-0 flex-1" />
            </label>
            <label class="flex items-center gap-1">候选标签
              <input v-model="suggestedTagsText" :class="inputClass" class="min-w-0 flex-1" />
            </label>
            <label class="flex items-center gap-1">一句话简析
              <input
                :class="inputClass" class="min-w-0 flex-1" :value="store.fields.briefExplanation"
                @input="store.fields.briefExplanation = ($event.target as HTMLInputElement).value; store.markDirty()"
              />
            </label>
            <label class="col-span-2 flex items-center gap-1">记忆口诀
              <input
                :class="inputClass" class="min-w-0 flex-1" :value="store.fields.mnemonic"
                @input="store.fields.mnemonic = ($event.target as HTMLInputElement).value; store.markDirty()"
              />
            </label>
          </div>

          <BlockEditor v-model:blocks="store.blocks" @dirty="store.markDirty()" />

          <div class="sticky bottom-0 mt-4 flex flex-wrap items-center gap-2 border-t border-border bg-background pt-3">
            <button
              class="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
              :disabled="store.saving || !store.dirty" @click="run(() => store.save())"
            ><PhFloppyDisk :size="16" /> 保存修改</button>
            <button
              class="flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover disabled:opacity-50"
              :disabled="!canApprove" @click="run(() => store.review('approved'))"
            ><PhCheck :size="16" /> 批准并继续</button>
            <button
              class="flex items-center gap-1 rounded-md border border-destructive/50 px-3 py-1.5 text-sm text-destructive hover:bg-destructive/5"
              @click="onReject"
            ><PhX :size="16" /> 驳回并继续</button>
            <label class="flex items-center gap-1 text-xs text-foreground-secondary">
              <input type="checkbox" v-model="store.skipRejectConfirm" /> 本次会话不再询问
            </label>
            <span class="flex-1" />
            <button
              class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
              :disabled="!canAuthorizeGeneral"
              title="只有未匹配或缺少教材的题目可以授权通识生成"
              @click="onAuthorizeGeneral"
            >授权通识生成</button>
            <button
              class="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
              @click="run(() => store.regenerate())"
            ><PhArrowClockwise :size="16" /> 重新生成</button>
          </div>
        </template>
        <div v-else class="flex h-full items-center justify-center text-sm text-foreground-secondary">
          从左侧队列选择一道题开始审核。
        </div>
      </div>

      <!-- 右栏：教材证据与 PDF 原页 -->
      <div class="flex w-96 shrink-0 flex-col overflow-hidden rounded-lg border border-border bg-background shadow-card">
        <EvidencePanel v-if="store.detail" />
        <div v-else class="flex h-full items-center justify-center text-sm text-foreground-secondary">
          打开题目后显示教材证据。
        </div>
      </div>
    </div>
    <LibraryPickerModal v-model:open="pickerOpen" @confirm="onPickerConfirm" />
    <QuickReviewModal v-model:open="quickReviewOpen" @approved="onQuickApproved" />
  </div>
</template>
