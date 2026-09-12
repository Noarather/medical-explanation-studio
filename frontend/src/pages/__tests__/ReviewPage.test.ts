import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock }))

import ReviewPage from "../ReviewPage.vue"
import { useReviewStore } from "../../stores/review"

const ROW = {
  id: 1, external_id: "Q-1", subject: "内科", question_type: "A1",
  prompt_text: "题干", pipeline_status: "generated", review_status: "pending",
  match_score: null, evidence_grade: "", updated_at: "",
}

const DETAIL = {
  id: 1, external_id: "Q-1", subject: "内科", question_type: "A1",
  explanation: "解析",
  raw: {
    question: "题干",
    answer: ["A"],
    options: ["A. 甲"],
    explanationBlocks: [
      { blockId: "b01", section: "analysis", type: "paragraph", title: "考点解析", text: "内容" },
    ],
  },
}

function mountPage() {
  return mount(ReviewPage, {
    global: { provide: { toast: () => {} } },
  })
}

/** 搜索按钮：紧跟在搜索输入框后的同级按钮（页头有快速审核等按钮，不能按序取第一个） */
function searchButton(wrapper: ReturnType<typeof mountPage>) {
  return wrapper.find('input[placeholder="搜索 ID 或题干"] + button')
}

function invokeCalls(): [string, string][] {
  return invokeMock.mock.calls.map(([domain, method]) => [domain, method] as [string, string])
}

describe("ReviewPage 未保存修改守卫", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    invokeMock.mockReset()
    invokeMock.mockImplementation((_domain: string, method: string) => {
      if (method === "filters") return Promise.resolve({ sets: [], subjects: [], sources: [], tags: [] })
      if (method === "list") return Promise.resolve({ rows: [ROW], total: 1 })
      if (method === "open") return Promise.resolve(DETAIL)
      if (method === "save") return Promise.resolve({ saved: true, question: { explanation: "解析" } })
      return Promise.resolve({})
    })
  })

  it("有未保存修改时触发搜索：先 confirm，保存后再发起新的 list", async () => {
    const confirmSpy = vi.fn(() => true)
    vi.stubGlobal("confirm", confirmSpy)

    const store = useReviewStore()
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAll("button").find((b) => b.text().includes("Q-1"))!.trigger("click")
    await flushPromises()
    expect(store.currentId).toBe(1)

    store.dirty = true
    invokeMock.mockClear()
    await searchButton(wrapper).trigger("click")
    await flushPromises()

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    const calls = invokeCalls()
    const saveIndex = calls.findIndex(([d, m]) => d === "review" && m === "save")
    expect(saveIndex).toBeGreaterThanOrEqual(0)
    const listAfterSave = calls.findIndex(([d, m], i) => d === "review" && m === "list" && i > saveIndex)
    expect(listAfterSave).toBeGreaterThan(saveIndex)
    expect(store.dirty).toBe(false)

    vi.unstubAllGlobals()
  })

  it("无未保存修改时触发搜索：不弹 confirm，直接刷新列表", async () => {
    const confirmSpy = vi.fn(() => true)
    vi.stubGlobal("confirm", confirmSpy)

    const store = useReviewStore()
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAll("button").find((b) => b.text().includes("Q-1"))!.trigger("click")
    await flushPromises()
    expect(store.dirty).toBe(false)

    invokeMock.mockClear()
    await searchButton(wrapper).trigger("click")
    await flushPromises()

    expect(confirmSpy).not.toHaveBeenCalled()
    const calls = invokeCalls()
    expect(calls.some(([d, m]) => d === "review" && m === "save")).toBe(false)
    expect(calls.some(([d, m]) => d === "review" && m === "list")).toBe(true)

    vi.unstubAllGlobals()
  })
})
