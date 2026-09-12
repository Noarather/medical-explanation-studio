import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock }))

import { useImportsStore } from "../imports"

describe("imports store", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    invokeMock.mockReset()
  })

  it("pickFile 保存路径、探测结果与默认名称", async () => {
    invokeMock
      .mockResolvedValueOnce({ path: "D:/题库/外科.xlsx" }) // pick_import_file
      .mockResolvedValueOnce({ // inspect_file
        kind: "excel", name: "外科", headers: ["题号", "题干"],
        mapping: { id: "题号", question: "题干" }, fields: ["id", "question"],
      })
    const store = useImportsStore()
    await store.pickFile()
    expect(invokeMock).toHaveBeenNthCalledWith(1, "imports", "pick_import_file", {})
    expect(invokeMock).toHaveBeenNthCalledWith(2, "imports", "inspect_file", { path: "D:/题库/外科.xlsx" })
    expect(store.setName).toBe("外科")
    expect(store.mapping).toEqual({ id: "题号", question: "题干" })
  })

  it("pickFile 取消时不探测", async () => {
    invokeMock.mockResolvedValueOnce({ path: "" })
    const store = useImportsStore()
    await store.pickFile()
    expect(invokeMock).toHaveBeenCalledTimes(1)
    expect(store.inspect).toBeNull()
  })

  it("importFile 仅对 Excel 携带映射，成功后刷新题目集", async () => {
    const store = useImportsStore()
    store.filePath = "D:/a.xlsx"
    store.setName = "外科"
    store.inspect = { kind: "excel", name: "外科", headers: [], mapping: {}, fields: [] }
    store.mapping = { id: "题号" }
    invokeMock
      .mockResolvedValueOnce({ set_id: "s1", name: "外科", question_count: 3 }) // import_file
      .mockResolvedValueOnce({ sets: [{ id: "s1", name: "外科", question_count: 3 }] }) // list_sets
    await store.importFile()
    expect(invokeMock).toHaveBeenNthCalledWith(1, "imports", "import_file", {
      path: "D:/a.xlsx", name: "外科", mapping: { id: "题号" },
    })
    expect(store.lastImported?.question_count).toBe(3)
    expect(store.sets).toHaveLength(1)
  })

  it("importFile 对 JSON 不携带映射", async () => {
    const store = useImportsStore()
    store.filePath = "D:/a.json"
    store.setName = "外科"
    store.inspect = { kind: "json", name: "外科", headers: [], mapping: {} }
    invokeMock
      .mockResolvedValueOnce({ set_id: "s1", name: "外科", question_count: 1 })
      .mockResolvedValueOnce({ sets: [] })
    await store.importFile()
    expect(invokeMock).toHaveBeenNthCalledWith(1, "imports", "import_file", {
      path: "D:/a.json", name: "外科", mapping: null,
    })
  })

  it("importFile 失败时错误向外抛出且不写结果", async () => {
    const store = useImportsStore()
    store.filePath = "D:/bad.json"
    store.setName = "坏题"
    store.inspect = { kind: "json", name: "坏题", headers: [], mapping: {} }
    invokeMock.mockRejectedValueOnce({ code: "import_invalid", message: "第 1 条缺少字段：answer" })
    await expect(store.importFile()).rejects.toMatchObject({ code: "import_invalid" })
    expect(store.lastImported).toBeNull()
  })

  it("estimateUpgrade 与 enqueueUpgrade 走 jobs 域入队", async () => {
    const store = useImportsStore()
    store.sets = [{ id: "s1", name: "外科", question_count: 10 }]
    invokeMock.mockResolvedValueOnce({ questions: 10, estimated_requests: 8, skipped: 2 })
    await store.estimateUpgrade("s1")
    expect(store.estimate?.estimated_requests).toBe(8)
    invokeMock.mockResolvedValueOnce({ job_id: "j1" })
    await store.enqueueUpgrade()
    expect(invokeMock).toHaveBeenLastCalledWith("jobs", "enqueue", {
      job_type: "upgrade_v2", title: "升级 v2：外科", payload: { set_id: "s1" },
    })
    expect(store.estimate).toBeNull()
  })

  it("有异常时只保存即时处理预览，不误报导入成功", async () => {
    const store = useImportsStore()
    const review = {draft_id:'d',name:'合成',path:'a.json',total:2,ready:1,issues:1,removed:0,repaired:0,items:[]}
    invokeMock.mockResolvedValueOnce({needs_review:true,review})
    expect(await store.importFile()).toBe(false)
    expect(store.fileReview).toEqual(review)
    expect(store.lastImported).toBeNull()
    expect(invokeMock).toHaveBeenCalledTimes(1)
    invokeMock.mockResolvedValueOnce({...review,issues:0,removed:1})
    await store.resolveFileImport('remove',0)
    expect(invokeMock).toHaveBeenLastCalledWith('imports','resolve_file_import',{draft_id:'d',action:'remove',index:0})
    invokeMock.mockResolvedValueOnce({set_id:'s',name:'合成',question_count:1}).mockResolvedValueOnce({sets:[]})
    await store.commitFileImport()
    expect(store.lastImported?.question_count).toBe(1)
    expect(store.fileReview).toBeNull()
  })

  it("修正请求失败时保留原预览便于重试", async () => {
    const store = useImportsStore()
    const review = {draft_id:'d',name:'合成',path:'a.json',total:1,ready:0,issues:1,removed:0,repaired:0,items:[]}
    store.fileReview = review
    invokeMock.mockRejectedValueOnce({message:'校验请求失败'})
    await expect(store.resolveFileImport('edit',0,{answer:'A'})).rejects.toMatchObject({message:'校验请求失败'})
    expect(store.fileReview).toEqual(review)
    expect(store.importing).toBe(false)
  })
})
