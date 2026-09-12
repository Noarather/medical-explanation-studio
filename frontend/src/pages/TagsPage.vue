<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue"
import { PhMagnifyingGlass } from "@phosphor-icons/vue"
import type { BridgeError } from "../lib/bridge"
import { useTagsStore } from "../stores/tags"

const store = useTagsStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})

onMounted(() => { store.init().catch((error) => toast((error as BridgeError).message ?? String(error), "destructive")) })

const inputClass =
  "rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:ring-2 focus:ring-primary"

const STATUS_LABELS: Record<string, string> = { active: "正式", candidate: "候选", merged: "已合并" }

const renameOpen = ref(false)
const renameValue = ref("")
const mergeOpen = ref(false)
const mergeTargetId = ref<number | null>(null)
const deleteOpen = ref(false)
const deleteConfirm = ref("")

function run(action: () => unknown) {
  try {
    const result = action()
    if (result instanceof Promise) {
      result.catch((error) => toast((error as BridgeError).message ?? String(error), "destructive"))
    }
  } catch (error) {
    toast((error as BridgeError).message ?? String(error), "destructive")
  }
}

const canRenameOrActivate = computed(() => store.selected !== null && store.selected.status !== "merged")
const renameValid = computed(() => {
  const value = renameValue.value.trim()
  return value.length >= 2 && value.length <= 24
})
const canMerge = computed(() => store.selected !== null && store.selected.status !== "merged" && store.mergeTargets.length > 0)

function openRename() {
  if (!store.selected) return
  renameValue.value = store.selected.label
  renameOpen.value = true
}

function submitRename() {
  const row = store.selected
  if (!row || !renameValid.value) return
  renameOpen.value = false
  run(async () => {
    await store.rename(row, renameValue.value.trim())
    toast("标签已改名", "success")
  })
}

function onActivate() {
  const row = store.selected
  if (!row) return
  run(async () => {
    await store.activate(row)
    toast(`“${row.label}”已设为正式标签`, "success")
  })
}

function openMerge() {
  mergeTargetId.value = store.mergeTargets[0]?.id ?? null
  mergeOpen.value = true
}

function submitMerge() {
  const source = store.selected
  const target = store.mergeTargets.find((row) => row.id === mergeTargetId.value)
  mergeOpen.value = false
  if (!source || !target) return
  run(async () => {
    const changed = await store.merge(source.id, target.id)
    toast(`已将“${source.label}”合并到“${target.label}”，更新 ${changed} 道题`, "success")
  })
}

function openDelete() {
  deleteConfirm.value = ""
  deleteOpen.value = true
}

function submitDelete() {
  const row = store.selected
  deleteOpen.value = false
  if (!row) return
  run(async () => {
    const result = await store.remove(row, deleteConfirm.value)
    toast(`已从 ${result.changed} 道题中移除“${result.deleted}”`, "success")
  })
}
</script>

<template>
  <div>
    <h1 class="mb-2 text-xl font-semibold">标签管理</h1>
    <p class="mb-4 text-sm text-foreground-secondary">
      标签按题目集与学科隔离；候选标签关联同学科至少 3 道不同题目后自动成为正式标签。
    </p>

    <div v-if="!store.sets.length" class="rounded-lg border border-border bg-background p-10 text-center text-sm text-foreground-secondary shadow-card">
      还没有题目集。请先在“题目导入”导入题目。
    </div>

    <template v-else>
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <select :value="store.setId" :class="inputClass" @change="run(() => store.applySet(($event.target as HTMLSelectElement).value))">
          <option v-for="set in store.sets" :key="set.id" :value="set.id">{{ set.name }} · {{ set.question_count }} 题</option>
        </select>
        <select v-model="store.subject" :class="inputClass" @change="run(() => store.load())">
          <option value="">全部学科</option>
          <option v-for="subject in store.subjects" :key="subject" :value="subject">{{ subject }}</option>
        </select>
        <select v-model="store.status" :class="inputClass" @change="run(() => store.load())">
          <option value="">全部状态</option>
          <option value="active">正式</option>
          <option value="candidate">候选</option>
          <option value="merged">已合并</option>
        </select>
        <input v-model="store.search" :class="inputClass" placeholder="搜索标签名称或别名" @keyup.enter="run(() => store.load())" />
        <button class="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="run(() => store.load())">
          <PhMagnifyingGlass :size="16" /> 搜索
        </button>
        <span class="flex-1" />
        <button class="rounded-md border border-border px-2.5 py-1.5 text-sm hover:bg-muted disabled:opacity-50" :disabled="!canRenameOrActivate" @click="openRename">改名</button>
        <button class="rounded-md bg-primary px-2.5 py-1.5 text-sm text-onprimary hover:bg-primary-hover disabled:opacity-50" :disabled="!canRenameOrActivate || store.selected?.status === 'active'" @click="onActivate">设为正式</button>
        <button class="rounded-md border border-border px-2.5 py-1.5 text-sm hover:bg-muted disabled:opacity-50" :disabled="!canMerge" @click="openMerge">合并同义标签</button>
        <button class="rounded-md border border-destructive/50 px-2.5 py-1.5 text-sm text-destructive hover:bg-destructive/5 disabled:opacity-50" :disabled="!store.selected" @click="openDelete">彻底删除</button>
      </div>

      <div class="overflow-auto rounded-lg border border-border bg-background shadow-card" style="max-height: 64vh">
        <table class="w-full border-collapse text-sm">
          <thead class="sticky top-0 bg-muted">
            <tr>
              <th class="px-3 py-1.5 text-left font-medium">标签</th>
              <th class="px-3 py-1.5 text-left font-medium">学科</th>
              <th class="px-3 py-1.5 text-left font-medium">状态</th>
              <th class="px-3 py-1.5 text-left font-medium">使用次数</th>
              <th class="px-3 py-1.5 text-left font-medium">合并至</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in store.tags" :key="row.id"
              class="cursor-pointer border-t border-border hover:bg-muted/60"
              :class="row.id === store.selectedId ? 'bg-primary-soft' : ''"
              @click="store.selectedId = row.id"
            >
              <td class="px-3 py-1.5 font-medium">{{ row.label }}</td>
              <td class="px-3 py-1.5">{{ row.subject }}</td>
              <td class="px-3 py-1.5">
                <span
                  class="rounded px-1.5 py-0.5 text-xs"
                  :class="row.status === 'active' ? 'bg-success/10 text-success' : row.status === 'merged' ? 'bg-muted text-foreground-secondary' : 'bg-warning/10 text-warning'"
                >{{ STATUS_LABELS[row.status] ?? row.status }}</span>
              </td>
              <td class="px-3 py-1.5">{{ row.usage_count }}</td>
              <td class="px-3 py-1.5 text-foreground-secondary">{{ row.merged_into || "—" }}</td>
            </tr>
            <tr v-if="!store.tags.length">
              <td colspan="5" class="px-4 py-8 text-center text-foreground-secondary">
                {{ store.loading ? "加载中…" : "当前筛选下没有标签。" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- 改名 -->
    <div v-if="renameOpen" class="fixed inset-0 z-40 bg-foreground/20" @click="renameOpen = false" />
    <div v-if="renameOpen" class="fixed left-1/2 top-1/2 z-50 w-96 -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-background p-5 shadow-overlay">
      <h2 class="mb-3 text-base font-semibold">标签改名</h2>
      <input v-model="renameValue" :class="inputClass" class="w-full" maxlength="24" placeholder="新的标签名称（2～24 字符）" @keyup.enter="submitRename" />
      <div class="mt-4 flex justify-end gap-2">
        <button class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="renameOpen = false">取消</button>
        <button class="rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover disabled:opacity-50" :disabled="!renameValid" @click="submitRename">保存</button>
      </div>
    </div>

    <!-- 合并 -->
    <div v-if="mergeOpen" class="fixed inset-0 z-40 bg-foreground/20" @click="mergeOpen = false" />
    <div v-if="mergeOpen" class="fixed left-1/2 top-1/2 z-50 w-96 -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-background p-5 shadow-overlay">
      <h2 class="mb-3 text-base font-semibold">将“{{ store.selected?.label }}”合并到：</h2>
      <select v-model="mergeTargetId" :class="inputClass" class="w-full">
        <option v-for="row in store.mergeTargets" :key="row.id" :value="row.id">{{ row.label }}（{{ row.usage_count }} 题）</option>
      </select>
      <div class="mt-4 flex justify-end gap-2">
        <button class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="mergeOpen = false">取消</button>
        <button class="rounded-md bg-primary px-3 py-1.5 text-sm text-onprimary hover:bg-primary-hover" :disabled="mergeTargetId === null" @click="submitMerge">合并</button>
      </div>
    </div>

    <!-- 彻底删除 -->
    <div v-if="deleteOpen" class="fixed inset-0 z-40 bg-foreground/20" @click="deleteOpen = false" />
    <div v-if="deleteOpen" class="fixed left-1/2 top-1/2 z-50 w-[420px] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-background p-5 shadow-overlay">
      <h2 class="mb-2 text-base font-semibold text-destructive">彻底删除标签</h2>
      <p class="mb-3 text-sm">将从正式标签、候选标签和所有关联题目中删除“{{ store.selected?.label }}”。<br />请输入完整标签名称确认：</p>
      <input v-model="deleteConfirm" :class="inputClass" class="w-full" :placeholder="store.selected?.label" />
      <div class="mt-4 flex justify-end gap-2">
        <button class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted" @click="deleteOpen = false">取消</button>
        <button
          class="rounded-md bg-destructive px-3 py-1.5 text-sm text-onprimary disabled:opacity-50"
          :disabled="deleteConfirm.trim() !== store.selected?.label" @click="submitDelete"
        >彻底删除</button>
      </div>
    </div>
  </div>
</template>
