import { describe, expect, it } from "vitest"
import {
  addBlock, addListItem, addTableColumn, addTableRow, convertBlockType, createBlock, moveBlock,
  removeBlock, removeListItem, removeTableColumn, removeTableRow,
} from "../blocks"

describe("blocks 编辑操作", () => {
  it("createBlock 按类型给默认结构", () => {
    expect(createBlock("paragraph", 0)).toMatchObject({ type: "paragraph", section: "analysis", text: "" })
    expect(createBlock("list", 0)).toMatchObject({ type: "list", items: [""] })
    expect(createBlock("table", 0)).toMatchObject({ type: "table", columns: ["项目", "说明"], rows: [["", ""]] })
    expect(createBlock("callout", 0)).toMatchObject({ type: "callout", tone: "important", text: "" })
  })

  it("addBlock 上限 12", () => {
    let blocks = [createBlock("paragraph", 0)]
    for (let i = 0; i < 11; i++) blocks = addBlock(blocks, "paragraph")
    expect(blocks).toHaveLength(12)
    expect(() => addBlock(blocks, "paragraph")).toThrow("12")
  })

  it("removeBlock 保底一块", () => {
    const blocks = [createBlock("paragraph", 0)]
    expect(() => removeBlock(blocks, 0)).toThrow()
    const two = addBlock(blocks, "list")
    expect(removeBlock(two, 0)).toHaveLength(1)
  })

  it("moveBlock 上下移动且越界不动", () => {
    let blocks = [createBlock("paragraph", 0), createBlock("list", 1)]
    blocks[0].title = "甲"; blocks[1].title = "乙"
    blocks = moveBlock(blocks, 1, -1)
    expect(blocks[0].title).toBe("乙")
    expect(moveBlock(blocks, 0, -1)[0].title).toBe("乙")
  })

  it("表格行列增减与边界", () => {
    let block = createBlock("table", 0)
    block = addTableRow(block)
    expect(block.rows).toHaveLength(2)
    block = addTableColumn(block)
    expect(block.columns).toHaveLength(3)
    expect(block.rows?.[0]).toHaveLength(3)
    block = addTableColumn(block); block = addTableColumn(block)
    expect(() => addTableColumn(block)).toThrow() // 已到 5 列
    block = removeTableColumn(block)
    expect(block.columns).toHaveLength(4)
    block = removeTableColumn(block); block = removeTableColumn(block)
    expect(() => removeTableColumn(block)).toThrow() // 保底 2 列
    block = removeTableRow(block)
    expect(() => removeTableRow(block)).toThrow() // 保底 1 行
  })

  it("列表项增减与边界", () => {
    let block = createBlock("list", 0)
    block = addListItem(block)
    expect(block.items).toHaveLength(2)
    block = removeListItem(block, 1)
    expect(block.items).toHaveLength(1)
    expect(() => removeListItem(block, 0)).toThrow() // 保底 1 项
  })
})

describe("convertBlockType 类型切换重建结构字段", () => {
  it("paragraph(两行文本) → list：items 为两行，保留 blockId/section/title", () => {
    const block = { ...createBlock("paragraph", 0), text: "第一行\n第二行", title: "标题" }
    const next = convertBlockType(block, "list")
    expect(next).toMatchObject({
      blockId: block.blockId, section: block.section, title: "标题", type: "list",
      items: ["第一行", "第二行"],
    })
    expect(next.text).toBeUndefined()
    expect(next.rows).toBeUndefined()
  })

  it("list(两项) → paragraph：text 为两项换行连接", () => {
    const block = { ...createBlock("list", 0), items: ["甲", "乙"] }
    const next = convertBlockType(block, "paragraph")
    expect(next.text).toBe("甲\n乙")
    expect(next.items).toBeUndefined()
  })

  it("paragraph → table：默认两列，text 行按 | 拆分；无 text 时给空行", () => {
    const block = { ...createBlock("paragraph", 0), text: "名称|说明文字\n剂量\t一日三次" }
    const next = convertBlockType(block, "table")
    expect(next.columns).toEqual(["项目", "说明"])
    expect(next.rows).toEqual([["名称", "说明文字"], ["剂量", "一日三次"]])
    expect(next.text).toBeUndefined()

    const empty = convertBlockType({ ...createBlock("paragraph", 0), text: "" }, "table")
    expect(empty.rows).toEqual([["", ""]])
  })

  it("同类型原样返回；table → paragraph 时 rows 文本化", () => {
    const block = { ...createBlock("table", 0), rows: [["A", "B"], ["C", "D"]] }
    expect(convertBlockType(block, "table")).toBe(block)
    const next = convertBlockType(block, "paragraph")
    expect(next.text).toBe("A、B\nC、D")
    expect(next.columns).toBeUndefined()
    expect(next.rows).toBeUndefined()
  })

  it("→ callout 保留原 tone，无 tone 时默认 important", () => {
    const warned = convertBlockType({ ...createBlock("callout", 0), text: "注意", tone: "warning" }, "paragraph")
    expect(warned.tone).toBeUndefined()
    const back = convertBlockType({ ...createBlock("callout", 0), text: "注意", tone: "warning" }, "callout")
    expect(back).toMatchObject({ type: "callout", tone: "warning" })
    const fromList = convertBlockType({ ...createBlock("list", 0), items: ["甲"] }, "callout")
    expect(fromList).toMatchObject({ type: "callout", text: "甲", tone: "important" })
  })
})
