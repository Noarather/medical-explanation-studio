<script setup lang="ts">
import { ref, watch } from "vue"
const props = defineProps<{ row: Record<string, unknown> }>()
const emit = defineEmits<{ (e: "save", row: Record<string, unknown>): void; (e: "dirty"): void }>()
const draft = ref<Record<string, unknown>>({})
const options = ref("")
const answer = ref("")
const advanced = ref(false)
const raw = ref("")
const error = ref("")
watch(() => JSON.stringify(props.row), serialized => {
  const row = JSON.parse(serialized)
  draft.value = JSON.parse(JSON.stringify(row)); raw.value = JSON.stringify(row, null, 2)
  options.value = Array.isArray(row.options) ? row.options.join("\n") : ""
  answer.value = Array.isArray(row.answer) ? JSON.stringify(row.answer) : String(row.answer ?? "")
  error.value = ""; advanced.value = false
}, {immediate:true})
function formRow() {
  return {...draft.value, options: draft.value.type === "fill" ? [] : options.value.split(/\r?\n/).filter(s => s.trim()),
    answer: draft.value.type === "fill" ? JSON.parse(answer.value || "[]") : answer.value}
}
function toggle() {
  try {
    if (!advanced.value) raw.value = JSON.stringify(formRow(), null, 2)
    else {
      const row = JSON.parse(raw.value)
      if (!row || typeof row !== "object" || Array.isArray(row)) throw new Error("题目必须为 JSON 对象")
      draft.value = row; options.value = (row.options || []).join("\n")
      answer.value = Array.isArray(row.answer) ? JSON.stringify(row.answer) : String(row.answer ?? "")
    }
    error.value = ""; advanced.value = !advanced.value
  } catch { error.value = "JSON 格式有误，请检查答案或题目内容" }
}
function save() {
  try {
    const row = advanced.value ? JSON.parse(raw.value) : formRow()
    if (!row || typeof row !== "object" || Array.isArray(row)) throw new Error()
    error.value = ""; emit("save", row)
  } catch { error.value = "JSON 格式有误；填空答案请使用二维数组，例如 [[\"ATP\"]]" }
}
function addOption() {
  const lines = options.value.split(/\r?\n/).filter(line => line.trim())
  const labels = lines.map(line => line.match(/^([A-Z])[.．、:：)）\s]/i)?.[1]?.toUpperCase())
  const label = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('').find(letter => !labels.includes(letter))
  if (!label) return
  const before = labels.findIndex(value => value && value > label)
  lines.splice(before < 0 ? lines.length : before, 0, `${label}. `)
  options.value = lines.join('\n')
  emit('dirty')
}
const field = "w-full rounded border border-border bg-background p-2 text-sm"
</script>
<template>
  <div class="space-y-2" @input="emit('dirty')" @change="emit('dirty')">
    <button class="text-sm underline" @click="toggle">{{advanced ? '切换字段表单' : '高级 JSON 编辑'}}</button>
    <textarea v-if="advanced" v-model="raw" :class="field" class="min-h-48 font-mono" aria-label="异常题目 JSON" />
    <div v-else class="grid gap-2 sm:grid-cols-2">
      <label class="text-sm">题目 ID<input v-model="draft.id" :class="field" aria-label="题目 ID" /></label>
      <label class="text-sm">学科<input v-model="draft.subject" :class="field" aria-label="异常题学科" /></label>
      <label class="text-sm">题库<select v-model="draft.bank" :class="field" aria-label="异常题题库"><option value="">请选择</option><option value="school">校内</option><option value="kaoyan">考研</option></select></label>
      <label class="text-sm">题型<select v-model="draft.type" :class="field" aria-label="异常题题型"><option v-for="kind in ['A1','A2','A3','multiple','judge','fill']" :key="kind">{{kind}}</option></select></label>
      <label class="text-sm sm:col-span-2">题干<textarea :value="String(draft.question ?? '')" @input="draft.question = ($event.target as HTMLTextAreaElement).value" :class="field" aria-label="异常题题干" /></label>
      <div v-if="draft.type !== 'fill'" class="text-sm sm:col-span-2">
        <label>选项（每行一项，保留字母标签）<textarea v-model="options" :class="field" class="min-h-32" aria-label="异常题选项" /></label>
        <button class="mt-1 text-sm underline" @click="addOption">补一个选项</button>
      </div>
      <label class="text-sm sm:col-span-2">答案{{draft.type === 'fill' ? '（二维 JSON 数组）' : ''}}<input v-model="answer" :class="field" aria-label="异常题答案" /></label>
    </div>
    <p v-if="error" role="alert" class="text-sm text-destructive">{{error}}</p>
    <button :class="field" @click="save">保存修正并重新检查</button>
  </div>
</template>
