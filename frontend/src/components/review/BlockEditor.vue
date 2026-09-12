<script setup lang="ts">
import { ref } from "vue"
import {
  SECTION_OPTIONS, TYPE_OPTIONS, TONE_OPTIONS,
  addBlock, removeBlock, moveBlock, addListItem, removeListItem,
  addTableRow, removeTableRow, addTableColumn, removeTableColumn, convertBlockType,
} from "../../lib/blocks"
import type { EditorBlock } from "../../lib/blocks"
import { PhArrowDown, PhArrowUp, PhPlus, PhTrash } from "@phosphor-icons/vue"

const props = defineProps<{ blocks: EditorBlock[] }>()
const emit = defineEmits<{ "update:blocks": [value: EditorBlock[]]; dirty: [] }>()

const newType = ref("paragraph")
const inputClass =
  "rounded-md border border-border bg-background px-2 py-1 text-sm outline-none focus:ring-2 focus:ring-primary"

function update(next: EditorBlock[]) {
  emit("update:blocks", next)
  emit("dirty")
}

function patchBlock(index: number, patch: Partial<EditorBlock>) {
  update(props.blocks.map((block, item) => (item === index ? { ...block, ...patch } : block)))
}

function guard(action: () => void) {
  try {
    action()
  } catch {
    // 边界操作（上限/保底）静默忽略，按钮本身已按状态禁用
  }
}
</script>

<template>
  <div>
    <div class="mb-2 flex flex-wrap items-center gap-2 text-sm">
      <select v-model="newType" :class="inputClass">
        <option v-for="option in TYPE_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
      </select>
      <button
        class="flex items-center gap-1 rounded-md border border-border px-2 py-1 hover:bg-muted disabled:opacity-50"
        :disabled="props.blocks.length >= 12"
        @click="guard(() => update(addBlock(props.blocks, newType)))"
      ><PhPlus :size="14" /> 区块</button>
      <span class="text-xs text-foreground-secondary">{{ props.blocks.length }} / 12 块</span>
    </div>

    <div class="space-y-3">
      <div
        v-for="(block, index) in props.blocks" :key="block.blockId"
        class="rounded-lg border border-border bg-surface p-3"
      >
        <div class="mb-2 flex flex-wrap items-center gap-2">
          <select
            :class="inputClass" :value="block.section"
            @change="patchBlock(index, { section: ($event.target as HTMLSelectElement).value })"
          >
            <option v-for="option in SECTION_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
          <select
            :class="inputClass" :value="block.type"
            @change="patchBlock(index, convertBlockType(block, ($event.target as HTMLSelectElement).value))"
          >
            <option v-for="option in TYPE_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
          <input
            :class="inputClass" class="min-w-0 flex-1" :value="block.title" placeholder="区块标题"
            @input="patchBlock(index, { title: ($event.target as HTMLInputElement).value })"
          />
          <template v-if="block.type === 'callout'">
            <select
              :class="inputClass" :value="block.tone ?? 'important'"
              @change="patchBlock(index, { tone: ($event.target as HTMLSelectElement).value })"
            >
              <option v-for="option in TONE_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
            </select>
          </template>
          <button class="rounded-md p-1 hover:bg-muted disabled:opacity-40" :disabled="index === 0" @click="update(moveBlock(props.blocks, index, -1))"><PhArrowUp :size="16" /></button>
          <button class="rounded-md p-1 hover:bg-muted disabled:opacity-40" :disabled="index === props.blocks.length - 1" @click="update(moveBlock(props.blocks, index, 1))"><PhArrowDown :size="16" /></button>
          <button class="rounded-md p-1 text-destructive hover:bg-destructive/10 disabled:opacity-40" :disabled="props.blocks.length <= 1" @click="guard(() => update(removeBlock(props.blocks, index)))"><PhTrash :size="16" /></button>
        </div>

        <textarea
          v-if="block.type === 'paragraph' || block.type === 'callout'"
          class="min-h-20 w-full rounded-md border border-border bg-background p-2 text-sm outline-none focus:ring-2 focus:ring-primary"
          :value="block.text ?? ''"
          @input="patchBlock(index, { text: ($event.target as HTMLTextAreaElement).value })"
        />

        <div v-else-if="block.type === 'list'" class="space-y-1">
          <div v-for="(itemText, itemIndex) in block.items ?? []" :key="itemIndex" class="flex items-center gap-1">
            <input
              :class="inputClass" class="min-w-0 flex-1" :value="itemText"
              @input="patchBlock(index, { items: (block.items ?? []).map((v, i) => (i === itemIndex ? ($event.target as HTMLInputElement).value : v)) })"
            />
            <button class="rounded-md p-1 text-destructive hover:bg-destructive/10 disabled:opacity-40" :disabled="(block.items ?? []).length <= 1" @click="guard(() => patchBlock(index, removeListItem(block, itemIndex)))"><PhTrash :size="14" /></button>
          </div>
          <button class="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-sm hover:bg-muted" @click="guard(() => patchBlock(index, addListItem(block)))"><PhPlus :size="14" /> 列表项</button>
        </div>

        <div v-else-if="block.type === 'table'" class="overflow-x-auto">
          <table class="w-full border-collapse text-sm">
            <thead>
              <tr>
                <th v-for="(column, columnIndex) in block.columns ?? []" :key="columnIndex" class="border border-border bg-muted p-1">
                  <input
                    class="w-full bg-transparent text-center font-medium outline-none" :value="column"
                    @input="patchBlock(index, { columns: (block.columns ?? []).map((v, i) => (i === columnIndex ? ($event.target as HTMLInputElement).value : v)) })"
                  />
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, rowIndex) in block.rows ?? []" :key="rowIndex">
                <td v-for="(cell, cellIndex) in row" :key="cellIndex" class="border border-border p-1">
                  <input
                    class="w-full bg-transparent outline-none" :value="cell"
                    @input="patchBlock(index, { rows: (block.rows ?? []).map((r, ri) => (ri === rowIndex ? r.map((c, ci) => (ci === cellIndex ? ($event.target as HTMLInputElement).value : c)) : r)) })"
                  />
                </td>
              </tr>
            </tbody>
          </table>
          <div class="mt-1 flex gap-2 text-sm">
            <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" @click="guard(() => patchBlock(index, addTableRow(block)))">＋行</button>
            <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="(block.rows ?? []).length <= 1" @click="guard(() => patchBlock(index, removeTableRow(block)))">－行</button>
            <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" @click="guard(() => patchBlock(index, addTableColumn(block)))">＋列</button>
            <button class="rounded-md border border-border px-2 py-1 hover:bg-muted" :disabled="(block.columns ?? []).length <= 2" @click="guard(() => patchBlock(index, removeTableColumn(block)))">－列</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
