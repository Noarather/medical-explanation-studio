import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock }))

import { useLibrariesStore } from "../libraries"

const ROW = {
  id: 3, name: "外科学（第10版）", subject: "外科学", version: "第10版",
  root_path: "C:/fixtures/textbooks/book.pdf", page_offset: 39, file_count: 1, page_count: 834,
  ocr_page_count: 13, file_status: "ready", file_error: null, file_name: "book.pdf",
  index_fingerprint: "abc", index_state: "compatible",
}

describe("libraries store", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    invokeMock.mockReset()
  })

  it("load 写入教材与当前指纹", async () => {
    invokeMock.mockResolvedValueOnce({ libraries: [ROW], current_fingerprint: "abc" })
    const store = useLibrariesStore()
    await store.load()
    expect(store.libraries).toHaveLength(1)
    expect(store.currentFingerprint).toBe("abc")
  })

  it("较旧的异步响应不能覆盖索引完成后的状态", async () => {
    let oldResolve: (value: unknown) => void = () => {}
    invokeMock.mockImplementationOnce(() => new Promise(resolve => { oldResolve = resolve }))
      .mockResolvedValueOnce({ libraries: [ROW], current_fingerprint: "new" })
    const store = useLibrariesStore()
    const oldRequest = store.load()
    await store.load()
    oldResolve({ libraries: [{ ...ROW, file_count: 0, index_state: "none" }], current_fingerprint: "old" })
    await oldRequest
    expect(store.libraries[0].index_state).toBe("compatible")
    expect(store.currentFingerprint).toBe("new")
    expect(store.loading).toBe(false)
  })

  it("submitDialog 新增走 add 并重载", async () => {
    invokeMock
      .mockResolvedValueOnce({ library_id: 5 })
      .mockResolvedValueOnce({ libraries: [], current_fingerprint: "abc" })
    const store = useLibrariesStore()
    await store.submitDialog(null, {
      name: "内科学", subject: "内科学", root_path: "E:/a.pdf",
      version: "", pdf_anchor: 40, textbook_anchor: 1,
    })
    expect(invokeMock).toHaveBeenNthCalledWith(1, "library", "add",
      expect.objectContaining({ name: "内科学", page_offset: 39 }))
  })

  it("submitDialog 编辑走 update", async () => {
    invokeMock
      .mockResolvedValueOnce({ updated: true })
      .mockResolvedValueOnce({ libraries: [], current_fingerprint: "abc" })
    const store = useLibrariesStore()
    await store.submitDialog(ROW, {
      name: "外科学（第11版）", subject: "外科学", root_path: "C:/fixtures/textbooks/book.pdf",
      version: "第11版", pdf_anchor: 41, textbook_anchor: 1,
    })
    expect(invokeMock).toHaveBeenNthCalledWith(1, "library", "update",
      expect.objectContaining({ library_id: 3, name: "外科学（第11版）", page_offset: 40 }))
  })

  it("startScan 未接受云端说明时先确认", async () => {
    invokeMock.mockResolvedValueOnce({ accepted: false })
    const store = useLibrariesStore()
    const outcome = await store.startScan(ROW)
    expect(outcome).toBe("needs_confirm")
    expect(invokeMock).toHaveBeenCalledTimes(1)
  })

  it("confirmCloudNotice 接受后按 force_ocr 入队 scan", async () => {
    invokeMock
      .mockResolvedValueOnce({ accepted: true }) // accept_cloud_notice
      .mockResolvedValueOnce({ job_id: "j1" }) // jobs.enqueue
    const store = useLibrariesStore()
    store.forceOcr = true
    await store.confirmCloudNotice(ROW)
    expect(invokeMock).toHaveBeenNthCalledWith(2, "jobs", "enqueue", {
      job_type: "scan", title: "索引：外科学（第10版）",
      payload: { library_id: 3, force_ocr: true },
    })
  })

  it("startScan 已接受时直接入队", async () => {
    invokeMock
      .mockResolvedValueOnce({ accepted: true })
      .mockResolvedValueOnce({ job_id: "j1" })
    const store = useLibrariesStore()
    const outcome = await store.startScan(ROW)
    expect(outcome).toBe("enqueued")
    expect(invokeMock).toHaveBeenNthCalledWith(2, "jobs", "enqueue", expect.objectContaining({
      payload: { library_id: 3, force_ocr: false },
    }))
  })

  it("inferOffset 返回 offset 或 null", async () => {
    invokeMock
      .mockResolvedValueOnce({ offset: 39 })
      .mockResolvedValueOnce({ libraries: [], current_fingerprint: "abc" })
    const store = useLibrariesStore()
    const offset = await store.inferOffset(ROW)
    expect(offset).toBe(39)
    expect(invokeMock).toHaveBeenNthCalledWith(1, "library", "infer_page_offset", { library_id: 3 })
  })

  it("remove 软删除并重载", async () => {
    invokeMock
      .mockResolvedValueOnce({ removed: true })
      .mockResolvedValueOnce({ libraries: [], current_fingerprint: "abc" })
    const store = useLibrariesStore()
    await store.remove(ROW)
    expect(invokeMock).toHaveBeenNthCalledWith(1, "library", "remove", { library_id: 3 })
  })
})
