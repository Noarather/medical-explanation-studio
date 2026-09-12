<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref, watch } from "vue"
import { PhX } from "@phosphor-icons/vue"
import { useSettingsStore } from "../stores/settings"
import { invoke, onApiTestResult } from "../lib/bridge"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: "update:open", v: boolean): void }>()

const settings = useSettingsStore()
const toast = inject<(message: string, tone?: "default" | "success" | "destructive") => void>("toast", () => {})

type FieldType = "text" | "number" | "checkbox"
interface FieldDef { key: string; label: string; type: FieldType }

const GROUPS: { id: string; label: string; fields: FieldDef[] }[] = [
  { id: "api", label: "模型服务 / API", fields: [] },
  {
    id: "model",
    label: "向量 / OCR",
    fields: [
      { key: "embedding_model", label: "Embedding 模型", type: "text" },
      { key: "ocr_model", label: "OCR 模型", type: "text" },
      { key: "dashscope_base_url", label: "DashScope Base URL", type: "text" },
    ],
  },
  {
    id: "retrieval",
    label: "检索",
    fields: [
      { key: "similarity_threshold", label: "相似度阈值", type: "number" },
      { key: "rerank_enabled", label: "启用 Rerank", type: "checkbox" },
      { key: "rerank_model", label: "Rerank 模型", type: "text" },
      { key: "rerank_candidate_count", label: "Rerank 候选数", type: "number" },
      { key: "rerank_timeout_seconds", label: "Rerank 超时(秒)", type: "number" },
      { key: "batch_process_size", label: "批处理大小", type: "number" },
    ],
  },
  {
    id: "generation",
    label: "生成",
    fields: [
      { key: "generation_concurrency", label: "生成并发", type: "number" },
      { key: "generation_adaptive_concurrency", label: "自适应并发", type: "checkbox" },
      { key: "generation_smart_routing", label: "智能路由", type: "checkbox" },
      { key: "generation_hard_use_pro", label: "难题使用备用模型", type: "checkbox" },
      { key: "generation_force_flash", label: "仅使用主模型", type: "checkbox" },
      { key: "generation_pro_concurrency", label: "备用模型并发", type: "number" },
      { key: "automatic_general_fallback", label: "自动通识兜底", type: "checkbox" },
    ],
  },
]

const PROVIDERS = [
  { id: "deepseek", label: "DeepSeek", protocol: "OpenAI 兼容" },
  { id: "claude", label: "Claude", protocol: "Anthropic Messages" },
  { id: "gemini", label: "Gemini", protocol: "Gemini generateContent" },
  { id: "custom", label: "自定义服务", protocol: "" },
]
const activeTab = ref("api")
const draft = reactive<Record<string, string>>({ llm_provider: "deepseek" })
const providerId = computed(() => draft.llm_provider || "deepseek")
const provider = computed(() => PROVIDERS.find(p => p.id === providerId.value) || PROVIDERS[0]!)
const hardModelKey = computed(() => providerId.value === "deepseek" ? "generation_hard_model" : `${providerId.value}_hard_model`)
const credentialDraft = reactive<Record<string, string>>({})
const saving = ref(false)
const testing = reactive<Record<string, boolean>>({})
const testResults = reactive<Record<string, { ok: boolean; message: string; elapsed_ms?: number } | null>>({})
const pending = new Map<string, string>()
const build = ref<{version?:string;buildId?:string;builtAt?:string}>({})

function invalidateTests() {
  pending.clear()
  for (const key of Object.keys(testing)) testing[key] = false
  for (const key of Object.keys(testResults)) testResults[key] = null
}
watch([draft, credentialDraft], invalidateTests, { deep: true, flush: "sync" })
watch(() => props.open, async (open) => {
  invalidateTests()
  for (const key of Object.keys(credentialDraft)) credentialDraft[key] = ""
  if (!open) return
  activeTab.value = "api"
  try {
    await settings.load()
    for (const key of Object.keys(draft)) delete draft[key]
    Object.assign(draft, settings.settings)
  } catch { toast("设置加载失败", "destructive") }
}, { immediate: true })

onMounted(() => {
  invoke<typeof build.value>("settings", "build_info").then(value => { build.value = value }).catch(() => {})
  onApiTestResult((result) => {
    if (!props.open || !result.request_id || pending.get(result.provider) !== result.request_id) return
    pending.delete(result.provider)
    testing[result.provider] = false
    testResults[result.provider] = result
  }).catch(() => {})
})

async function save() {
  if (saving.value) return
  saving.value = true
  try {
    const payload = Object.fromEntries(Object.entries(draft).map(([key, value]) => [key, String(value)]))
    const keys = Object.fromEntries(Object.entries(credentialDraft).filter(([, value]) => value.trim()).map(([key, value]) => [key, value.trim()]))
    await settings.save(payload, keys)
    toast("设置已保存，新任务将使用所选模型服务", "success")
    emit("update:open", false)
  } catch (e: any) { toast(`保存失败：${e?.message ?? e}`, "destructive") }
  finally { saving.value = false }
}

async function testConnection(id: string) {
  if (testing[id]) return
  const requestId = `${Date.now()}-${Math.random().toString(36).slice(2)}`
  pending.set(id, requestId)
  testing[id] = true
  testResults[id] = null
  try { await settings.test(id, { ...draft }, credentialDraft[id]?.trim() || "", requestId) }
  catch (e: any) {
    if (pending.get(id) !== requestId) return
    pending.delete(id)
    testing[id] = false
    testResults[id] = { ok: false, message: e?.message ?? String(e) }
  }
}

const inputClass =
  "h-9 w-full rounded-md border border-border bg-background px-3 text-sm outline-none transition-colors duration-fast focus:border-primary"
</script>

<template>
  <div v-if="open" class="fixed inset-0 z-50 flex items-center justify-center">
    <div class="absolute inset-0 bg-black/45" @click="emit('update:open', false)"></div>

    <div class="relative flex h-[560px] w-[640px] max-w-[calc(100vw-4rem)] flex-col rounded-lg bg-background shadow-overlay">
      <div class="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
        <span class="text-base font-semibold">设置</span>
        <button
          class="flex h-8 w-8 items-center justify-center rounded-md text-foreground-secondary transition-colors duration-fast hover:bg-muted"
          @click="emit('update:open', false)"
        >
          <PhX :size="18" />
        </button>
      </div>

      <div class="flex shrink-0 gap-1 border-b border-border px-4 pt-2">
        <button
          v-for="group in GROUPS"
          :key="group.id"
          class="rounded-t-md px-3 py-2 text-sm transition-colors duration-fast"
          :class="activeTab === group.id
            ? 'bg-primary-soft font-medium text-primary'
            : 'text-foreground-secondary hover:bg-muted'"
          @click="activeTab = group.id"
        >
          {{ group.label }}
        </button>
      </div>

      <div class="min-h-0 flex-1 overflow-y-auto p-4">
        <div v-if="activeTab === 'api'" class="space-y-4">
          <label class="block text-sm">用于题目整理与解析的服务
            <select v-model="draft.llm_provider" :class="inputClass" aria-label="模型服务">
              <option v-for="item in PROVIDERS" :key="item.id" :value="item.id">{{ item.label }}</option>
            </select>
          </label>
          <label v-if="providerId === 'custom'" class="block text-sm">接口协议
            <select v-model="draft.custom_protocol" :class="inputClass" aria-label="接口协议">
              <option value="openai">OpenAI 兼容（Chat Completions）</option>
              <option value="anthropic">Claude 原生（Messages）</option>
              <option value="gemini">Gemini 原生（generateContent）</option>
            </select>
          </label>
          <p v-else class="text-xs text-foreground-secondary">{{ provider.protocol }}；使用中转服务时，可选择“自定义服务”并指定对应协议。</p>
          <label class="block text-sm">API URL
            <input v-model="draft[`${providerId}_base_url`]" :class="inputClass" aria-label="API URL" placeholder="https://服务地址/v1" spellcheck="false" />
          </label>
          <label class="block text-sm">模型名称
            <input v-model="draft[`${providerId}_model`]" :class="inputClass" aria-label="模型名称" placeholder="填写服务商提供的模型 ID" spellcheck="false" />
          </label>
          <label class="block text-sm">{{ provider.label }} API Key
            <span class="text-xs text-foreground-secondary">{{ settings.credentials[providerId] ? '（已保存，留空沿用；更换地址需重填）' : '（未配置）' }}</span>
            <input v-model="credentialDraft[providerId]" type="password" :class="inputClass" aria-label="模型 API Key" autocomplete="off" placeholder="请输入 API Key" />
          </label>
          <button class="h-9 rounded-md border border-border px-3 text-sm disabled:opacity-40" :disabled="testing[providerId]" @click="testConnection(providerId)">
            {{ testing[providerId] ? '测试中…' : '测试模型连通性' }}
          </button>
          <p v-if="testResults[providerId]" class="break-words text-xs" :class="testResults[providerId]!.ok ? 'text-success' : 'text-destructive'" role="status">
            {{ testResults[providerId]!.message }} <span v-if="testResults[providerId]!.elapsed_ms !== undefined">（{{ testResults[providerId]!.elapsed_ms }} ms）</span>
          </p>
          <p class="text-xs text-foreground-secondary">测试使用当前填写内容，实际请求一次模型，会产生少量 API 用量。测试不会保存配置；保存后对新任务生效。密钥存入 Windows 凭据管理器。</p>
          <details class="text-sm"><summary class="cursor-pointer">可选：难题备用模型</summary>
            <input v-model="draft[hardModelKey]" :class="inputClass" aria-label="难题备用模型" placeholder="同一服务下的备用模型 ID，留空使用主模型" />
          </details>
          <div class="border-t border-border pt-3">
            <label class="block text-sm">DashScope API Key（教材向量、Rerank 和云端 OCR）
              <input v-model="credentialDraft.dashscope" type="password" :class="inputClass" autocomplete="off" :placeholder="settings.credentials.dashscope ? '已保存，输入以更换' : '请输入 DashScope API Key'" />
            </label>
            <button class="mt-2 h-9 rounded-md border border-border px-3 text-sm" :disabled="testing.dashscope" @click="testConnection('dashscope')">{{ testing.dashscope ? '测试中…' : '测试向量模型' }}</button>
            <p v-if="testResults.dashscope" class="mt-1 break-words text-xs" :class="testResults.dashscope.ok ? 'text-success' : 'text-destructive'">{{ testResults.dashscope.message }}</p>
            <p class="mt-1 text-xs text-foreground-secondary">地址与模型在“向量 / OCR”中配置；测试使用该页当前填写内容。</p>
          </div>
        </div>

        <div v-else class="space-y-4">
          <template v-for="group in GROUPS" :key="group.id">
            <template v-if="activeTab === group.id">
              <div v-for="field in group.fields" :key="field.key">
                <label v-if="field.type === 'checkbox'" class="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    class="h-4 w-4 accent-primary"
                    :checked="draft[field.key] === 'true'"
                    @change="draft[field.key] = ($event.target as HTMLInputElement).checked ? 'true' : 'false'"
                  />
                  {{ field.label }}
                </label>
                <template v-else>
                  <label class="mb-1 block text-xs text-foreground-secondary">{{ field.label }}</label>
                  <input
                    v-model="draft[field.key]"
                    :type="field.type"
                    :class="inputClass"
                  />
                </template>
              </div>
            </template>
          </template>
        </div>
      </div>

      <div class="flex shrink-0 items-center justify-end gap-2 border-t border-border p-4">
        <p class="mr-auto text-xs text-foreground-secondary" aria-label="程序构建信息">版本 {{build.version || '未知'}} · {{build.buildId || '未知构建'}}<br />{{build.builtAt}}</p>
        <button
          class="h-9 rounded-md border border-border px-4 text-sm text-foreground-secondary transition-colors duration-fast hover:bg-muted"
          @click="emit('update:open', false)"
        >
          取消
        </button>
        <button
          class="h-9 rounded-md bg-primary px-4 text-sm text-onprimary transition-colors duration-fast hover:bg-primary-hover disabled:opacity-40"
          :disabled="saving"
          @click="save"
        >
          {{ saving ? "保存中…" : "保存" }}
        </button>
      </div>
    </div>
  </div>
</template>
