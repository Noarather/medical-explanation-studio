<script setup lang="ts">
import { RouterLink } from "vue-router"
import { PhHouse, PhUploadSimple, PhFiles, PhCheckSquare, PhBooks, PhTag, PhExport, PhGear } from "@phosphor-icons/vue"
import { NAV_ITEMS } from "../router"

const ICONS: Record<string, unknown> = {
  House: PhHouse, UploadSimple: PhUploadSimple, Files: PhFiles,
  CheckSquare: PhCheckSquare, Books: PhBooks, Tag: PhTag, Export: PhExport,
}
const emit = defineEmits<{ (e: "open-settings"): void }>()
</script>

<template>
  <aside class="flex w-56 shrink-0 flex-col border-r border-border bg-surface">
    <div class="flex h-14 items-center gap-2 border-b border-border px-4">
      <span class="text-lg font-semibold text-primary">Med</span>
      <span class="text-lg font-semibold">Explain</span>
    </div>
    <nav class="flex-1 space-y-0.5 overflow-y-auto p-2">
      <RouterLink
        v-for="item in NAV_ITEMS" :key="item.path" :to="item.path"
        class="flex h-9 items-center gap-2.5 rounded-md px-3 text-sm text-foreground-secondary transition-colors duration-fast hover:bg-muted"
        active-class="!bg-primary-soft !text-primary font-medium"
      >
        <component :is="ICONS[item.icon]" :size="18" />
        {{ item.title }}
      </RouterLink>
    </nav>
    <button
      class="m-2 flex h-9 items-center gap-2.5 rounded-md px-3 text-sm text-foreground-secondary transition-colors duration-fast hover:bg-muted"
      @click="emit('open-settings')"
    >
      <PhGear :size="18" /> 设置
    </button>
  </aside>
</template>
