import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock }))

import { useReviewStore } from "../review"

const LIST_ROW = {
  id: 7, external_id: "r1", subject: "外科学", pipeline_status: "generated",
  review_status: "pending", prompt_text: "题干", question_type: "A1",
  match_score: null, evidence_grade: "", updated_at: "2026-01-01T00:00:00",
}
const DETAIL = {
  id: 7, external_id: "r1", set_id: "s1", subject: "外科学", explanation: "旧解析",
  raw: { question: "题干", options: ["A. 甲", "B. 乙"], answer: "A",
         explanationBlocks: [{ blockId: "b01-x", section: "analysis", type: "paragraph", title: "考点解析", text: "旧" }],
         tags: ["休克"], knowledgePoints: ["休克"], suggestedTags: [], mnemonic: "", briefExplanation: "简析" },
  evidence: [],
}

describe("review store", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    invokeMock.mockReset()
  })

  it("open 载入详情并初始化编辑状态", async () => {
    invokeMock.mockResolvedValueOnce(DETAIL)
    const store = useReviewStore()
    await store.open(7)
    expect(invokeMock).toHaveBeenCalledWith("review", "open", { question_pk: 7 })
    expect(store.currentId).toBe(7)
    expect(store.blocks).toHaveLength(1)
    expect(store.fields.tags).toEqual(["休克"])
    expect(store.fields.briefExplanation).toBe("简析")
    expect(store.dirty).toBe(false)
  })

  it("save 提交编辑状态并清除脏标记", async () => {
    invokeMock.mockResolvedValueOnce(DETAIL)
    const store = useReviewStore()
    await store.open(7)
    store.blocks[0].text = "新内容"
    store.dirty = true
    invokeMock
      .mockResolvedValueOnce({ saved: true, question: { explanation: "新内容" } }) // save
      .mockResolvedValueOnce({ rows: [LIST_ROW], total: 1 }) // save 内部 load
    await store.save()
    expect(invokeMock).toHaveBeenNthCalledWith(2, "review", "save", expect.objectContaining({
      question_pk: 7, tags: ["休克"], brief_explanation: "简析",
    }))
    expect(store.dirty).toBe(false)
    expect(store.detail?.explanation).toBe("新内容") // 就地同步供 review 使用
  })

  it("批准前自动保存脏编辑，然后自动前进", async () => {
    invokeMock.mockResolvedValueOnce(DETAIL)
    const store = useReviewStore()
    store.rows = [LIST_ROW, { ...LIST_ROW, id: 8, external_id: "r2" }]
    await store.open(7)
    store.dirty = true
    invokeMock
      .mockResolvedValueOnce({ saved: true, question: { explanation: "新内容" } }) // save
      .mockResolvedValueOnce({ rows: [LIST_ROW, { ...LIST_ROW, id: 8, external_id: "r2" }], total: 2 }) // save 内部 load
      .mockResolvedValueOnce({ reviewed: "approved" }) // review
      .mockResolvedValueOnce({ rows: [{ ...LIST_ROW, id: 8, external_id: "r2" }], total: 1 }) // advance 后 load
      .mockResolvedValueOnce({ ...DETAIL, id: 8, external_id: "r2" }) // open next
    await store.review("approved")
    expect(invokeMock).toHaveBeenNthCalledWith(4, "review", "review",
      expect.objectContaining({ question_pk: 7, action: "approved", explanation: "新内容" }))
    expect(store.currentId).toBe(8)
  })

  it("重新生成入队并刷新列表", async () => {
    invokeMock.mockResolvedValueOnce(DETAIL)
    const store = useReviewStore()
    await store.open(7)
    invokeMock
      .mockResolvedValueOnce({ job_id: "j1" })
      .mockResolvedValueOnce({ rows: [], total: 0 })
    await store.regenerate()
    expect(invokeMock).toHaveBeenNthCalledWith(2, "review", "regenerate", { question_pk: 7 })
    expect(store.currentId).toBe(null)
  })

  it("applyFilters 切换筛选时清空批量选择", async () => {
    invokeMock.mockResolvedValueOnce({ rows: [], total: 0 })
    const store = useReviewStore()
    store.selected = [7, 8]
    store.page = 2
    await store.applyFilters()
    expect(store.selected).toEqual([])
    expect(store.page).toBe(0)
    expect(invokeMock).toHaveBeenCalledWith("review", "list", expect.anything())
  })

  it("多选与批量学科", async () => {
    const store = useReviewStore()
    store.rows = [LIST_ROW, { ...LIST_ROW, id: 8 }]
    store.toggleSelected(7); store.toggleSelected(8)
    expect(store.selected).toEqual([7, 8])
    store.toggleSelected(7)
    expect(store.selected).toEqual([8])
    invokeMock
      .mockResolvedValueOnce({ updated: 1 }) // questions.bulk_set_subject
      .mockResolvedValueOnce({ rows: [], total: 0 }) // load
    await store.batchSetSubject("内科学")
    expect(invokeMock).toHaveBeenNthCalledWith(1, "questions", "bulk_set_subject",
      { question_pks: [8], subject: "内科学" })
    expect(store.selected).toEqual([])
  })

  it("批量重生带所选教材入队并清空选择", async () => {
    invokeMock
      .mockResolvedValueOnce({ job_id: "j9", count: 2 })
      .mockResolvedValueOnce({ rows: [], total: 0 })
    const store = useReviewStore()
    store.setId = "s1"
    store.selected = [7, 8]
    await store.batchRegenerate([3, 7])
    expect(invokeMock).toHaveBeenNthCalledWith(1, "review", "regenerate_batch", {
      set_id: "s1", question_ids: [7, 8], library_ids: [3, 7],
    })
    expect(store.selected).toEqual([])
  })

  it("批量通识先预筛数量再入队", async () => {
    invokeMock
      .mockResolvedValueOnce({ count: 5 }) // general_batch_preview
      .mockResolvedValueOnce({ job_id: "j10" }) // jobs.enqueue
    const store = useReviewStore()
    store.setId = "s1"
    store.selected = [7, 8]
    const count = await store.batchGeneral()
    expect(count).toBe(5)
    expect(invokeMock).toHaveBeenNthCalledWith(2, "jobs", "enqueue", {
      job_type: "general_batch", title: "批量通识生成：5 题",
      payload: { set_id: "s1", question_ids: [7, 8] },
    })
  })

  it("批量通识预筛为 0 时不入队", async () => {
    invokeMock.mockResolvedValueOnce({ count: 0 })
    const store = useReviewStore()
    store.setId = "s1"
    store.selected = [7]
    const count = await store.batchGeneral()
    expect(count).toBe(0)
    expect(invokeMock).toHaveBeenCalledTimes(1)
  })

  it("快速审核批准后刷新列表并清空当前题", async () => {
    invokeMock.mockResolvedValueOnce(DETAIL)
    const store = useReviewStore()
    store.setId = "s1"
    await store.open(7)
    invokeMock
      .mockResolvedValueOnce({ approved: 12, skipped: 1 }) // quick_review_approve
      .mockResolvedValueOnce({ rows: [], total: 0 }) // load
    const result = await store.quickApprove(0.8, [7])
    expect(invokeMock).toHaveBeenNthCalledWith(2, "review", "quick_review_approve", {
      set_id: "s1", minimum_score: 0.8, expected_ids: [7],
    })
    expect(result).toEqual({ approved: 12, skipped: 1 })
    expect(store.currentId).toBe(null)
  })

  it("单题通识授权入队 general 任务", async () => {
    invokeMock
      .mockResolvedValueOnce({ ...DETAIL, pipeline_status: "unmatched" }) // open
      .mockResolvedValueOnce({ job_id: "j11" }) // general_authorize
      .mockResolvedValueOnce({ rows: [], total: 0 }) // load
    const store = useReviewStore()
    await store.open(7)
    await store.authorizeGeneral()
    expect(invokeMock).toHaveBeenNthCalledWith(2, "review", "general_authorize", { question_pk: 7 })
    expect(store.currentId).toBe(null)
  })

  it("补齐估算与入队", async () => {
    invokeMock
      .mockResolvedValueOnce({ total: 40, repairable: 40, requires_regeneration: 0, approved: 3, estimated_requests: 2, kind: "tags" })
      .mockResolvedValueOnce({ job_id: "j12" })
    const store = useReviewStore()
    store.setId = "s1"
    const estimate = await store.backfillEstimate("tags")
    expect(estimate.estimated_requests).toBe(2)
    await store.enqueueBackfill("tags", "补齐题目标签：40 题")
    expect(invokeMock).toHaveBeenNthCalledWith(2, "jobs", "enqueue", {
      job_type: "tag_backfill", title: "补齐题目标签：40 题",
      payload: { set_id: "s1", include_approved: true },
    })
  })
})
