import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock }))

import UpgradeCard from "../UpgradeCard.vue"
import { useImportsStore } from "../../stores/imports"

describe("UpgradeCard", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    invokeMock.mockReset()
  })

  it("选择题目集后估算并确认入队", async () => {
    const store = useImportsStore()
    store.sets = [{ id: "s1", name: "外科", question_count: 10 }]
    const wrapper = mount(UpgradeCard, {
      global: { provide: { toast: () => {}, openJobs: () => {} } },
    })
    await wrapper.find("select").setValue("s1")
    invokeMock.mockResolvedValueOnce({ questions: 10, estimated_requests: 8, skipped: 2 })
    await wrapper.find("[data-test=estimate]").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("10")
    invokeMock.mockResolvedValueOnce({ job_id: "j1" })
    await wrapper.find("[data-test=confirm]").trigger("click")
    await flushPromises()
    expect(invokeMock).toHaveBeenLastCalledWith("jobs", "enqueue", {
      job_type: "upgrade_v2", title: "升级 v2：外科", payload: { set_id: "s1" },
    })
  })

  it("切换题目集后清空旧估算结果", async () => {
    const store = useImportsStore()
    store.sets = [
      { id: "s1", name: "外科", question_count: 10 },
      { id: "s2", name: "内科", question_count: 20 },
    ]
    const wrapper = mount(UpgradeCard, {
      global: { provide: { toast: () => {}, openJobs: () => {} } },
    })
    await wrapper.find("select").setValue("s1")
    invokeMock.mockResolvedValueOnce({ questions: 10, estimated_requests: 8, skipped: 2 })
    await wrapper.find("[data-test=estimate]").trigger("click")
    await flushPromises()
    expect(store.estimate).not.toBeNull()
    expect(wrapper.text()).toContain("10")
    await wrapper.find("select").setValue("s2")
    await flushPromises()
    expect(store.estimate).toBeNull()
    expect(wrapper.text()).not.toContain("共 10 题")
  })
})
