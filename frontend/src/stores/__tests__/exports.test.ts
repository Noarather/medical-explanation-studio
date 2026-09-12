import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock }))

import { useExportsStore } from "../exports"

const SETS = [{ id: "s1", name: "外科", question_count: 10, approved_count: 6 }]

describe("exports store", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    invokeMock.mockReset()
  })

  it("init 加载题目集、默认目录与历史", async () => {
    invokeMock
      .mockResolvedValueOnce({ sets: SETS, subjects: [], sources: [], tags: [] })
      .mockResolvedValueOnce({ path: "C:/Users/X/Documents/医学题库解析" })
      .mockResolvedValueOnce({ records: [] })
    const store = useExportsStore()
    await store.init()
    expect(store.setId).toBe("s1")
    expect(store.outputDir).toContain("医学题库解析")
  })

  it("exportData 非拆分形态生成结果行并刷新历史", async () => {
    invokeMock
      .mockResolvedValueOnce({
        json: "D:/o/a-v2.json", xlsx: "D:/o/a-v2.xlsx",
        mapping: "D:/o/a-解析映射.json", full: "D:/o/a-v2.json",
      })
      .mockResolvedValueOnce({ records: [{ id: 1, export_type: "json_v2" }] })
    const store = useExportsStore()
    store.setId = "s1"
    store.outputDir = "D:/o"
    await store.exportData()
    expect(invokeMock).toHaveBeenNthCalledWith(1, "exports", "export", {
      set_id: "s1", output_dir: "D:/o", split_by_subject: false,
    })
    expect(store.resultLines.join("\n")).toContain("JSON v2：D:/o/a-v2.json")
    expect(store.resultLines.join("\n")).toContain("XLSX v2：D:/o/a-v2.xlsx")
    expect(store.resultLines.join("\n")).toContain("兼容解析映射：D:/o/a-解析映射.json")
    expect(store.history).toHaveLength(1)
  })

  it("exportData 拆分形态逐行显示文件", async () => {
    invokeMock
      .mockResolvedValueOnce({ files: ["D:/o/a-外科学-t-v2.json", "D:/o/a-外科学-t-v2.xlsx"], mapping: "D:/o/m.json" })
      .mockResolvedValueOnce({ records: [] })
    const store = useExportsStore()
    store.setId = "s1"
    store.outputDir = "D:/o"
    store.split = true
    await store.exportData()
    expect(store.resultLines[0]).toContain("拆分文件：D:/o/a-外科学-t-v2.json")
    expect(store.resultLines[2]).toContain("兼容解析映射：D:/o/m.json")
  })

  it("exportIssues 显示报告路径", async () => {
    invokeMock.mockResolvedValueOnce({ path: "D:/o/问题报告-t.csv" })
    const store = useExportsStore()
    store.setId = "s1"
    store.outputDir = "D:/o"
    await store.exportIssues()
    expect(store.resultLines[0]).toBe("问题报告：D:/o/问题报告-t.csv")
  })

  it("导出失败向外抛出", async () => {
    invokeMock.mockRejectedValueOnce({ code: "bad_state", message: "该题目集还没有已批准题目" })
    const store = useExportsStore()
    store.setId = "s1"
    await expect(store.exportData()).rejects.toMatchObject({ code: "bad_state" })
  })

  it("openFolder 与 pickDirectory", async () => {
    invokeMock
      .mockResolvedValueOnce({ path: "D:/chosen" })
      .mockResolvedValueOnce({ opened: true, path: "D:/chosen" })
    const store = useExportsStore()
    await store.pickDirectory()
    expect(store.outputDir).toBe("D:/chosen")
    await store.openFolder()
    expect(invokeMock).toHaveBeenNthCalledWith(2, "exports", "open_folder", { path: "D:/chosen" })
  })
})
