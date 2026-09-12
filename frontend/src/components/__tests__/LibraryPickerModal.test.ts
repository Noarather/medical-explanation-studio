import { beforeEach, describe, expect, it, vi } from "vitest"
import { flushPromises, mount } from "@vue/test-utils"

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }))
vi.mock("../../lib/bridge", () => ({ invoke: invokeMock }))

import LibraryPickerModal from "../review/LibraryPickerModal.vue"

function mountModal(toast: (message: string, tone?: string) => void) {
  return mount(LibraryPickerModal, {
    props: { open: true },
    global: { provide: { toast } },
  })
}

describe("LibraryPickerModal confirm", () => {
  beforeEach(() => {
    invokeMock.mockReset()
  })

  it("保存成功：emit confirm 并关闭 modal", async () => {
    const toast = vi.fn()
    invokeMock.mockResolvedValueOnce({ saved: true })
    const wrapper = mountModal(toast)
    await wrapper.findAll("button").find((b) => b.text() === "确定")!.trigger("click")
    await flushPromises()
    expect(invokeMock).toHaveBeenCalledWith("review", "save_generation_libraries", { library_ids: [] })
    expect(wrapper.emitted("confirm")).toBeTruthy()
    expect(wrapper.emitted("update:open")).toEqual([[false]])
    expect(toast).not.toHaveBeenCalled()
  })

  it("保存失败：toast 报错且 modal 保持打开", async () => {
    const toast = vi.fn()
    invokeMock.mockRejectedValueOnce({ message: "写入设置失败" })
    const wrapper = mountModal(toast)
    await wrapper.findAll("button").find((b) => b.text() === "确定")!.trigger("click")
    await flushPromises()
    expect(toast).toHaveBeenCalledWith("写入设置失败", "destructive")
    expect(wrapper.emitted("confirm")).toBeFalsy()
    expect(wrapper.emitted("update:open")).toBeFalsy()
  })
})
