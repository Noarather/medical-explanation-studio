import { beforeEach, describe, expect, it, vi } from "vitest"
import { setActivePinia, createPinia } from "pinia"

vi.mock("../../lib/bridge", () => ({
  invoke: vi.fn(async (_d: string, method: string) => {
    if (method === "list") return { jobs: [{ id: "j1", status: "running", progress_current: 3, progress_total: 10, title: "生成" }] }
    return {}
  }),
  onJobsChanged: vi.fn(async () => {}),
}))

describe("jobs store", () => {
  beforeEach(() => setActivePinia(createPinia()))

  it("computes progress percent from running job", async () => {
    const { useJobsStore } = await import("../jobs")
    const store = useJobsStore()
    await store.init()
    expect(store.jobs).toHaveLength(1)
    expect(store.progressPercent).toBe(30)
  })

  it("returns 0 progress before init", async () => {
    const { useJobsStore } = await import("../jobs")
    const store = useJobsStore()
    expect(store.progressPercent).toBe(0)
  })
})
