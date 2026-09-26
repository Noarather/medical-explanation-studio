import { beforeEach, expect, it, vi } from "vitest"
import { mount, flushPromises } from "@vue/test-utils"
import { createPinia, setActivePinia } from "pinia"
import QuestionsPage from "../QuestionsPage.vue"
import { useQuestionsStore } from "../../stores/questions"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock }))
let pinia: ReturnType<typeof createPinia>
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia)
  invokeMock.mockReset()
  invokeMock.mockImplementation((_domain, method) => Promise.resolve(
    method === "filters" ? { sets: [{ id: "a", name: "合成批次", question_count: 600 }], subjects: [], tags: [], sources: [] }
    : method === "first_generation_preview" ? { set_id: "a", name: "合成批次", total: 600, count: 600, skipped: 0, previous: 50, reviewed: 2, active: false, token: "snapshot" }
    : method === "first_generation_start" ? { count: 600 }
    : { rows: [], total: 0 }))
})
const mountPage = () => mount(QuestionsPage, { global: { plugins: [pinia], stubs: { QuestionDetailDrawer: true, LibraryPickerModal: true } } })

it("状态下拉显示与 store 中持久化的筛选保持一致", async () => {
  const store = useQuestionsStore()
  store.filters.review_status = "pending"
  store.filters.pipeline_status = "generated"
  const page = mountPage(); await flushPromises()
  expect((page.findAll("select")[1].element as HTMLSelectElement).value).toBe("已生成待审核")
})

it("整批入口要求选择批次和费用确认，不沿用分页、搜索或勾选范围", async () => {
  const page = mountPage(); await flushPromises()
  expect(page.get('[data-testid="first-generation"]').attributes("disabled")).toBeDefined()
  const store = useQuestionsStore()
  store.filters.set_id = "a"; store.filters.search = "局部"; store.selected = [7]; store.page = 2
  await flushPromises()
  await page.get('[data-testid="first-generation"]').trigger("click"); await flushPromises()
  expect(invokeMock).toHaveBeenCalledWith("questions", "first_generation_preview", { set_id: "a" })
  expect(page.text()).toContain("本次将重新生成 600 题，跳过 0 题")
  expect(page.text()).toContain("其中 50 题已有解析、2 题已审核")
  expect(page.get('[data-testid="generation-choose"]').attributes("disabled")).toBeDefined()
  await page.get('[data-testid="generation-consent"]').setValue(true)
  await page.get('[data-testid="generation-choose"]').trigger("click")
  // Changing the visible filters cannot silently change the confirmed target batch.
  store.filters.set_id = "b"
  page.findComponent({ name: "LibraryPickerModal" }).vm.$emit("confirm", [3])
  await flushPromises()
  expect(invokeMock).toHaveBeenCalledWith("questions", "first_generation_start", { set_id: "a", token: "snapshot", library_ids: [3], consent: true })
  expect(page.text()).not.toContain("本次将重新生成 600")
})

it("已有活动任务时禁止整批首次生成", async () => {
  const page = mountPage(); await flushPromises()
  useQuestionsStore().filters.set_id = "a"; await flushPromises()
  invokeMock.mockResolvedValueOnce({ set_id: "a", name: "合成批次", total: 600, count: 600, skipped: 0, previous: 50, reviewed: 2, active: true, token: "snapshot" })
  await page.get('[data-testid="first-generation"]').trigger("click"); await flushPromises()
  await page.get('[data-testid="generation-consent"]').setValue(true)
  expect(page.get('[data-testid="generation-choose"]').attributes("disabled")).toBeDefined()
  expect(page.text()).toContain("本批次已有生成任务")
})
