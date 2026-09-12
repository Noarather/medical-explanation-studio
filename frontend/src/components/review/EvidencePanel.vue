<script setup lang="ts">
import { useReviewStore } from "../../stores/review"
import PdfViewer from "./PdfViewer.vue"

const store = useReviewStore()

function pageLabel(item: Record<string, any>): string {
  const page = item.source_page ?? item.sourcePage
  return page ? `课本第 ${page} 页` : "课本前置页"
}
</script>

<template>
  <div class="flex h-full flex-col">
    <div class="border-b border-border px-3 py-2 text-sm font-medium">
      教材证据（{{ store.evidence.length }} 条）
    </div>
    <div class="max-h-72 overflow-y-auto">
      <button
        v-for="(item, index) in store.evidence" :key="index"
        data-test="evidence-item"
        class="block w-full border-b border-border px-3 py-2 text-left text-sm hover:bg-muted"
        :class="index === store.selectedEvidenceIndex ? 'bg-primary-soft border-l-2 border-l-primary' : ''"
        @click="store.selectedEvidenceIndex = index"
      >
        <div class="flex items-center justify-between gap-2">
          <span class="truncate font-medium">{{ item.textbook || item.source_file || "教材" }}</span>
          <span v-if="item.score != null" class="shrink-0 text-xs text-foreground-secondary">
            {{ Number(item.score).toFixed(3) }}
          </span>
        </div>
        <div class="text-xs text-foreground-secondary">
          {{ item.textbook_version || item.version || "" }} · {{ pageLabel(item) }}
          <template v-if="item.pdf_page || item.pdfPage"> · PDF 第 {{ item.pdf_page ?? item.pdfPage }} 页</template>
        </div>
        <p class="mt-1 line-clamp-3 text-xs">{{ item.excerpt || item.text || item.quote }}</p>
      </button>
      <div v-if="!store.evidence.length" class="p-4 text-center text-sm text-foreground-secondary">
        没有教材证据。
      </div>
    </div>
    <div class="min-h-0 flex-1">
      <PdfViewer
        v-if="store.selectedEvidence?.source_path"
        :src="store.selectedEvidence.source_path"
        :page="store.selectedEvidence.pdf_page ?? store.selectedEvidence.pdfPage ?? 1"
      />
      <div v-else class="flex h-full items-center justify-center text-sm text-foreground-secondary">
        选择一条证据查看 PDF 原页。
      </div>
    </div>
  </div>
</template>
