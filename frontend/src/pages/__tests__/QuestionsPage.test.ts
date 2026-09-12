import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock }))

import QuestionsPage from "../QuestionsPage.vue"
import { useQuestionsStore } from "../../stores/questions"

describe("QuestionsPage", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    invokeMock.mockReset()
    invokeMock.mockImplementation((_domain: string, method: string) => {
      if (method === "filters") return Promise.resolve({ sets: [], subjects: [], sources: [], tags: [] })
      if (method === "list") return Promise.resolve({ rows: [], total: 0 })
      return Promise.resolve({})
    })
  })

  it("状态下拉显示与 store 中持久化的筛选保持一致", async () => {
    const store = useQuestionsStore()
    store.filters.review_status = "pending"
    store.filters.pipeline_status = "generated"
    const wrapper = mount(QuestionsPage, {
      global: { provide: { toast: () => {} } },
    })
    await flushPromises()
    const statusSelect = wrapper.findAll("select")[1]
    expect((statusSelect.element as HTMLSelectElement).value).toBe("已生成待审核")
  })
})
