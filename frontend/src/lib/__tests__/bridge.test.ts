import { beforeEach, describe, expect, it, vi } from "vitest"

const objects: Record<string, any> = {}

function setupChannel() {
  ;(window as any).qt = { webChannelTransport: {} }
  ;(window as any).QWebChannel = class {
    constructor(_t: unknown, cb: (channel: any) => void) { cb({ objects }) }
  }
}

describe("bridge invoke", () => {
  beforeEach(async () => {
    vi.resetModules()
    setupChannel()
  })

  it("resolves data on ok envelope", async () => {
    objects.demo = { invoke: (_m: string, _p: string, cb: (raw: string) => void) => cb(JSON.stringify({ ok: true, data: { n: 1 } })) }
    const { invoke } = await import("../bridge")
    await expect(invoke<{ n: number }>("demo", "ping")).resolves.toEqual({ n: 1 })
  })

  it("rejects with error envelope", async () => {
    objects.demo = { invoke: (_m: string, _p: string, cb: (raw: string) => void) => cb(JSON.stringify({ ok: false, error: { code: "boom", message: "失败" } })) }
    const { invoke } = await import("../bridge")
    await expect(invoke("demo", "ping")).rejects.toEqual({ code: "boom", message: "失败" })
  })

  it("rejects unknown domain", async () => {
    const { invoke } = await import("../bridge")
    await expect(invoke("ghost", "ping")).rejects.toMatchObject({ code: "no_domain" })
  })
})
