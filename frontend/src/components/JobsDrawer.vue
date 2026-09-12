<script setup lang="ts">
import { computed, ref } from "vue"
import { PhX, PhPause, PhPlay, PhStop, PhTrash } from "@phosphor-icons/vue"
import { useJobsStore, type JobRow } from "../stores/jobs"

defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: "update:open", v: boolean): void }>()

const jobs = useJobsStore()
const selected = ref<Set<string>>(new Set())

const TERMINAL = ["completed", "failed", "cancelled"]
const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  paused: "已暂停",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
}
const STATUS_BADGE: Record<string, string> = {
  queued: "bg-muted text-foreground-secondary",
  running: "bg-primary-soft text-primary",
  paused: "bg-muted text-warning",
  completed: "bg-muted text-success",
  failed: "bg-muted text-destructive",
  cancelled: "bg-muted text-foreground-secondary",
}
const TYPE_LABELS: Record<string, string> = {
  scan: "索引", generate: "生成解析", general: "通识生成", general_batch: "批量通识生成",
  tag_backfill: "补齐题目标签", study_point_backfill: "补齐复习考点",
  memory_card_backfill: "生成背诵知识卡", upgrade_v2: "升级 v2",
}

const removable = computed(() => jobs.jobs.filter((j) => TERMINAL.includes(j.status)))

function badge(status: string): string {
  return STATUS_BADGE[status] ?? "bg-muted text-foreground-secondary"
}
function label(status: string): string {
  return STATUS_LABEL[status] ?? status
}
function percent(job: JobRow): number {
  return job.progress_total > 0 ? Math.round((job.progress_current / job.progress_total) * 100) : 0
}
function toggleSelect(id: string, checked: boolean) {
  const next = new Set(selected.value)
  if (checked) next.add(id)
  else next.delete(id)
  selected.value = next
}
async function removeSelected() {
  if (selected.value.size === 0) return
  await jobs.remove([...selected.value])
  selected.value = new Set()
}
async function clearFinished() {
  const ids = removable.value.map((j) => j.id)
  if (ids.length === 0) return
  await jobs.remove(ids)
  selected.value = new Set()
}
</script>

<template>
  <aside
    class="fixed inset-y-0 right-0 z-40 flex w-96 transform flex-col border-l border-border bg-background shadow-overlay transition-transform duration-base"
    :class="open ? 'translate-x-0' : 'translate-x-full'"
  >
    <div class="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
      <span class="text-base font-semibold">任务</span>
      <button
        class="flex h-8 w-8 items-center justify-center rounded-md text-foreground-secondary transition-colors duration-fast hover:bg-muted"
        @click="emit('update:open', false)"
      >
        <PhX :size="18" />
      </button>
    </div>

    <div class="min-h-0 flex-1 overflow-y-auto p-3">
      <div v-if="jobs.jobs.length === 0" class="p-6 text-center text-sm text-foreground-secondary">
        暂无任务
      </div>
      <div
        v-for="job in jobs.jobs"
        :key="job.id"
        class="mb-2 rounded-lg border border-border bg-background p-3 shadow-card"
      >
        <div class="flex items-center justify-between gap-2">
          <div class="flex min-w-0 items-center gap-2">
            <input
              v-if="TERMINAL.includes(job.status)"
              type="checkbox"
              class="h-3.5 w-3.5 shrink-0 accent-primary"
              :checked="selected.has(job.id)"
              @change="toggleSelect(job.id, ($event.target as HTMLInputElement).checked)"
            />
            <span class="truncate text-sm font-medium">{{ job.title }}</span>
          </div>
          <span class="shrink-0 rounded-md px-1.5 py-0.5 text-xs" :class="badge(job.status)">
            {{ label(job.status) }}
          </span>
        </div>
        <div class="mt-0.5 text-xs text-foreground-secondary">{{ TYPE_LABELS[job.job_type] ?? job.job_type }}</div>

        <div v-if="job.status === 'running' || job.status === 'paused'" class="mt-2">
          <div class="h-1 overflow-hidden rounded bg-muted">
            <div class="h-full bg-primary transition-[width] duration-base" :style="{ width: percent(job) + '%' }"></div>
          </div>
          <div class="mt-1 text-xs text-foreground-secondary">
            {{ job.progress_current }}/{{ job.progress_total }}<template v-if="job.message"> · {{ job.message }}</template>
          </div>
        </div>
        <div v-else-if="job.status === 'failed' && job.error" class="mt-2 text-xs text-destructive">
          {{ job.error }}
        </div>
        <div v-else-if="job.message" class="mt-2 text-xs text-foreground-secondary">{{ job.message }}</div>

        <div v-if="job.status === 'running' || job.status === 'paused'" class="mt-2 flex gap-1">
          <button
            v-if="job.status === 'running'"
            class="flex h-7 items-center gap-1 rounded-md px-2 text-xs text-foreground-secondary transition-colors duration-fast hover:bg-muted"
            @click="jobs.control(job.id, 'pause')"
          >
            <PhPause :size="14" /> 暂停
          </button>
          <button
            v-else
            class="flex h-7 items-center gap-1 rounded-md px-2 text-xs text-foreground-secondary transition-colors duration-fast hover:bg-muted"
            @click="jobs.control(job.id, 'resume')"
          >
            <PhPlay :size="14" /> 继续
          </button>
          <button
            class="flex h-7 items-center gap-1 rounded-md px-2 text-xs text-destructive transition-colors duration-fast hover:bg-muted"
            @click="jobs.control(job.id, 'cancel')"
          >
            <PhStop :size="14" /> 取消
          </button>
        </div>
      </div>
    </div>

    <div class="flex shrink-0 items-center justify-between border-t border-border p-3">
      <button
        class="flex h-8 items-center gap-1 rounded-md px-2 text-xs text-destructive transition-colors duration-fast hover:bg-muted disabled:opacity-40"
        :disabled="selected.size === 0"
        @click="removeSelected"
      >
        <PhTrash :size="14" /> 删除选中（{{ selected.size }}）
      </button>
      <button
        class="h-8 rounded-md px-2 text-xs text-foreground-secondary transition-colors duration-fast hover:bg-muted disabled:opacity-40"
        :disabled="removable.length === 0"
        @click="clearFinished"
      >
        清理已完成
      </button>
    </div>
  </aside>
</template>
