import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock }))

import { useQuestionsStore, STATUS_OPTIONS } from "../questions"

const ROW = {
  id: 1, set_id: "s1", external_id: "a1", subject: "外科学", question_type: "A1",
  prompt_text: "题干", pipeline_status: "queued", review_status: "pending",
  generation_mode: null, match_score: null, error_message: null, updated_at: "2026-08-06",
  question_source: "医考帮", tags: ["休克"], evidence_grade: "",
}

describe("questions store", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    invokeMock.mockReset()
  })

  it("load 携带筛选与分页并写入总数", async () => {
    invokeMock.mockResolvedValueOnce({ rows: [ROW], total: 41 })
    const store = useQuestionsStore()
    store.filters.subject = "外科学"
    store.page = 1
    await store.load()
    expect(invokeMock).toHaveBeenCalledWith("questions", "list", {
      set_id: "", review_status: "", pipeline_status: "", subject: "外科学",
      tag: "", question_source: "", search: "", limit: 200, offset: 200,
    })
    expect(store.rows).toHaveLength(1)
    expect(store.total).toBe(41)
    expect(store.pageCount).toBe(1) // 41 条 / 200 一页
  })

  it("状态选项同时设置 review 与 pipeline 两个键", async () => {
    invokeMock.mockResolvedValue({ rows: [], total: 0 })
    const store = useQuestionsStore()
    store.applyStatusOption(STATUS_OPTIONS[2]) // 已生成待审核
    expect(store.filters.review_status).toBe("pending")
    expect(store.filters.pipeline_status).toBe("generated")
    await vi.waitFor(() => expect(invokeMock).toHaveBeenCalled())
    expect(store.page).toBe(0)
  })

  it("选择全部筛选结果走 ids 端点", async () => {
    invokeMock.mockResolvedValueOnce({ ids: [1, 2, 3] })
    const store = useQuestionsStore()
    await store.selectAllFiltered()
    expect(invokeMock).toHaveBeenCalledWith("questions", "ids", expect.objectContaining({ search: "" }))
    expect(store.selected).toEqual([1, 2, 3])
  })

  it("移入回收站后刷新并清空选择", async () => {
    invokeMock
      .mockResolvedValueOnce({ moved: 2 }) // move_to_trash
      .mockResolvedValueOnce({ rows: [], total: 0 }) // load
    const store = useQuestionsStore()
    store.selected = [1, 2]
    await store.moveSelectedToTrash()
    expect(invokeMock).toHaveBeenNthCalledWith(1, "questions", "move_to_trash", { question_pks: [1, 2] })
    expect(store.selected).toEqual([])
  })

  it("导出选中先弹保存对话框再导出", async () => {
    invokeMock
      .mockResolvedValueOnce({ path: "D:/导出.json" }) // imports.pick_save_path
      .mockResolvedValueOnce({ path: "D:/导出.json", count: 2 }) // questions.export_selected
    const store = useQuestionsStore()
    store.selected = [1, 2]
    await store.exportSelected()
    expect(invokeMock).toHaveBeenNthCalledWith(1, "imports", "pick_save_path", {
      default_name: "选中题目.json", file_filter: "JSON 文件 (*.json)",
    })
    expect(invokeMock).toHaveBeenNthCalledWith(2, "questions", "export_selected", {
      question_pks: [1, 2], path: "D:/导出.json",
    })
    expect(store.lastExportCount).toBe(2)
  })

  it("导出取消对话框时不调用导出", async () => {
    invokeMock.mockResolvedValueOnce({ path: "" })
    const store = useQuestionsStore()
    store.selected = [1]
    await store.exportSelected()
    expect(invokeMock).toHaveBeenCalledTimes(1)
  })

  it("回收站恢复返回冲突列表", async () => {
    invokeMock
      .mockResolvedValueOnce({ restored: 1, conflicts: ["a2"] }) // trash_restore
      .mockResolvedValueOnce({ rows: [], total: 1 }) // loadTrash
    const store = useQuestionsStore()
    store.trashSelected = [11, 12]
    const result = await store.restoreSelected()
    expect(invokeMock).toHaveBeenNthCalledWith(1, "questions", "trash_restore", { trash_ids: [11, 12] })
    expect(result).toEqual({ restored: 1, conflicts: ["a2"] })
    expect(store.trashSelected).toEqual([])
    expect(store.trashTotal).toBe(1)
  })

  it("回收站彻底清除后刷新", async () => {
    invokeMock
      .mockResolvedValueOnce({ purged: 1 })
      .mockResolvedValueOnce({ rows: [], total: 0 })
    const store = useQuestionsStore()
    store.trashSelected = [11]
    await store.purgeSelected()
    expect(invokeMock).toHaveBeenNthCalledWith(1, "questions", "trash_purge", { trash_ids: [11] })
    expect(store.trashTotal).toBe(0)
  })
})
