<script setup lang="ts">
import { computed, ref, watch } from "vue"
import type { FileImportReview } from "../stores/imports"
import BatchIssueEditor from "./BatchIssueEditor.vue"

const props = defineProps<{ review: FileImportReview; busy: boolean }>()
const emit = defineEmits<{
  (e: "resolve", action: "edit" | "remove" | "restore", index: number, row?: Record<string, unknown>): void
  (e: "commit"): void
  (e: "discard"): void
}>()
const page = ref(0)
const dirty = ref(new Set<number>())
let submittedIndex: number | null = null
const pageCount = computed(() => Math.max(1, Math.ceil(props.review.items.length / 20)))
const ordered = computed(() => props.review.items)
const items = computed(() => ordered.value.slice(page.value * 20, (page.value + 1) * 20))
watch(() => props.review.draft_id, () => { page.value = 0; dirty.value.clear() })
watch(() => props.review, () => {
  if (submittedIndex !== null) dirty.value.delete(submittedIndex)
  submittedIndex = null
})
watch(pageCount, count => { page.value = Math.min(page.value, count - 1) })
const button = "rounded border border-border px-3 py-1.5 text-sm disabled:opacity-50"
function resolve(action: "edit" | "remove" | "restore", index: number, row?: Record<string, unknown>) {
  submittedIndex = index
  emit('resolve', action, index, row)
}
</script>

<template>
  <section class="mt-4 rounded-md border border-border p-4" aria-label="导入题目即时处理">
    <h3 class="font-semibold">导入检查与即时处理</h3>
    <p class="mt-2 text-sm" role="status">
      共 {{ review.total }} 题 · 已适配 {{ review.repaired }} 题 · 待处理 {{ review.issues }} 题 · 本次删除 {{ review.removed }} 题 · 可导入 {{ review.ready }} 题
    </p>
    <p class="my-2 text-sm text-foreground-secondary">
      尚未写入题库。已适配题可展开核对；异常题请对照原文补充选项，或删除本次导入项。不会修改原文件或已入库题目，删除可撤销。关闭软件会丢弃未提交的修改。
    </p>
    <p v-if="dirty.size" class="mb-2 text-sm text-destructive">有 {{ dirty.size }} 题尚未保存修正，请先点击对应题目的“保存修正并重新检查”。</p>
    <fieldset :disabled="busy" class="space-y-3">
      <details v-for="item in items" :key="`${review.draft_id}-${item.index}`" :open="item.errors.length > 0" class="rounded border border-border p-3">
        <summary class="cursor-pointer text-sm font-medium">
          第 {{ item.number }} 题 · {{ item.removed ? '已从本次导入删除' : item.errors.length ? '待处理' : item.repaired ? '已适配，待确认' : '修正通过' }}
          <span class="ml-2 text-foreground-secondary">{{ String(item.row.question ?? '').slice(0, 80) }}</span>
        </summary>
        <div class="mt-3 space-y-3">
          <p v-if="item.errors.length" class="whitespace-pre-wrap text-sm text-destructive">{{ item.errors.join('\n') }}</p>
          <div>
            <h4 class="mb-1 text-sm font-medium">导入前原文（只读，含原有解析）</h4>
            <pre class="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-3 text-sm" aria-label="题目原文">{{ JSON.stringify(item.original, null, 2) }}</pre>
          </div>
          <p v-if="item.repaired" class="text-sm text-success">已将缺失选项从题干末尾拆回选项列表；未改写选项内容或答案，请核对。</p>
          <template v-if="!item.removed">
            <BatchIssueEditor :row="item.row" @dirty="dirty.add(item.index)" @save="row => resolve('edit', item.index, row)" />
            <button :class="button" class="text-destructive" @click="resolve('remove', item.index)">删除本次导入的这道题</button>
          </template>
          <button v-else :class="button" @click="resolve('restore', item.index)">撤销删除</button>
        </div>
      </details>
      <div v-if="pageCount > 1" class="flex items-center gap-3 text-sm">
        <button :class="button" :disabled="page === 0 || dirty.size > 0" @click="page--">上一页处理项</button>
        <span>{{ page + 1 }} / {{ pageCount }}</span>
        <button :class="button" :disabled="page + 1 >= pageCount || dirty.size > 0" @click="page++">下一页处理项</button>
      </div>
      <div class="flex flex-wrap gap-3">
        <button :class="button" class="bg-primary text-onprimary" :disabled="review.issues > 0 || review.ready === 0 || dirty.size > 0" @click="emit('commit')">确认导入 {{ review.ready }} 题</button>
        <button :class="button" @click="emit('discard')">放弃本次处理</button>
      </div>
    </fieldset>
  </section>
</template>
