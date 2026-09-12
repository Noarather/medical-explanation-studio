import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"

const { invokeMock, resultCbs, progressCbs } = vi.hoisted(() => ({
  invokeMock: vi.fn(),
  resultCbs: [] as Array<(payload: unknown) => void>,
  progressCbs: [] as Array<(payload: unknown) => void>,
}))
vi.mock("../../lib/bridge", () => ({
  invoke: invokeMock,
  onOrganizeResult: (cb: (payload: unknown) => void) => { resultCbs.push(cb); return Promise.resolve() },
  onOrganizeProgress: (cb: (payload: unknown) => void) => { progressCbs.push(cb); return Promise.resolve() },
}))

import { useOrganizeStore } from "../organize"

const ROW_A = { id: "a1", subject: "内科学", type: "A1", question: "题干甲", options: ["A. 一", "B. 二"], answer: "A" }
const ROW_B = { id: "b1", subject: "", type: "A1", question: "题干乙", options: ["A. 一", "B. 二"], answer: "" }

describe("organize store", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    invokeMock.mockReset()
    resultCbs.length = 0
    progressCbs.length = 0
  })

  it("整理结果按 token 接收并触发校验", async () => {
    const store = useOrganizeStore()
    await store.init()
    store.rawText = "1. 题干\nA. 一\nB. 二\n答案：A"
    invokeMock.mockResolvedValueOnce({ started: true, token: "organize-1" }) // organize
    invokeMock.mockResolvedValueOnce({ warnings: [[], ["缺少字段：answer"]], valid_count: 1 }) // validate_rows
    const pending = store.organize()
    resultCbs[0]({
      token: store.activeToken, ok: true,
      data: { rows: [ROW_A, ROW_B], method: "local" },
    })
    await pending
    // handleResult 先写 rows 再 await revalidate，等 warnings 落地再断言
    await vi.waitFor(() => expect(store.warnings).toEqual([[], ["缺少字段：answer"]]))
    expect(store.rows).toHaveLength(2)
    expect(store.running).toBe(false)
    expect(store.filter).toBe("invalid")
    expect(store.invalidCount).toBe(1)
  })

  it("过期 token 的结果被忽略", async () => {
    const store = useOrganizeStore()
    await store.init()
    store.activeToken = "current"
    await store.handleResult({ token: "stale", ok: true, data: { rows: [ROW_A], method: "local" } } as never)
    expect(store.rows).toHaveLength(0)
  })

  it("过滤与分页", async () => {
    const store = useOrganizeStore()
    store.rows = Array.from({ length: 250 }, (_, i) => ({ ...ROW_A, id: `q${i}` }))
    store.warnings = store.rows.map((_, i) => (i < 5 ? ["缺答案"] : []))
    store.filter = "invalid"
    expect(store.filteredIndices).toEqual([0, 1, 2, 3, 4])
    store.filter = "valid"
    expect(store.pageCount).toBe(2)
    expect(store.pageIndices).toHaveLength(200)
    store.page = 1
    expect(store.pageIndices).toHaveLength(45)
  })

  it("批量设置校验枚举并作用于选中行", async () => {
    invokeMock.mockResolvedValue({ warnings: [[], []], valid_count: 2 })
    const store = useOrganizeStore()
    store.rows = [structuredClone(ROW_A), structuredClone(ROW_B)]
    store.warnings = [[], []]
    store.selected = [1]
    expect(() => store.applyBulk("type", "B型", "selected")).toThrow("题型")
    store.applyBulk("subject", "外科学", "selected")
    await vi.waitFor(() => expect(store.rows[1].subject).toBe("外科学"))
    expect(store.rows[0].subject).toBe("内科学")
  })

  it("导入全部可用只发送无错误行并保留待调整", async () => {
    const store = useOrganizeStore()
    store.name = "整理集"
    store.bank = "school"
    store.rows = [structuredClone(ROW_A), structuredClone(ROW_B)]
    store.warnings = [[], ["缺少字段：answer"]]
    invokeMock.mockResolvedValueOnce({ set_id: "s1", imported: 1 }) // import_rows
    invokeMock.mockResolvedValueOnce({ warnings: [["缺少字段：answer"]], valid_count: 0 }) // validate_rows
    await store.importValid()
    expect(invokeMock).toHaveBeenNthCalledWith(1, "imports", "import_rows", {
      name: "整理集", rows: [expect.objectContaining({ id: "a1" })],
      source_path: "pasted://manual", set_id: "",
    })
    expect(store.partialSetId).toBe("s1")
    expect(store.importedCount).toBe(1)
    expect(store.rows).toHaveLength(1)
    expect(store.rows[0].id).toBe("b1")

    store.warnings = [[]]
    invokeMock.mockResolvedValueOnce({ set_id: "s1", imported: 1 })
    invokeMock.mockResolvedValueOnce({ warnings: [], valid_count: 0 })
    await store.importValid()
    expect(invokeMock).toHaveBeenNthCalledWith(3, "imports", "import_rows",
      expect.objectContaining({ set_id: "s1", name: "整理集" }))
    expect(store.rows).toHaveLength(0)
    expect(store.status).toContain("已导入全部 2 道")
  })

  it("导入进行中重入被忽略，不重复发起 import_rows", async () => {
    const store = useOrganizeStore()
    store.name = "整理集"
    store.bank = "school"
    store.rows = [structuredClone(ROW_A)]
    store.warnings = [[]]
    invokeMock.mockImplementationOnce(() => new Promise(() => {})) // import_rows 悬挂
    const first = store.importValid()
    expect(store.importing).toBe(true)
    await store.importValid() // 重入应直接返回
    expect(invokeMock.mock.calls.filter((call) => call[1] === "import_rows")).toHaveLength(1)
    first.catch(() => {}) // 悬挂的 promise 不会 settle，仅避免未处理拒绝告警
  })

  it("新一轮整理结果重置导入会话状态，过期结果不重置", async () => {
    const store = useOrganizeStore()
    await store.init()
    store.partialSetId = "s1"
    store.importedCount = 3
    store.importedIds = ["a1"]
    store.activeToken = "current"
    invokeMock.mockResolvedValueOnce({ warnings: [[]], valid_count: 1 }) // validate_rows
    await store.handleResult({ token: "current", ok: true, data: { rows: [ROW_A], method: "local" } } as never)
    expect(store.partialSetId).toBe("")
    expect(store.importedCount).toBe(0)
    expect(store.importedIds).toEqual([])

    store.partialSetId = "s2"
    store.importedCount = 5
    store.importedIds = ["b1"]
    await store.handleResult({ token: "stale", ok: true, data: { rows: [ROW_A], method: "local" } } as never)
    expect(store.partialSetId).toBe("s2")
    expect(store.importedCount).toBe(5)
    expect(store.importedIds).toEqual(["b1"])
  })
})
