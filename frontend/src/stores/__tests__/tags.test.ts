import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock }))

import { useTagsStore } from "../tags"

const TAGS = [
  { id: 1, set_id: "s1", label: "休克", subject: "外科学", status: "active", usage_count: 3, merged_into: "", aliases: [] },
  { id: 2, set_id: "s1", label: "感染", subject: "外科学", status: "candidate", usage_count: 1, merged_into: "", aliases: [] },
  { id: 3, set_id: "s1", label: "败血症", subject: "内科学", status: "active", usage_count: 4, merged_into: "", aliases: [] },
]

describe("tags store", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    invokeMock.mockReset()
  })

  it("init 默认选中第一个题目集并加载标签", async () => {
    invokeMock
      .mockResolvedValueOnce({ sets: [{ id: "s1", name: "外科", question_count: 3 }], subjects: [], sources: [], tags: [] })
      .mockResolvedValueOnce({ subjects: ["外科学"] })
      .mockResolvedValueOnce({ tags: TAGS })
    const store = useTagsStore()
    await store.init()
    expect(store.setId).toBe("s1")
    expect(invokeMock).toHaveBeenNthCalledWith(3, "tags", "list",
      expect.objectContaining({ set_id: "s1" }))
    expect(store.tags).toHaveLength(3)
  })

  it("无题目集时不请求标签列表", async () => {
    invokeMock.mockResolvedValueOnce({ sets: [], subjects: [], sources: [], tags: [] })
    const store = useTagsStore()
    await store.init()
    expect(invokeMock).toHaveBeenCalledTimes(1)
    expect(store.tags).toEqual([])
  })

  it("mergeTargets 只含同学科 active 且非自身的标签", async () => {
    invokeMock.mockResolvedValueOnce({ tags: TAGS })
    const store = useTagsStore()
    store.setId = "s1"
    await store.load()
    store.selectedId = 2
    expect(store.mergeTargets.map((row) => row.id)).toEqual([1])
    store.selectedId = 1
    expect(store.mergeTargets).toEqual([])
  })

  it("rename/activate/merge/delete 调用后重载", async () => {
    invokeMock.mockResolvedValue({ tags: TAGS })
    const store = useTagsStore()
    store.setId = "s1"

    invokeMock.mockResolvedValueOnce({ updated: true })
    await store.rename(TAGS[1], "细菌感染")
    expect(invokeMock).toHaveBeenNthCalledWith(1, "tags", "rename", { tag_id: 2, label: "细菌感染" })

    invokeMock.mockResolvedValueOnce({ updated: true })
    await store.activate(TAGS[1])
    expect(invokeMock).toHaveBeenNthCalledWith(3, "tags", "activate", { tag_id: 2 })

    invokeMock.mockResolvedValueOnce({ changed: 1 })
    const changed = await store.merge(2, 1)
    expect(changed).toBe(1)

    invokeMock.mockResolvedValueOnce({ deleted: "感染", changed: 1 })
    const result = await store.remove(TAGS[1], "感染")
    expect(result).toEqual({ deleted: "感染", changed: 1 })
    expect(invokeMock).toHaveBeenNthCalledWith(7, "tags", "delete", { tag_id: 2, confirm_label: "感染" })
  })
})
