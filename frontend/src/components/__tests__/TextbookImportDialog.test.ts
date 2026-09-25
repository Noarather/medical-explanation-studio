import {beforeEach, afterEach, expect, it, vi} from "vitest"
import {flushPromises, mount} from "@vue/test-utils"
const {invokeMock} = vi.hoisted(() => ({invokeMock: vi.fn()}))
vi.mock("../../lib/bridge", () => ({invoke: invokeMock}))
import TextbookImportDialog from "../TextbookImportDialog.vue"

const row = () => ({index:0, name:"病理生理学（第10版）", subject:"病理生理学", version:"第10版",
  file_name:"sample.pdf", root_path:"C:/sample.pdf", page_count:291, warnings:[], error:"",
  metadata_source:"文件名 + 书内文字交叉检查", calibration:{status:"recognized",offset:24,
    mapped_pages:263,unknown_pages:28,message:"已识别",segments:[{pdf_start:25,pdf_end:287,
      printed_start:1,printed_end:263,support:262,confidence:"high"}]}})
function setup(rows:any[] = [row()]) {
  invokeMock.mockImplementation((_d, method, args) => Promise.resolve({
    pick_pdfs:{paths:rows.map(r=>r.root_path)}, inspect_start:{token:"draft"}, commit_batch:{token:"commit"},
    inspect_status:args?.token === "commit" ? {status:"completed",result:{imported:1,results:[{index:0,status:"imported"}]}}
      : {status:"completed",result:{items:rows}},
  }[method as string] ?? {}))
}
const button = (w:any, text:string) => w.findAll("button").find((b:any)=>b.text().includes(text))!
const mountDialog = (props:any) => mount(TextbookImportDialog,{props,global:{stubs:{teleport:true}}})
beforeEach(()=>invokeMock.mockReset())
afterEach(()=>vi.useRealTimers())

it("显示分段对应和识别值，修改后只提交有效教材，不自动建索引", async()=>{
  setup([row(), {...row(),index:1,error:"损坏 PDF",file_name:"broken.pdf"}])
  const w=mountDialog({open:true})
  await button(w,"选择 PDF").trigger("click"); await flushPromises()
  expect(w.text()).toContain("PDF 25–287 页 → 课本 1–263 页")
  expect(w.text()).toContain("损坏 PDF")
  await w.find('[aria-label="教材1名称"]').setValue("自定义书名")
  await button(w,"确认导入 1 本").trigger("click"); await flushPromises()
  expect(invokeMock).toHaveBeenCalledWith("library","commit_batch",expect.objectContaining({
    token:"draft",items:[expect.objectContaining({index:0,name:"自定义书名",subject:"病理生理学",version:"第10版"})]}))
  expect(w.text()).toContain("成功导入 1 本")
  expect(w.emitted("saved")).toHaveLength(1)
  expect(invokeMock.mock.calls.every(([domain])=>domain==="library")).toBe(true)
  w.unmount()
})

it("未识别学科时需补填，仍可手工指定页码",async()=>{
  setup([{...row(),subject:"",calibration:{status:"unresolved",offset:null,segments:[],mapped_pages:0,unknown_pages:291}}])
  const w=mountDialog({open:true})
  await button(w,"选择 PDF").trigger("click"); await flushPromises()
  expect(button(w,"确认导入").attributes("disabled")).toBeDefined()
  await w.find('[aria-label="教材1学科"]').setValue("病理生理学")
  await w.find("select").setValue("manual")
  await w.find('[aria-label="PDF 对应页"]').setValue(25)
  await w.find('[aria-label="课本对应页"]').setValue(1)
  await button(w,"确认导入").trigger("click"); await flushPromises()
  expect(invokeMock).toHaveBeenCalledWith("library","commit_batch",expect.objectContaining({items:[expect.objectContaining({mode:"manual",pdf_anchor:25,textbook_anchor:1})]}))
  w.unmount()
})

it("已有教材只校准页码，无法识别时禁止覆盖原结果",async()=>{
  setup([{...row(),subject:"",calibration:{status:"unresolved",segments:[],mapped_pages:0,unknown_pages:291}}])
  const editing={id:1,name:"已保存名称",subject:"病理生理学",version:"第10版",root_path:"C:/sample.pdf"} as any
  const w=mountDialog({open:false,calibrating:editing})
  await w.setProps({open:true}); await flushPromises()
  expect((w.find('[aria-label="教材1名称"]').element as HTMLInputElement).value).toBe("已保存名称")
  expect(w.find('[aria-label="教材1名称"]').attributes("disabled")).toBeDefined()
  expect(button(w,"确认应用校准").attributes("disabled")).toBeDefined()
  expect(w.text()).toContain("受影响的历史题目会回到待审核")
  w.unmount()
})

it("后台识别可取消，取消前不能关闭或提交",async()=>{
  vi.useFakeTimers(); let cancelled=false
  invokeMock.mockImplementation((_d,method)=>{
    if(method==="inspect_cancel") cancelled=true
    return Promise.resolve({pick_pdfs:{paths:["sample.pdf"]},inspect_start:{token:"t"},
      inspect_cancel:{accepted:true}, inspect_status:cancelled ? {status:"cancelled",message:"已取消"}
        : {status:"running",stage:"读取页码",current:1,total:10,cancellable:true},
    }[method as string] ?? {})
  })
  const w=mountDialog({open:true})
  await button(w,"选择 PDF").trigger("click"); await flushPromises()
  expect(button(w,"关闭").attributes("disabled")).toBeDefined()
  await button(w,"取消处理").trigger("click"); await flushPromises()
  await vi.advanceTimersByTimeAsync(400); await flushPromises()
  expect(w.text()).toContain("已取消")
  expect(button(w,"关闭").attributes("disabled")).toBeUndefined()
  expect(invokeMock.mock.calls.some(([,method])=>method==="commit_batch")).toBe(false)
  w.unmount()
})

it("大量教材分页只绘制十本，跨页编辑与勾选不丢失，提交全部已选",async()=>{
  setup(Array.from({length:25},(_,index)=>({...row(),index,file_name:`book${index}.pdf`})))
  const w=mountDialog({open:true})
  await button(w,"选择 PDF").trigger("click"); await flushPromises()
  expect(w.findAll("article")).toHaveLength(10)
  await w.find('[aria-label="教材1名称"]').setValue("跨页保留的名称")
  await w.findAll('input[type="checkbox"]')[1].setValue(false)
  await button(w,"下一页教材").trigger("click")
  await button(w,"下一页教材").trigger("click")
  expect(w.findAll("article")).toHaveLength(5)
  expect(w.text()).toContain("第 3 / 3 页")
  await w.find('[aria-label="教材25版本"]').setValue("尾部版本")
  await button(w,"上一页教材").trigger("click")
  await button(w,"上一页教材").trigger("click")
  expect((w.find('[aria-label="教材1名称"]').element as HTMLInputElement).value).toBe("跨页保留的名称")
  expect((w.findAll('input[type="checkbox"]')[1].element as HTMLInputElement).checked).toBe(false)
  await button(w,"确认导入 24 本").trigger("click"); await flushPromises()
  const payload=invokeMock.mock.calls.find(([,method])=>method==="commit_batch")![2]
  expect(payload.items).toHaveLength(24)
  expect(payload.items.find((r:any)=>r.index===0).name).toBe("跨页保留的名称")
  expect(payload.items.find((r:any)=>r.index===24).version).toBe("尾部版本")
  expect(payload.items.some((r:any)=>r.index===1)).toBe(false)
  w.unmount()
})

it("弹窗不使用变换居中，头尾在独立滚动区外",()=>{
  const w=mountDialog({open:true})
  const dialog=w.find('[role="dialog"]')
  expect(dialog.classes().some(c=>c.includes("translate"))).toBe(false)
  const scroll=w.find('[data-testid="textbook-scroll"]')
  expect(scroll.find('[data-testid="textbook-header"]').exists()).toBe(false)
  expect(scroll.find('[data-testid="textbook-footer"]').exists()).toBe(false)
  expect(w.find('[data-testid="textbook-header"]').classes()).toContain("shrink-0")
  expect(w.find('[data-testid="textbook-footer"]').classes()).toContain("shrink-0")
  w.unmount()
})
