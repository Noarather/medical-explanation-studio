<script setup lang="ts">
import { computed } from "vue"
import { RouterLink } from "vue-router"

const props = withDefaults(defineProps<{
  label: string
  value: number | null
  tone?: "default" | "warning" | "success" | "destructive"
  to?: string
}>(), { tone: "default", to: undefined })

const valueClass = computed(() => ({
  default: "text-foreground",
  warning: "text-warning",
  success: "text-success",
  destructive: "text-destructive",
}[props.tone]))
</script>

<template>
  <component
    :is="to ? RouterLink : 'div'"
    :to="to"
    class="block rounded-lg border border-border bg-background p-4 shadow-card transition-colors duration-fast"
    :class="to ? 'hover:border-primary' : ''"
  >
    <div class="text-xs text-foreground-secondary">{{ label }}</div>
    <div class="mt-1 text-2xl font-semibold" :class="valueClass">{{ value ?? "—" }}</div>
  </component>
</template>
