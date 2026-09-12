<script setup lang="ts">
import { ref } from "vue"

type Tone = "default" | "success" | "destructive"
interface ToastItem { id: number; message: string; tone: Tone }

const items = ref<ToastItem[]>([])
let nextId = 1

function push(message: string, tone: Tone = "default") {
  const id = nextId++
  items.value.push({ id, message, tone })
  setTimeout(() => {
    items.value = items.value.filter((t) => t.id !== id)
  }, 3000)
}

function toneClass(tone: Tone): string {
  if (tone === "success") return "text-success"
  if (tone === "destructive") return "text-destructive"
  return "text-foreground"
}

defineExpose({ push })
</script>

<template>
  <div class="pointer-events-none fixed right-4 top-4 z-[60] flex flex-col items-end gap-2">
    <TransitionGroup name="toast">
      <div
        v-for="t in items"
        :key="t.id"
        class="pointer-events-auto rounded-md border border-border bg-background px-4 py-2 text-sm shadow-overlay"
        :class="toneClass(t.tone)"
      >
        {{ t.message }}
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-enter-active {
  transition: opacity 200ms ease, transform 200ms ease;
}
.toast-enter-from {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
