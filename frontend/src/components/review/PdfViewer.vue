<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue"
import * as pdfjs from "pdfjs-dist"
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url"
import { invoke } from "../../lib/bridge"

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

const props = defineProps<{ src: string; page: number; pageCount?: number }>()

const canvasRef = ref<HTMLCanvasElement | null>(null)
const currentPage = ref(1)
const zoom = ref(1.4)
const loading = ref(false)
const failed = ref(false)
const totalPages = ref(0)

let document_: any = null
let renderToken = 0
let loadToken = 0

function fileUrl(path: string): string {
  return "file:///" + encodeURI(path.replace(/\\/g, "/"))
}

async function loadDocument(src: string) {
  const token = ++loadToken
  let next: any = null
  try {
    next = await pdfjs.getDocument({ url: fileUrl(src) }).promise
  } catch {
    if (token !== loadToken) return
    // file:// 加载失败时回退桥接 base64（小文件）
    const data = await invoke<{ base64: string }>("review", "read_pdf_base64", { path: src })
    if (token !== loadToken) return
    const bytes = Uint8Array.from(atob(data.base64), (char) => char.charCodeAt(0))
    next = await pdfjs.getDocument({ data: bytes }).promise
  }
  if (token !== loadToken) {
    // 过期结果：销毁已加载的新文档，避免泄漏
    void next?.destroy().catch(() => {})
    return
  }
  const previous = document_
  document_ = null
  ++renderToken // 使旧文档上的在途渲染失效，其取消错误不计为失败
  await previous?.destroy().catch(() => {})
  document_ = next
  totalPages.value = next.numPages
}

async function render() {
  if (!document_ || !canvasRef.value) return
  const token = ++renderToken
  loading.value = true
  try {
    const page = await document_.getPage(Math.min(Math.max(1, currentPage.value), totalPages.value || 1))
    if (token !== renderToken) return
    const viewport = page.getViewport({ scale: zoom.value })
    const canvas = canvasRef.value
    canvas.width = viewport.width
    canvas.height = viewport.height
    await page.render({ canvasContext: canvas.getContext("2d")!, viewport }).promise
  } catch (error) {
    // 过期渲染的取消/文档销毁错误不算失败：新一次加载会渲染正确页面
    if (token === renderToken) throw error
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.src, props.page],
  async () => {
    failed.value = false
    currentPage.value = Math.max(1, props.page || 1)
    try {
      await loadDocument(props.src)
      await render()
    } catch {
      failed.value = true
    }
  },
  { immediate: true },
)
watch([currentPage, zoom], () => { void render() })

onBeforeUnmount(() => {
  ++loadToken
  ++renderToken // 使在途渲染的 reject 落入静默吞掉分支，避免卸载后 unhandled rejection
  const previous = document_
  document_ = null
  void previous?.destroy().catch(() => {})
})

const inputClass =
  "w-14 rounded-md border border-border bg-background px-1 py-0.5 text-center text-sm outline-none"
</script>

<template>
  <div class="flex h-full flex-col">
    <div class="flex items-center justify-center gap-2 border-t border-border px-2 py-1.5 text-sm">
      <button class="rounded-md border border-border px-2 hover:bg-muted disabled:opacity-40" :disabled="currentPage <= 1" @click="currentPage -= 1">‹</button>
      <input :class="inputClass" :value="currentPage" @change="currentPage = Math.min(Math.max(1, Number(($event.target as HTMLInputElement).value) || 1), totalPages || 1)" />
      <span class="text-foreground-secondary">/ {{ totalPages || "…" }}</span>
      <button class="rounded-md border border-border px-2 hover:bg-muted disabled:opacity-40" :disabled="totalPages > 0 && currentPage >= totalPages" @click="currentPage += 1">›</button>
      <button class="rounded-md border border-border px-2 hover:bg-muted" @click="zoom = Math.max(0.6, zoom - 0.2)">－</button>
      <button class="rounded-md border border-border px-2 hover:bg-muted" @click="zoom = Math.min(3, zoom + 0.2)">＋</button>
    </div>
    <div class="flex min-h-0 flex-1 items-start justify-center overflow-auto bg-muted/40 p-2">
      <div v-if="failed" class="p-4 text-center text-sm text-destructive">无法打开教材原页。</div>
      <canvas v-show="!failed" ref="canvasRef" class="shadow-card" />
    </div>
  </div>
</template>
