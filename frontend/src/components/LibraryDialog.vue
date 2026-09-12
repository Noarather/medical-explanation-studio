<script setup lang="ts">
import { computed, inject, ref, watch } from "vue"
import { PhFolderOpen } from "@phosphor-icons/vue"
import { invoke, type BridgeError } from "../lib/bridge"
import { useLibrariesStore } from "../stores/libraries"
import type { LibraryRow } from "../stores/libraries"

const props = defineProps<{ open: boolean; editing: LibraryRow | null }>()
const emit = defineEmits<{ "update:open": [value: boolean]; saved: [] }>()

const store = useLibrariesStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})

const name = ref("")
const subject = ref("")
const version = ref("")
const rootPath = ref("")
const pdfAnchor = ref(1)
const textbookAnchor = ref(1)
const error = ref("")
const saving = ref(false)

watch(() => props.open, (open) => {
  if (!open) return
  error.value = ""
  const row = props.editing
  name.value = row?.name ?? ""
  subject.value = row?.subject ?? ""
  version.value = row?.version ?? ""
  rootPath.value = row?.root_path ?? ""
  const offset = row?.page_offset ?? 0
  pdfAnchor.value = offset >= 0 ? offset + 1 : 1
  textbookAnchor.value = offset >= 0 ? 1 : 1 - offset
})

const title = computed(() => (props.editing ? "编辑教材" : "添加教材"))
const offsetChanged = computed(
  () => props.editing !== null
    && pdfAnchor.value - textbookAnchor.value !== props.editing.page_offset,
)

async function pickPdf() {
  const { path } = await invoke<{ path: string }>("library", "pick_pdf")
  if (!path) return
  rootPath.value = path
  if (!name.value.trim()) {
    name.value = path.replace(/\\/g, "/").split("/").pop()?.replace(/\.pdf$/i, "") ?? ""
  }
}

async function submit() {
  error.value = ""
  saving.value = true
  try {
    await store.submitDialog(props.editing, {
      name: name.value, subject: subject.value, root_path: rootPath.value,
      version: version.value, pdf_anchor: pdfAnchor.value, textbook_anchor: textbookAnchor.value,
    })
    emit("saved")
    toast(props.editing ? "教材已保存" : "教材已添加", "success")
  } catch (exc) {
    error.value = (exc as BridgeError).message ?? String(exc)
  } finally {
    saving.value = false
  }
}

const inputClass =
  "w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:ring-2 focus:ring-primary"
</script>

<template>
  <div v-if="props.open" class="fixed inset-0 z-40 bg-foreground/20" @click="emit('update:open', false)" />
  <div
    v-if="props.open"
    class="fixed left-1/2 top-1/2 z-50 w-[480px] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-background p-5 shadow-overlay"
  >
    <h2 class="mb-1 text-base font-semibold">{{ title }}</h2>
    <p class="mb-4 text-xs text-foreground-secondary">
      名称、学科和 PDF 文件为必填项；页码校准用于把 PDF 页映射为课本印刷页。
    </p>
    <div class="space-y-3 text-sm">
      <label class="block">名称
        <input v-model="name" :class="inputClass" placeholder="例如：外科学（第10版）" />
      </label>
      <label class="block">学科
        <input v-model="subject" :class="inputClass" placeholder="例如：外科学" />
      </label>
      <label class="block">版本
        <input v-model="version" :class="inputClass" placeholder="例如：第10版（可不填）" />
      </label>
      <div>
        <span>PDF 文件</span>
        <div class="mt-1 flex gap-2">
          <input :value="rootPath" :class="inputClass" readonly placeholder="选择 PDF 文件" />
          <button class="flex shrink-0 items-center gap-1 rounded-md border border-border px-3 text-sm hover:bg-muted" @click="pickPdf">
            <PhFolderOpen :size="16" /> 选择
          </button>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <span class="shrink-0">实际页码校准：PDF 第</span>
        <input v-model.number="pdfAnchor" type="number" min="1" max="20000" :class="inputClass" style="width: 90px" />
        <span class="shrink-0">页，对应课本第</span>
        <input v-model.number="textbookAnchor" type="number" min="1" max="20000" :class="inputClass" style="width: 90px" />
        <span class="shrink-0">页</span>
      </div>
      <p v-if="offsetChanged" class="rounded-md border border-warning/40 bg-warning/5 p-2 text-xs text-warning">
        修改页码差会同步重算已有证据的课本页码，并更新解析中的页码引用。
      </p>
      <pre v-if="error" class="max-h-32 overflow-y-auto whitespace-pre-wrap rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">{{ error }}</pre>
    </div>
    <div class="mt-5 flex justify-end gap-2">
      <button class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="emit('update:open', false)">取消</button>
      <button
        class="rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover disabled:opacity-50"
        :disabled="saving" @click="submit"
      >{{ saving ? "保存中…" : "保存" }}</button>
    </div>
  </div>
</template>
