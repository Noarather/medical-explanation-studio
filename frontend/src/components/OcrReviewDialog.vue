<script setup lang="ts">
import { ref, watch, onUnmounted } from "vue"
import { invoke } from "../lib/bridge"
const props = defineProps<{ library: { id: number; name: string } | null }>()
const emit = defineEmits<{ (e: "close"): void }>()
type Result = {model: string; ok: boolean; text?: string; raw_text?: string; error?: string; trace?: {requestId?: string; elapsedMs?: number; usage?: {total_tokens?: number}}}
type Report = {id: string; pdf_page: number; source_name: string; image: string; total_tokens: number; identical: boolean; results: Result[]}
const page = ref(1), model = ref("qwen3.8-max"), consent = ref(false)
const busy = ref(false), message = ref(""), error = ref("")
const report = ref<Report | null>(null)
const history = ref<{id: string; created_at: string; pdf_page: number}[]>([])
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
async function refreshHistory(id: number) {
  const data = await invoke<{items: typeof history.value}>("library", "ocr_review_history", {library_id: id})
  if (props.library?.id === id) history.value = data.items
}
async function poll(token: string, current: number) {
  if (current !== generation) return
  try {
    const task = await invoke<{status: string; stage: string; message?: string; result?: Report}>("library", "ocr_review_status", {token})
    if (current !== generation) return
    message.value = task.stage
    if (task.status === "running") { timer = setTimeout(() => poll(token, current), 700); return }
    busy.value = false
    if (task.status === "completed" && task.result) {
      report.value = task.result
      if (props.library) await refreshHistory(props.library.id)
    } else error.value = task.message || "复核失败"
  } catch (e: any) { if (current === generation) { busy.value = false; error.value = e?.message || String(e) } }
}
watch(() => props.library, async (library) => {
  const current = ++generation
  clearTimeout(timer)
  report.value = null; error.value = ""; consent.value = false; history.value = []; busy.value = false; page.value = 1
  if (!library) return
  try {
    await refreshHistory(library.id)
    const {task} = await invoke<{task: {token: string} | null}>("library", "ocr_review_active")
    if (current !== generation) return
    if (task) { busy.value = true; await poll(task.token, current) }
  } catch (e: any) { if (current === generation) error.value = e?.message || String(e) }
}, {immediate: true})
async function start() {
  if (!props.library || busy.value || !consent.value) return
  busy.value = true; error.value = ""; report.value = null; message.value = "准备复核"
  try {
    const {token} = await invoke<{token: string}>("library", "ocr_review_start", {
      library_id: props.library.id, pdf_page: page.value, model: model.value, consent: true,
    })
    await poll(token, generation)
  } catch (e: any) { busy.value = false; error.value = e?.message || String(e) }
}
async function load(id: string) {
  if (!props.library || busy.value) return
  try { report.value = await invoke<Report>("library", "ocr_review_report", {library_id: props.library.id, review_id: id}) }
  catch (e: any) { error.value = e?.message || String(e) }
}
onUnmounted(() => { generation++; clearTimeout(timer) })
</script>

<template>
  <div v-if="library" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
    <section class="flex max-h-[90vh] w-full max-w-6xl flex-col rounded-lg bg-background shadow-overlay" aria-label="单页高级 OCR 复核">
      <header class="flex shrink-0 items-center justify-between border-b border-border p-4">
        <h2>高级 OCR 复核 · {{ library.name }}</h2>
        <button :disabled="busy" class="rounded border border-border px-3 py-1 disabled:opacity-50" @click="emit('close')">关闭</button>
      </header>
      <div class="min-h-0 overflow-y-auto p-4 space-y-4">
        <p class="text-sm text-foreground-secondary">指定一个 PDF 物理页（不是印刷页码），分别调用当前 OCR 模型和高级视觉模型；仅保存比较记录，不覆盖索引，也不自动认定高级模型更准确。原图与结果仅保存在本机数据库。</p>
        <div class="flex flex-wrap items-center gap-3">
          <label>PDF 物理页 <input v-model.number="page" type="number" min="1" step="1" :disabled="busy" class="w-24 rounded border border-border bg-background px-2 py-1" /></label>
          <label>复核模型 <select v-model="model" :disabled="busy" class="rounded border border-border bg-background px-2 py-1"><option>qwen3.8-max</option><option>qwen3.7-plus</option></select></label>
          <button :disabled="busy || !consent || !Number.isInteger(page) || page < 1" class="rounded bg-primary px-3 py-2 text-onprimary disabled:opacity-50" @click="start">{{ busy ? '复核中…' : '开始单页复核' }}</button>
        </div>
        <label class="flex items-center gap-2 text-sm"><input v-model="consent" type="checkbox" :disabled="busy" />同意将本页图片发送到 DashScope，最多两次请求，可能产生费用；再次点击会重新收费。请等待完成后再退出软件。</label>
        <p v-if="busy" role="status">{{ message }}</p>
        <p v-if="error" role="alert" class="text-destructive">{{ error }}</p>
        <details v-if="history.length"><summary>历史复核（最近 20 条，查看不收费）</summary><div class="flex flex-wrap gap-2 mt-2"><button v-for="item in history" :key="item.id" :disabled="busy" class="rounded border border-border p-2 text-xs" @click="load(item.id)">PDF {{ item.pdf_page }} 页 · {{ item.created_at }}</button></div></details>
        <template v-if="report">
          <p class="text-sm">{{ report.source_name }} · PDF {{ report.pdf_page }} 页 · 已报告 {{ report.total_tokens }} tokens（失败请求可能未返回用量） · {{ report.identical ? '两次文本相同，不代表无识别错误' : '结果存在差异或调用失败，请逐项核对' }}</p>
          <div class="grid grid-cols-1 gap-3 lg:grid-cols-3">
            <div><h3 class="mb-2">实际发送的原图</h3><img :src="report.image" alt="本次 OCR 复核的原始页面" class="w-full border border-border" /></div>
            <article v-for="(result, i) in report.results" :key="i" class="min-w-0 rounded border border-border p-3">
              <h3>{{ i === 0 ? '当前 OCR' : '高级复核' }} · {{ result.model }}</h3>
              <p class="my-2 break-all text-xs text-foreground-secondary">{{ result.trace?.elapsedMs ?? '未知' }} ms · {{ result.trace?.usage?.total_tokens ?? '未知' }} tokens · {{ result.trace?.requestId }}</p>
              <pre v-if="result.ok" class="whitespace-pre-wrap break-words text-sm">{{ result.text }}</pre>
              <p v-else class="text-destructive">{{ result.error }}</p>
              <details v-if="result.raw_text"><summary class="mt-3 text-xs">模型原始输出</summary><pre class="whitespace-pre-wrap break-words text-xs">{{ result.raw_text }}</pre></details>
            </article>
          </div>
        </template>
      </div>
    </section>
  </div>
</template>
