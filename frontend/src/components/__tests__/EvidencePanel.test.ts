import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { mount } from "@vue/test-utils"

vi.mock("../../lib/bridge", () => ({ invoke: vi.fn() }))
// jsdom 无法真实渲染 PDF；桩掉 PdfViewer 避免加载 pdfjs-dist
vi.mock("../review/PdfViewer.vue", () => ({ default: { template: "<div data-test=pdf-viewer />" } }))

import EvidencePanel from "../review/EvidencePanel.vue"
import { useReviewStore } from "../../stores/review"

const EVIDENCE = [
  { textbook: "外科学", textbook_version: "第10版", source_file: "book.pdf",
    source_page: 508, pdf_page: 547, score: 0.82, text: "教材原文摘录……",
    source_path: "E:/教材/book.pdf" },
  { textbook: "病理学", source_file: "patho.pdf", source_page: null, pdf_page: 12,
    score: 0.61, text: "另一条摘录", source_path: "E:/教材/patho.pdf" },
]

describe("EvidencePanel", () => {
  beforeEach(() => { setActivePinia(createPinia()) })

  it("渲染证据列表并默认选中第一条", async () => {
    const store = useReviewStore()
    store.detail = { evidence: EVIDENCE }
    const wrapper = mount(EvidencePanel)
    expect(wrapper.text()).toContain("外科学")
    expect(wrapper.text()).toContain("第10版")
    expect(wrapper.text()).toContain("课本第 508 页")
    expect(wrapper.text()).toContain("0.820")
    expect(store.selectedEvidenceIndex).toBe(0)
  })

  it("印刷页缺失时显示课本前置页", () => {
    const store = useReviewStore()
    store.detail = { evidence: EVIDENCE }
    const wrapper = mount(EvidencePanel)
    expect(wrapper.text()).toContain("课本前置页")
  })

  it("点击证据更新选中索引", async () => {
    const store = useReviewStore()
    store.detail = { evidence: EVIDENCE }
    const wrapper = mount(EvidencePanel)
    await wrapper.findAll("[data-test=evidence-item]")[1].trigger("click")
    expect(store.selectedEvidenceIndex).toBe(1)
  })

  it("无证据时显示空态", () => {
    const store = useReviewStore()
    store.detail = { evidence: [] }
    const wrapper = mount(EvidencePanel)
    expect(wrapper.text()).toContain("没有教材证据")
  })
})
