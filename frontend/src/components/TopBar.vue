<script setup lang="ts">
import { computed } from "vue"
import { PhListChecks } from "@phosphor-icons/vue"
import { useJobsStore } from "../stores/jobs"

const emit = defineEmits<{ (e: "toggle-jobs"): void }>()
const jobs = useJobsStore()

const activeCount = computed(
  () => jobs.jobs.filter((j) => ["queued", "running", "paused"].includes(j.status)).length,
)
</script>

<template>
  <header class="relative flex h-14 shrink-0 items-center justify-end gap-2 border-b border-border bg-background px-4">
    <button
      class="flex h-8 items-center gap-1.5 rounded-md px-3 text-sm text-foreground-secondary transition-colors duration-fast hover:bg-muted"
      @click="emit('toggle-jobs')"
    >
      <PhListChecks :size="18" /> 任务
      <span
        v-if="activeCount > 0"
        class="rounded-full bg-primary px-1.5 text-xs leading-4 text-onprimary"
      >{{ activeCount }}</span>
    </button>
    <div
      v-if="jobs.progressPercent > 0"
      class="absolute inset-x-0 bottom-0 h-[2px] bg-muted"
    >
      <div
        class="h-full bg-primary transition-[width] duration-base"
        :style="{ width: jobs.progressPercent + '%' }"
      ></div>
    </div>
  </header>
</template>
