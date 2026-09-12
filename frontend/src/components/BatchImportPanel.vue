<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue"
import { invoke } from "../lib/bridge"
import { useImportsStore } from "../stores/imports"
import BatchIssueEditor from "./BatchIssueEditor.vue"

interface Profile { mapping: Record<string,string>; defaults: Record<string,string> }
interface Item { index: number; source: string; number: number; row: Record<string,unknown> | null; reasons: string[] }
interface Preview { draft_id: string; total: number; ready: number; duplicates: number; issues: number; items: Item[]; result: { imported?: number } }
const store = useImportsStore()
const profiles = ref<Record<string,Profile>>({})
const profileName = ref("")
const defaults = ref<Record<string,string>>({bank: "", subject: "", system: "", questionSource: ""})
const paths = ref<string[]>([])
const name = ref("")
const drafts = ref<{id:string; name:string}[]>([])
const draftId = ref("")
const preview = ref<Preview | null>(null)
const page = ref(1)
const pageSize = 50
const pageCount = computed(() => Math.max(1, Math.ceil((preview.value?.items.length ?? 0) / pageSize)))
const visibleItems = computed(() => preview.value?.items.slice((page.value - 1) * pageSize, page.value * pageSize) ?? [])
interface Task {token?:string; status:string; stage:string; current:number; total:number; cancellable:boolean; result?:Preview; message?:string}
const task = ref<Task | null>(null)
const taskToken = ref("")
let disposed = false
onUnmounted(() => { disposed = true })
const busy = ref(false)
const startGeneration = ref(true)
const message = ref("")
const inputClass = "rounded-md border border-border bg-background px-2 py-1.5 text-sm"
async function run(action: () => Promise<void>) {
  busy.value = true; message.value = ""
  try { await action() } catch (error) { message.value = (error as {message?:string}).message ?? String(error) }
  finally { busy.value = false }
}
async function refresh() {
  profiles.value = (await invoke<{profiles:Record<string,Profile>}>("imports", "import_profiles")).profiles
  drafts.value = (await invoke<{drafts:typeof drafts.value}>("imports", "batch_drafts")).drafts
}
onMounted(() => run(async () => {
  await refresh()
  const active = await invoke<{task: Task | null}>("imports", "batch_active")
  if (active?.task?.token) setPreview(await waitTask(active.task.token))
}))
function loadProfile() {
  const item = profiles.value[profileName.value]
  if (item) { defaults.value = {...item.defaults}; store.mapping = {...item.mapping} }
}
function setPreview(value: Preview) {
  preview.value = value; draftId.value = value.draft_id
  page.value = Math.min(page.value, Math.max(1, Math.ceil(value.items.length / pageSize)))
}
async function waitTask(token: string): Promise<Preview> {
  taskToken.value = token
  try {
    while (!disposed) {
      const state = await invoke<Task>("imports", "batch_status", {token})
      task.value = state
      if (state.status === "completed" && state.result) return state.result
      if (state.status !== "running") throw new Error(state.message || "批量操作未完成")
      await new Promise(resolve => setTimeout(resolve, 350))
    }
    throw new Error("任务在后台继续，可返回导入页查看")
  } finally { taskToken.value = ""; task.value = null }
}
async function batch(action: string, payload: Record<string,unknown>) {
  const started = await invoke<{token:string}>("imports", "batch_start", {action, payload})
  return waitTask(started.token)
}
async function cancel() {
  try {
    const result = await invoke<{accepted:boolean}>("imports", "batch_cancel", {token:taskToken.value})
    message.value = result.accepted ? "正在取消，请等待当前文件读取结束…" : "正在原子提交，请等待完成"
  } catch (error) { message.value = String(error) }
}
async function pick() {
  paths.value = (await invoke<{paths:string[]}>("imports", "pick_batch_files")).paths
  preview.value = null
}
async function inspect() {
  page.value = 1
  setPreview(await batch("preview", {
    paths: paths.value, name: name.value, profile: {defaults: defaults.value, mapping: store.mapping},
  }))
  await refresh()
}
async function commit() {
  if (!preview.value) return
  setPreview(await batch("commit", {draft_id: preview.value.draft_id, start_generation: startGeneration.value}))
  await store.refreshSets()
}
async function fix(item: Item, row: Record<string,unknown>) {
  if (!preview.value) return
  setPreview(await batch("resume", {
    draft_id: preview.value.draft_id, corrections: {[item.index]: row},
  }))
}
</script>

<template>
  <section class="mb-6 rounded-lg border border-border bg-background p-4 shadow-card">
    <h2 class="mb-2 font-semibold">批量导入 · 只处理异常</h2>
    <p class="mb-3 text-sm text-foreground-secondary">合格题目批量入库，完全重复题自动跳过。答案冲突或缺少字段的记录保存在批次中，修正后可继续导入。</p>
    <fieldset :disabled="busy" class="space-y-3 disabled:opacity-60">
      <div class="flex flex-wrap gap-2">
        <select v-model="profileName" :class="inputClass" @change="loadProfile">
          <option value="">来源模板</option><option v-for="(_, key) in profiles" :key="key" :value="key">{{ key }}</option>
        </select>
        <input v-model="profileName" :class="inputClass" placeholder="模板名称" aria-label="模板名称" />
        <button :class="inputClass" :disabled="!profileName.trim()" @click="run(async () => { await invoke('imports', 'import_profiles', {name: profileName, profile: {defaults, mapping: store.mapping}}); await refresh() })">保存模板及下方 Excel 映射</button>
        <select v-model="defaults.bank" :class="inputClass" aria-label="缺省题库">
          <option value="">缺省题库（源文件优先）</option><option value="school">校内</option><option value="kaoyan">考研</option>
        </select>
        <input v-model="defaults.subject" :class="inputClass" placeholder="缺省学科" aria-label="缺省学科" />
        <input v-model="defaults.system" :class="inputClass" placeholder="缺省章节" aria-label="缺省章节" />
        <input v-model="defaults.questionSource" :class="inputClass" placeholder="缺省来源" aria-label="缺省来源" />
      </div>
      <div class="flex flex-wrap gap-2">
        <input v-model="name" :class="inputClass" placeholder="批次名称" aria-label="批次名称" />
        <button :class="inputClass" @click="run(pick)">选择多个 JSON / Excel 文件（{{ paths.length }}）</button>
        <button :class="inputClass" :disabled="!paths.length || !name.trim()" @click="run(inspect)">检查批次</button>
        <select v-model="draftId" :class="inputClass" aria-label="恢复批次" @change="run(async () => { if (draftId) { page = 1; setPreview(await batch('resume', {draft_id: draftId})) } })">
          <option value="">恢复已保存批次</option><option v-for="draft in drafts" :key="draft.id" :value="draft.id">{{ draft.name }}</option>
        </select>
      </div>
      <div v-if="preview" class="space-y-2">
        <p class="text-sm">总计 {{ preview.total }} · 可导入 {{ preview.ready }} · 重复跳过 {{ preview.duplicates }} · 异常 {{ preview.issues }} · 已导入 {{ preview.result.imported ?? 0 }}</p>
        <label class="flex items-center gap-2 text-sm"><input v-model="startGeneration" type="checkbox" />导入后自动生成解析（使用已配置模型与教材）</label>
        <button class="rounded-md bg-primary px-3 py-2 text-sm text-onprimary disabled:opacity-50" :disabled="!preview.ready" @click="run(commit)">导入全部可用题目{{ startGeneration ? '并开始生成' : '' }}</button>
        <div class="flex items-center gap-3 text-sm">
          <button :disabled="page <= 1" :class="inputClass" @click="page--">上一页异常</button>
          <span>异常第 {{page}} / {{pageCount}} 页 · 每页 {{pageSize}} 条</span>
          <button :disabled="page >= pageCount" :class="inputClass" @click="page++">下一页异常</button>
        </div>
        <details v-for="item in visibleItems" :key="item.index" class="rounded border border-border p-2">
          <summary class="cursor-pointer text-sm">{{ item.row?.id ?? item.source }} · 第 {{ item.number }} 条：{{ item.reasons.join('；') }}</summary>
          <p class="my-2 break-all text-xs text-foreground-secondary">来源：{{ item.source }}</p>
          <BatchIssueEditor v-if="item.row" :row="item.row" @save="row => run(() => fix(item, row))" />
          <p v-else class="text-sm">修复源文件后重新创建批次；其他文件的题目可继续导入。</p>
        </details>
      </div>
    </fieldset>
    <div v-if="busy" class="mt-2 flex items-center gap-3 text-sm" role="status">
      <span>{{task?.stage || '处理中…'}} <template v-if="task?.total">{{task.current}} / {{task.total}}</template></span>
      <button v-if="taskToken" :disabled="!task?.cancellable" class="rounded border border-border px-3 py-1" @click="cancel">{{task?.cancellable ? '取消批量操作' : '正在提交，不可取消'}}</button>
    </div>
    <p v-if="message" role="alert" class="mt-2 whitespace-pre-wrap text-sm text-destructive">{{ message }}</p>
  </section>
</template>
