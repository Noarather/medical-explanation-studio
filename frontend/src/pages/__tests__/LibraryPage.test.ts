import { beforeEach, expect, it, vi } from "vitest"
import { mount, flushPromises } from "@vue/test-utils"
import { createPinia, setActivePinia } from "pinia"
import LibraryPage from "../LibraryPage.vue"
import { useJobsStore } from "../../stores/jobs"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock, onJobsChanged: vi.fn(async () => {}) }))

const emptyRow = { id: 12, name: "合成教材", subject: "测试", version: "", root_path: "sample.pdf",
  page_offset: 0, file_count: 0, page_count: 0, file_status: null, file_error: null, index_state: "none" }
let row: any
let pinia: ReturnType<typeof createPinia>
const job = (status: string) => ({ id: "scan-1", job_type: "scan", title: "索引", status,
  progress_current: 0, progress_total: 10, message: "合成进度", payload_json: '{"library_id":12}' })
const libraryCalls = () => invokeMock.mock.calls.filter(([domain]) => domain === "library").length
function mountPage() {
  return mount(LibraryPage, { global: { plugins: [pinia],
    stubs: { LibraryDialog: true, TextbookImportDialog: true } } })
}
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia)
  row = { ...emptyRow }
  invokeMock.mockReset()
  invokeMock.mockImplementation((domain) => Promise.resolve(domain === "library"
    ? { libraries: [{ ...row }], current_fingerprint: "test" } : { jobs: [] }))
})

it("索引完成后自动刷新卡片，不再停留在未建立", async () => {
  const page = mountPage(); await flushPromises()
  expect(page.text()).toContain("尚未建立索引")
  const jobs = useJobsStore()
  jobs.jobs = [job("running")]; await flushPromises()
  expect(page.text()).toContain("建立索引中")
  expect(page.text()).not.toContain("尚未建立索引")
  row = { ...row, file_count: 1, page_count: 398, file_status: "ready", index_state: "compatible", file_name: "sample.pdf" }
  jobs.jobs = [job("completed")]; await flushPromises()
  expect(page.text()).toContain("索引可用")
  expect(page.text()).toContain("398 页")
  expect(page.text()).not.toContain("尚未建立索引")
  page.unmount()
})

it.each(["failed", "cancelled", "paused"])("任务 %s 也刷新，进度 tick 不重复加载，离开页面停止监听", async status => {
  const page = mountPage(); await flushPromises()
  const jobs = useJobsStore()
  jobs.jobs = [job("running")]; await flushPromises()
  const count = libraryCalls()
  jobs.jobs = [{ ...job("running"), progress_current: 7 }]; await flushPromises()
  expect(libraryCalls()).toBe(count)
  jobs.jobs = [job(status)]; await flushPromises()
  expect(libraryCalls()).toBe(count + 1)
  page.unmount()
  jobs.jobs = [job("completed")]; await flushPromises()
  expect(libraryCalls()).toBe(count + 1)
})

it("刷新按钮读取最新状态，降级结果不显示为完全成功", async () => {
  const page = mountPage(); await flushPromises()
  row = { ...row, file_count: 1, page_count: 498, file_status: "warning", index_state: "partial",
    file_error: "synthetic error", file_error_summary: "主解析器失败，已尝试备用解析" }
  await page.findAll("button").find(b => b.text() === "刷新状态")!.trigger("click")
  await flushPromises()
  expect(page.text()).toContain("已建立（有解析降级／异常）")
  expect(page.text()).toContain("主解析器失败，已尝试备用解析")
  page.unmount()
})
