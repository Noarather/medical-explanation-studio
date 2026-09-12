export interface EditorBlock {
  blockId: string
  section: string
  type: string
  title: string
  text?: string
  items?: string[]
  columns?: string[]
  rows?: string[][]
  tone?: string
}

export const SECTION_OPTIONS = [
  { value: "analysis", label: "考点解析" },
  { value: "answerBasis", label: "正确答案依据" },
  { value: "pitfalls", label: "易错点提示" },
  { value: "clinicalNotes", label: "临床与操作要点" },
]
export const TYPE_OPTIONS = [
  { value: "paragraph", label: "段落" },
  { value: "list", label: "列表" },
  { value: "table", label: "表格" },
  { value: "callout", label: "提示卡" },
]
export const TONE_OPTIONS = [
  { value: "info", label: "提示" },
  { value: "important", label: "重要" },
  { value: "warning", label: "警告" },
]

export const MAX_BLOCKS = 12
export const MAX_TABLE_COLUMNS = 5
export const MAX_TABLE_ROWS = 20
export const MAX_LIST_ITEMS = 20

function newBlockId(index: number): string {
  return `b${String(index + 1).padStart(2, "0")}-${Math.random().toString(36).slice(2, 8)}`
}

export function createBlock(type: string, index: number): EditorBlock {
  const base = { blockId: newBlockId(index), section: "analysis", type, title: "" }
  if (type === "list") return { ...base, items: [""] }
  if (type === "table") return { ...base, columns: ["项目", "说明"], rows: [["", ""]] }
  if (type === "callout") return { ...base, text: "", tone: "important" }
  return { ...base, text: "" }
}

export function convertBlockType(block: EditorBlock, type: string): EditorBlock {
  if (type === block.type) return block
  const base = { blockId: block.blockId, section: block.section, title: block.title, type }

  if (type === "paragraph" || type === "callout") {
    let text = block.text
    if (text === undefined || text === null) {
      if (block.items && block.items.length > 0) {
        text = block.items.join("\n")
      } else if (block.rows && block.rows.length > 0) {
        text = block.rows.map((row) => row.join("、")).join("\n")
      } else {
        text = ""
      }
    }
    if (type === "callout") return { ...base, text, tone: block.tone ?? "important" }
    return { ...base, text }
  }

  if (type === "list") {
    let items: string[]
    if (block.items && block.items.length > 0) {
      items = block.items
    } else if (block.text !== undefined && block.text !== null) {
      items = block.text.split("\n").map((line) => line.trim()).filter((line) => line !== "")
      if (items.length === 0) items = [""]
    } else {
      items = [""]
    }
    return { ...base, items }
  }

  if (type === "table") {
    if (block.columns && block.columns.length >= 2 && block.rows && block.rows.length > 0) {
      return { ...base, columns: block.columns, rows: block.rows }
    }
    const columns = ["项目", "说明"]
    let rows: string[][]
    if (block.text !== undefined && block.text !== null) {
      const lines = block.text.split("\n").map((line) => line.trim()).filter((line) => line !== "")
      rows = lines.map((line) => {
        const cells = line.split(/[|\t]/)
        while (cells.length < 2) cells.push("")
        return cells
      })
      if (rows.length === 0) rows = [["", ""]]
    } else {
      rows = [["", ""]]
    }
    return { ...base, columns, rows }
  }

  return { ...base, text: block.text ?? "" }
}

export function addBlock(blocks: EditorBlock[], type: string): EditorBlock[] {
  if (blocks.length >= MAX_BLOCKS) throw new Error(`每题最多 ${MAX_BLOCKS} 个区块`)
  return [...blocks, createBlock(type, blocks.length)]
}

export function removeBlock(blocks: EditorBlock[], index: number): EditorBlock[] {
  if (blocks.length <= 1) throw new Error("至少保留一个区块")
  return blocks.filter((_, item) => item !== index)
}

export function moveBlock(blocks: EditorBlock[], index: number, delta: number): EditorBlock[] {
  const target = index + delta
  if (target < 0 || target >= blocks.length) return blocks
  const next = [...blocks]
  const [moved] = next.splice(index, 1)
  next.splice(target, 0, moved)
  return next
}

export function addListItem(block: EditorBlock): EditorBlock {
  const items = [...(block.items ?? [])]
  if (items.length >= MAX_LIST_ITEMS) throw new Error(`每块最多 ${MAX_LIST_ITEMS} 项`)
  return { ...block, items: [...items, ""] }
}

export function removeListItem(block: EditorBlock, index: number): EditorBlock {
  const items = block.items ?? []
  if (items.length <= 1) throw new Error("列表至少保留一项")
  return { ...block, items: items.filter((_, item) => item !== index) }
}

export function addTableRow(block: EditorBlock): EditorBlock {
  const rows = block.rows ?? []
  if (rows.length >= MAX_TABLE_ROWS) throw new Error(`每块最多 ${MAX_TABLE_ROWS} 行`)
  const width = (block.columns ?? []).length
  return { ...block, rows: [...rows, Array.from({ length: width }, () => "")] }
}

export function removeTableRow(block: EditorBlock): EditorBlock {
  const rows = block.rows ?? []
  if (rows.length <= 1) throw new Error("表格至少保留一行")
  return { ...block, rows: rows.slice(0, -1) }
}

export function addTableColumn(block: EditorBlock): EditorBlock {
  const columns = block.columns ?? []
  if (columns.length >= MAX_TABLE_COLUMNS) throw new Error(`每块最多 ${MAX_TABLE_COLUMNS} 列`)
  return {
    ...block,
    columns: [...columns, `列${columns.length + 1}`],
    rows: (block.rows ?? []).map((row) => [...row, ""]),
  }
}

export function removeTableColumn(block: EditorBlock): EditorBlock {
  const columns = block.columns ?? []
  if (columns.length <= 2) throw new Error("表格至少保留两列")
  return {
    ...block,
    columns: columns.slice(0, -1),
    rows: (block.rows ?? []).map((row) => row.slice(0, -1)),
  }
}
