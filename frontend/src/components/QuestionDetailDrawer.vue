<script setup lang="ts">
import { computed } from "vue"
import { PhX } from "@phosphor-icons/vue"
import { useQuestionsStore } from "../stores/questions"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ "update:open": [value: boolean] }>()
const store = useQuestionsStore()

const detail = computed(() => store.detail)
const raw = computed(() => (detail.value?.raw ?? {}) as Record<string, unknown>)
const evidence = computed(() => (detail.value?.evidence ?? []) as Record<string, unknown>[])

function text(value: unknown): string {
  if (Array.isArray(value)) return value.join("、")
  return String(value ?? "")
}

function close() { emit("update:open", false) }
</script>

<template>
  <div v-if="props.open" class="fixed inset-0 z-40 bg-foreground/20" @click="close" />
  <aside
    class="fixed right-0 top-0 z-50 flex h-full w-[480px] flex-col border-l border-border bg-background shadow-overlay transition-transform duration-base"
    :class="props.open ? 'translate-x-0' : 'translate-x-full'"
  >
    <header class="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
      <span class="text-sm font-semibold">题目详情 · {{ detail?.external_id }}</span>
      <button class="rounded-md p-1 hover:bg-muted" @click="close"><PhX :size="18" /></button>
    </header>
    <div v-if="detail" class="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 text-sm">
      <div class="flex flex-wrap gap-x-4 gap-y-1 text-foreground-secondary">
        <span>学科：{{ detail.subject || "—" }}</span>
        <span>题型：{{ detail.question_type || "—" }}</span>
        <span>来源：{{ raw.questionSource || "—" }}</span>
        <span>流程：{{ detail.pipeline_status }} / 审核：{{ detail.review_status }}</span>
      </div>
      <div v-if="raw.caseInfo" class="rounded-md bg-surface p-3">
        <div class="mb-1 text-xs text-foreground-secondary">病例背景</div>
        <p class="whitespace-pre-wrap">{{ raw.caseInfo }}</p>
      </div>
      <div>
        <div class="mb-1 text-xs text-foreground-secondary">题干</div>
        <p class="whitespace-pre-wrap">{{ raw.question }}</p>
      </div>
      <div v-if="Array.isArray(raw.options) && raw.options.length">
        <div class="mb-1 text-xs text-foreground-secondary">选项</div>
        <ul class="space-y-1">
          <li v-for="(option, index) in raw.options" :key="index">{{ option }}</li>
        </ul>
      </div>
      <div>答案：<span class="font-medium">{{ text(raw.answer) }}</span></div>
      <div v-if="(raw.tags as unknown[])?.length || (raw.knowledgePoints as unknown[])?.length">
        标签：{{ text(raw.tags) }}<template v-if="(raw.knowledgePoints as unknown[])?.length">　知识点：{{ text(raw.knowledgePoints) }}</template>
      </div>
      <div v-if="detail.error_message" class="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-destructive">
        {{ detail.error_message }}
      </div>
      <div v-if="detail.explanation">
        <div class="mb-1 text-xs text-foreground-secondary">解析</div>
        <p class="whitespace-pre-wrap rounded-md bg-surface p-3">{{ detail.explanation }}</p>
      </div>
      <div v-if="evidence.length">
        <div class="mb-1 text-xs text-foreground-secondary">教材证据（{{ evidence.length }} 条）</div>
        <ul class="space-y-2">
          <li v-for="(item, index) in evidence" :key="index" class="rounded-md border border-border p-2">
            <div class="text-foreground-secondary">
              {{ item.textbook || item.source_file || "教材" }}
              <template v-if="item.source_page"> · 课本第 {{ item.source_page }} 页</template>
              <template v-if="item.score != null"> · 相似度 {{ Number(item.score).toFixed(3) }}</template>
            </div>
            <p class="mt-1 line-clamp-3">{{ item.text || item.quote }}</p>
          </li>
        </ul>
      </div>
    </div>
  </aside>
</template>
