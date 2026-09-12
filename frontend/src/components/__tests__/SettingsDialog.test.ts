import { beforeEach, describe, expect, it, vi } from "vitest"
import { flushPromises, mount } from "@vue/test-utils"
import { createPinia } from "pinia"
import SettingsDialog from "../SettingsDialog.vue"

const { invokeMock, listener } = vi.hoisted(() => ({ invokeMock: vi.fn(), listener: { callback: null as any } }))
vi.mock("../../lib/bridge", () => ({
  invoke: invokeMock,
  onApiTestResult: (cb: any) => { listener.callback = cb; return Promise.resolve() },
}))
const saved = { llm_provider: "deepseek", deepseek_model: "saved-model", deepseek_base_url: "https://saved.test/v1",
  claude_base_url: "https://api.anthropic.com/v1", claude_model: "", custom_protocol: "openai" }

async function setup() {
  const wrapper = mount(SettingsDialog, { props: { open: true }, global: { plugins: [createPinia()] } })
  await flushPromises()
  return wrapper
}
function testButton(wrapper: any) {
  return wrapper.findAll("button").find((b: any) => b.text() === "测试模型连通性")!
}

describe("模型设置与连通性测试", () => {
  beforeEach(() => {
    invokeMock.mockReset()
    invokeMock.mockImplementation((_domain, method) => Promise.resolve(method === "get"
      ? { settings: { ...saved }, credentials: { deepseek: true, claude: false } }
      : { started: true, saved: true }))
  })
  it("用未保存的服务、URL、模型和密钥测试，测试不触发保存", async () => {
    const wrapper = await setup()
    await wrapper.get('[aria-label="模型服务"]').setValue("claude")
    await wrapper.get('[aria-label="API URL"]').setValue("https://draft.test/v1")
    await wrapper.get('[aria-label="模型名称"]').setValue("draft-model")
    await wrapper.get('[aria-label="模型 API Key"]').setValue("draft-key")
    await testButton(wrapper).trigger("click")
    await flushPromises()
    const payload = invokeMock.mock.calls.find((call) => call[1] === "test")![2]
    expect(payload).toMatchObject({ provider: "claude", api_key: "draft-key", settings: {
      llm_provider: "claude", claude_model: "draft-model", claude_base_url: "https://draft.test/v1" } })
    expect(payload.request_id).toBeTruthy()
    expect(invokeMock.mock.calls.some(call => call[1] === "save")).toBe(false)
    listener.callback({ provider: "claude", request_id: payload.request_id, ok: true, message: "调用成功", elapsed_ms: 31 })
    await flushPromises()
    expect(wrapper.text()).toContain("调用成功")
    expect(wrapper.text()).toContain("31 ms")
    wrapper.unmount()
  })
  it("编辑配置后丢弃旧测试结果，避免误报新地址可用", async () => {
    const wrapper = await setup()
    await testButton(wrapper).trigger("click")
    const payload = invokeMock.mock.calls.find((call) => call[1] === "test")![2]
    await wrapper.get('[aria-label="模型名称"]').setValue("changed-model")
    listener.callback({ provider: "deepseek", request_id: payload.request_id, ok: true, message: "旧模型成功" })
    await flushPromises()
    expect(wrapper.text()).not.toContain("旧模型成功")
    expect(testButton(wrapper).attributes("disabled")).toBeUndefined()
    wrapper.unmount()
  })
  it("自定义服务支持三种协议并保存各自配置", async () => {
    const wrapper = await setup()
    await wrapper.get('[aria-label="模型服务"]').setValue("custom")
    await wrapper.get('[aria-label="接口协议"]').setValue("gemini")
    await wrapper.get('[aria-label="API URL"]').setValue("https://gateway.test/v1beta")
    await wrapper.get('[aria-label="模型名称"]').setValue("my-model")
    await wrapper.get('[aria-label="模型 API Key"]').setValue("my-key")
    await wrapper.findAll("button").find(b => b.text() === "保存")!.trigger("click")
    await flushPromises()
    const payload = invokeMock.mock.calls.find(call => call[1] === "save")![2]
    expect(payload.settings).toMatchObject({ llm_provider: "custom", custom_protocol: "gemini", custom_model: "my-model", deepseek_model: "saved-model" })
    expect(payload.credentials).toEqual({ custom: "my-key" })
    expect(wrapper.emitted("update:open")).toEqual([[false]])
    wrapper.unmount()
  })
})
