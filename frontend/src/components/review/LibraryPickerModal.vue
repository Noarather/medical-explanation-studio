<script setup lang="ts">
import { inject, ref, watch } from "vue"
import { invoke, type BridgeError } from "../../lib/bridge"

interface LibraryOption {
  id: number; name: string; subject: string; version: string; index_state: string
}

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ "update:open": [value: boolean]; confirm: [libraryIds: number[]] }>()

const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})

const libraries = ref<LibraryOption[]>([])
const checked = ref<number[]>([])
const loading = ref(false)

watch(() => props.open, async (open) => {
  if (!open) return
  loading.value = true
  try {
    const [listResponse, savedResponse] = await Promise.all([
      invoke<{ libraries: LibraryOption[] }>("library", "list"),
      invoke<{ library_ids: number[] }>("review", "get_generation_libraries"),
    ])
    libraries.value = listResponse.libraries
    const usable = new Set(
      listResponse.libraries.filter((row) => row.index_state === "compatible").map((row) => row.id))
    checked.value = savedResponse.library_ids.filter((id) => usable.has(id))
  } catch (error) {
    toast((error as BridgeError).message ?? String(error), "destructive")
  } finally {
    loading.value = false
  }
})

function toggle(id: number) {
  checked.value = checked.value.includes(id)
    ? checked.value.filter((item) => item !== id)
    : [...checked.value, id]
}

async function confirm() {
  try {
    await invoke("review", "save_generation_libraries", { library_ids: checked.value })
  } catch (error) {
    // 保存失败时保持 modal 打开，让用户可以重试
    toast((error as BridgeError).message ?? String(error), "destructive")
    return
  }
  emit("confirm", checked.value)
  emit("update:open", false)
}

const INDEX_STATE_LABELS: Record<string, string> = {
  none: "未建立索引", unknown: "索引状态未知", compatible: "索引可用", stale: "需重建",
}
</script>

<template>
  <div v-if="props.open" class="fixed inset-0 z-40 bg-foreground/20" @click="emit('update:open', false)" />
  <div v-if="props.open" class="fixed left-1/2 top-1/2 z-50 w-[440px] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-background p-5 shadow-overlay">
    <h2 class="mb-1 text-base font-semibold">选择本次生成使用的教材</h2>
    <p class="mb-3 text-xs text-foreground-secondary">
      勾选的教材将作为本次任务的检索范围（可跨学科多选）；未建立可用索引的教材不可勾选。选择会记住，下次生成沿用。
    </p>
    <div class="max-h-72 space-y-1 overflow-y-auto">
      <label
        v-for="row in libraries" :key="row.id"
        class="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm"
        :class="row.index_state === 'compatible' ? '' : 'opacity-50'"
      >
        <input
          type="checkbox" :checked="checked.includes(row.id)"
          :disabled="row.index_state !== 'compatible'" @change="toggle(row.id)"
        />
        <span class="min-w-0 flex-1 truncate">{{ row.name }}</span>
        <span class="shrink-0 text-xs text-foreground-secondary">
          {{ row.subject }} · {{ INDEX_STATE_LABELS[row.index_state] ?? row.index_state }}
        </span>
      </label>
      <div v-if="!libraries.length && !loading" class="py-4 text-center text-sm text-foreground-secondary">
        还没有登记教材。请先在“教材库”中添加 PDF 并建立索引。
      </div>
    </div>
    <div class="mt-4 flex items-center justify-end gap-2">
      <span class="flex-1 text-xs text-foreground-secondary">已选 {{ checked.length }} 本</span>
      <button class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="emit('update:open', false)">取消</button>
      <button class="rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover" @click="confirm">确定</button>
    </div>
  </div>
</template>
