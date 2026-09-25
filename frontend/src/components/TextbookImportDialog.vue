<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue"
import { invoke } from "../lib/bridge"
import type { LibraryRow } from "../stores/libraries"

interface Segment { pdf_start: number; pdf_end: number; printed_start: number; printed_end: number; support: number; confidence: string }
interface Calibration { status: string; offset: number | null; mapped_pages: number; unknown_pages: number; message: string; segments: Segment[] }
interface Item { index: number; name: string; subject: string; version: string; file_name: string; root_path: string;
  error: string; warnings: string[]; metadata_source?: string; page_count: number; calibration?: Calibration;
  selected: boolean; mode: string; pdf_anchor: number; textbook_anchor: number; outcome?: string }
interface Task { status: string; stage: string; current: number; total: number; cancellable: boolean; message?: string; result?: any }
const props = defineProps<{ open: boolean; calibrating?: LibraryRow | null }>()
const emit = defineEmits<{ "update:open": [value: boolean]; saved: [] }>()
const items = ref<Item[]>([])
const page = ref(1)
const pageSize = 10
const pageCount = computed(() => Math.max(1, Math.ceil(items.value.length / pageSize)))
const visibleItems = computed(() => items.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const scrollPane = ref<HTMLElement | null>(null)
watch(page, async () => { await nextTick(); if (scrollPane.value) scrollPane.value.scrollTop = 0 })
const busy = ref(false)
const task = ref<Task | null>(null)
const taskToken = ref("")
const draftToken = ref("")
const error = ref("")
const message = ref("")
const inputClass = "w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm"
const selected = computed(() => items.value.filter(r => r.selected && !r.error && !r.outcome))
const invalid = computed(() => selected.value.some(r => !r.name.trim() || !r.subject.trim()))
const cannotCalibrate = computed(() => props.calibrating && selected.value.some(r => r.calibration?.status !== "recognized"))
watch(() => props.open, open => {
  if (!open || busy.value) return
  items.value = []; page.value = 1; draftToken.value = ""; error.value = ""; message.value = ""
  if (props.calibrating) run(() => inspect([props.calibrating!.root_path]))
})
async function run(action: () => Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ""; message.value = ""
  try { await action() } catch (e: any) { error.value = e?.message ?? String(e) }
  finally { busy.value = false; taskToken.value = ""; task.value = null }
}
async function wait(token: string) {
  taskToken.value = token
  while (true) {
    task.value = await invoke<Task>("library", "inspect_status", {token})
    if (task.value.status === "completed") return task.value.result
    if (task.value.status !== "running") throw new Error(task.value.message || "教材操作未完成")
    await new Promise(resolve => setTimeout(resolve, 350))
  }
}
async function inspect(paths: string[]) {
  const {token} = await invoke<{token: string}>("library", "inspect_start", {paths})
  const result = await wait(token)
  draftToken.value = token
  page.value = 1
  items.value = result.items.map((row: Item) => ({...row,
    name: props.calibrating?.name ?? row.name, subject: props.calibrating?.subject ?? row.subject,
    version: props.calibrating?.version ?? row.version, selected: !row.error, mode: "auto",
    pdf_anchor: Math.max(1, (row.calibration?.offset ?? 0) + 1),
    textbook_anchor: Math.max(1, 1 - (row.calibration?.offset ?? 0))}))
}
async function pick() {
  await run(async () => {
    const {paths} = await invoke<{paths: string[]}>("library", "pick_pdfs")
    if (!paths.length) return
    await inspect(paths)
  })
}
async function cancel() {
  if (!taskToken.value) return
  try {
    const result = await invoke<{accepted: boolean}>("library", "inspect_cancel", {token: taskToken.value})
    message.value = result.accepted ? "正在取消，本次未提交的教材不会入库…" : "已进入提交阶段，请等待完成"
  } catch (e: any) { error.value = e?.message ?? String(e) }
}
async function commit() {
  await run(async () => {
    const {token} = await invoke<{token: string}>("library", "commit_batch", {
      token: draftToken.value, library_id: props.calibrating?.id ?? null,
      items: selected.value.map(({index, name, subject, version, mode, pdf_anchor, textbook_anchor}) =>
        ({index, name, subject, version, mode, pdf_anchor, textbook_anchor})),
    })
    const result = await wait(token)
    let affected = 0
    for (const outcome of result.results) {
      const row = items.value.find(item => item.index === outcome.index)
      if (!row) continue
      if (["imported", "calibrated", "duplicate"].includes(outcome.status)) {
        row.selected = false
        row.outcome = outcome.status === "imported" ? "已导入" : outcome.status === "calibrated" ? "已应用页码校准" : outcome.message
      } else row.error = outcome.message
      affected += outcome.affected_questions ?? 0
    }
    message.value = props.calibrating ? `校准已完成；${affected} 道历史题目需要复核页码引用。` : `成功导入 ${result.imported} 本；未自动建立索引。扫描页识别可能已使用云端 OCR。`
    emit("saved")
  })
}
function close() { if (!busy.value) emit("update:open", false) }
</script>

<template>
  <Teleport to="body">
  <div v-if="open" class="fixed inset-0 z-50 grid place-items-center overflow-hidden bg-foreground/20 p-4" @click.self="close">
  <section role="dialog" aria-modal="true" aria-label="教材批量导入与页码校准"
    class="flex h-[min(88vh,960px)] min-h-0 w-full max-w-[960px] flex-col overflow-hidden rounded-lg border border-border bg-background p-5 shadow-overlay">
    <header class="shrink-0" data-testid="textbook-header">
    <h2 class="text-base font-semibold">{{ calibrating ? '自动校准教材页码' : '批量导入教材' }}</h2>
    <p class="mt-1 text-xs text-foreground-secondary">优先读取文字层；扫描页的封面／页眉页脚图片将发送至 DashScope 云端 OCR，可能产生费用（每本最多抽样 60 页）。可逐本核对，不自动建立索引。</p>
    <p v-if="calibrating" class="mt-2 rounded border border-warning/40 p-2 text-xs text-warning">确认应用后更新结构化证据页码；受影响的历史题目会回到待审核，解析文字不自动改写。手工统一偏移请使用教材的“编辑”。</p>
    <div class="my-3 flex flex-wrap items-center gap-3">
      <button v-if="!calibrating" :disabled="busy" class="rounded border border-border px-3 py-1.5 text-sm disabled:opacity-50" @click="pick">选择 PDF（支持多选）</button>
      <span class="text-xs text-foreground-secondary">{{ items.length }} 本 · 已选 {{ selected.length }} 本</span>
      <span v-if="busy" role="status" class="text-sm">{{ task?.stage || '准备中' }} {{ task?.current ?? 0 }}/{{ task?.total ?? 0 }}</span>
      <button v-if="busy && task?.cancellable" class="ml-auto text-sm text-destructive" @click="cancel">取消处理</button>
    </div>
    </header>
    <div ref="scrollPane" data-testid="textbook-scroll" class="textbook-scroll min-h-0 flex-1 space-y-3 overflow-x-hidden overflow-y-auto bg-background pr-1">
      <article v-for="row in visibleItems" :key="row.index" class="rounded-md border border-border bg-background p-3" :aria-label="`教材 ${row.index+1}`">
        <label class="flex items-center gap-2 text-sm font-medium">
          <input v-model="row.selected" type="checkbox" :disabled="busy || !!row.error || !!row.outcome" />
          <span class="break-all">{{ row.file_name }}</span><span class="ml-auto shrink-0 text-xs">{{ row.page_count || '?' }} 页</span>
        </label>
        <p v-if="row.error" role="alert" class="mt-2 text-sm text-destructive">{{ row.error }}（本条不导入，其他教材可继续）</p>
        <p v-if="row.outcome" class="mt-2 text-sm text-success">{{ row.outcome }}</p>
        <template v-if="!row.error && !row.outcome">
          <div class="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
            <label class="text-xs">名称<input v-model="row.name" :disabled="busy || !!calibrating" :class="inputClass" :aria-label="`教材${row.index+1}名称`" /></label>
            <label class="text-xs">学科<input v-model="row.subject" :disabled="busy || !!calibrating" :class="inputClass" :aria-label="`教材${row.index+1}学科`" placeholder="未识别时请填写" /></label>
            <label class="text-xs">版本<input v-model="row.version" :disabled="busy || !!calibrating" :class="inputClass" :aria-label="`教材${row.index+1}版本`" /></label>
          </div>
          <p class="mt-1 text-xs text-foreground-secondary">识别依据：{{ row.metadata_source }}。{{ calibrating ? '此操作只应用页码，不改名称／学科／版本。' : '请核对后导入。' }}</p>
          <p v-for="warning in row.warnings" :key="warning" class="mt-1 text-xs text-warning">{{ warning }}</p>
          <div class="mt-3 rounded bg-muted/40 p-2 text-xs">
            <label v-if="!calibrating" class="flex items-center gap-2">页码方式
              <select v-model="row.mode" :disabled="busy" class="rounded border border-border bg-background p-1">
                <option value="auto">自动分段校准（未确认页不猜测）</option>
                <option value="manual">手工统一偏移（仅适用连续编号）</option>
              </select>
            </label>
            <template v-if="row.mode === 'auto'">
              <p class="my-2">{{ row.calibration?.message }}</p>
              <div v-for="segment in row.calibration?.segments" :key="segment.pdf_start" class="py-0.5">
                PDF {{ segment.pdf_start }}–{{ segment.pdf_end }} 页 → 课本 {{ segment.printed_start }}–{{ segment.printed_end }} 页
                <span class="text-foreground-secondary">（{{ segment.support }} 个页码依据，{{ segment.confidence === 'high' ? '高' : '中' }}可信度）</span>
              </div>
              <p class="mt-1 text-foreground-secondary">已映射 {{ row.calibration?.mapped_pages ?? 0 }} 页；{{ row.calibration?.unknown_pages ?? row.page_count }} 页未确认印刷页。</p>
            </template>
            <div v-else class="mt-2 flex flex-wrap items-center gap-2">
              PDF 第 <input v-model.number="row.pdf_anchor" type="number" min="1" :max="row.page_count" aria-label="PDF 对应页" class="w-20 rounded border p-1" :disabled="busy" /> 页
              = 课本第 <input v-model.number="row.textbook_anchor" type="number" min="1" max="20000" aria-label="课本对应页" class="w-20 rounded border p-1" :disabled="busy" /> 页
            </div>
          </div>
        </template>
      </article>
    </div>
    <footer class="shrink-0 bg-background" data-testid="textbook-footer">
    <nav v-if="items.length > pageSize" aria-label="教材分页" class="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs">
      <span>第 {{ page }} / {{ pageCount }} 页 · 每页 {{ pageSize }} 本，勾选和修改跨页保留</span>
      <div class="flex gap-2">
        <button :disabled="busy || page <= 1" class="rounded border border-border px-2 py-1 disabled:opacity-50" @click="page--">上一页教材</button>
        <button :disabled="busy || page >= pageCount" class="rounded border border-border px-2 py-1 disabled:opacity-50" @click="page++">下一页教材</button>
      </div>
    </nav>
    <p v-if="error" role="alert" class="mt-2 text-sm text-destructive">{{ error }}</p>
    <p v-if="message" role="status" class="mt-2 text-sm">{{ message }}</p>
    <p v-if="invalid" class="mt-2 text-xs text-warning">请补全所选教材的名称和学科，或取消勾选。</p>
    <div class="mt-4 flex justify-end gap-2">
      <button :disabled="busy" class="rounded border border-border px-3 py-1.5 text-sm disabled:opacity-50" @click="close">关闭</button>
      <button :disabled="busy || !selected.length || invalid || !!cannotCalibrate" class="rounded bg-primary px-3 py-1.5 text-sm text-onprimary disabled:opacity-50" @click="commit">{{ calibrating ? '确认应用校准' : `确认导入 ${selected.length} 本` }}</button>
    </div>
    </footer>
  </section>
  </div>
  </Teleport>
</template>

<style scoped>
.textbook-scroll {
  contain: paint;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}
</style>
