import {expect,it} from 'vitest'
import {mount} from '@vue/test-utils'
import BatchIssueEditor from '../BatchIssueEditor.vue'
it('字段表单保留未编辑的扩展和病例',async()=>{
  const row={id:'a',bank:'school',type:'A1',question:'题干',subject:'内科',options:['A.甲','B.乙'],answer:'',caseInfo:'病例',extensions:{x:1}}
  const wrapper=mount(BatchIssueEditor,{props:{row}})
  await wrapper.get('[aria-label="异常题答案"]').setValue('A')
  await wrapper.findAll('button').find(b=>b.text().includes('保存修正'))!.trigger('click')
  expect(wrapper.emitted('save')![0]![0]).toEqual({...row,answer:'A'})
})
it('填空答案保留大小写和同义答案，坏 JSON 阻止保存',async()=>{
  const wrapper=mount(BatchIssueEditor,{props:{row:{id:'f',type:'fill',options:[],answer:[['ATP','三磷酸腺苷']]}}})
  const button=wrapper.findAll('button').find(b=>b.text().includes('保存修正'))!
  await button.trigger('click')
  expect((wrapper.emitted('save')![0]![0] as any).answer).toEqual([['ATP','三磷酸腺苷']])
  await wrapper.get('[aria-label="异常题答案"]').setValue('broken'); await button.trigger('click')
  expect(wrapper.emitted('save')).toHaveLength(1)
  expect(wrapper.text()).toContain('JSON 格式有误')
})
