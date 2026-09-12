import {beforeEach, expect, it, vi} from "vitest"
import {createPinia, setActivePinia} from "pinia"
import {flushPromises, mount} from "@vue/test-utils"
const {invokeMock} = vi.hoisted(() => ({invokeMock: vi.fn()}))
vi.mock("../../lib/bridge", () => ({invoke: invokeMock}))
import BatchImportPanel from "../BatchImportPanel.vue"

beforeEach(() => { setActivePinia(createPinia()); invokeMock.mockReset() })
it("批量导入只展开异常，提交可用题并显示保存的异常", async () => {
  const issue = {index:2,source:"fixture.json",number:3,row:{id:"bad",answer:""},reasons:["缺少答案"]}
  const result = {draft_id:"draft",total:3,ready:1,duplicates:1,issues:1,items:[issue],result:{}}
  invokeMock.mockImplementation((_domain:string, method:string) => Promise.resolve({
    batch_active:{task:null}, batch_start:{token:'t'}, batch_status:{status:'completed',result},
    import_profiles:{profiles:{}}, batch_drafts:{drafts:[]}, pick_batch_files:{paths:["fixture.json"]},
    batch_preview:result, batch_commit:{...result,ready:0,result:{imported:1}}, list_sets:{sets:[]},
  }[method as string] ?? {sets:[]}))
  const wrapper = mount(BatchImportPanel)
  await flushPromises()
  await wrapper.find('input[aria-label="批次名称"]').setValue("测试")
  const button = (text:string) => wrapper.findAll("button").find(item=>item.text().includes(text))!
  await button("选择多个").trigger("click"); await flushPromises()
  await button("检查批次").trigger("click"); await flushPromises()
  expect(wrapper.findAll("details")).toHaveLength(1)
  expect(wrapper.text()).toContain("重复跳过 1")
  await button("导入全部可用题目").trigger("click"); await flushPromises()
  expect(invokeMock).toHaveBeenCalledWith("imports","batch_start",{action:'commit',payload:{draft_id:"draft",start_generation:true}})
  expect(wrapper.text()).toContain("缺少答案")
  wrapper.unmount()
})

it("超过 200 条异常可以翻页查看尾部", async () => {
  const items=Array.from({length:205},(_,index)=>({index,source:'a.json',number:index+1,row:{id:`q${index}`},reasons:['缺少答案']}))
  invokeMock.mockImplementation((_d,method)=>Promise.resolve({import_profiles:{profiles:{}},batch_drafts:{drafts:[]},
    batch_active:{task:{token:'t'}},batch_status:{status:'completed',result:{draft_id:'d',total:205,ready:0,duplicates:0,issues:205,items,result:{}}},
  }[method as string] ?? {}))
  const wrapper=mount(BatchImportPanel); await flushPromises()
  expect(wrapper.findAll('details')).toHaveLength(50)
  for(let i=0;i<4;i++) await wrapper.findAll('button').find(b=>b.text()==='下一页异常')!.trigger('click')
  expect(wrapper.findAll('details')).toHaveLength(5)
  expect(wrapper.text()).toContain('q204'); wrapper.unmount()
})
