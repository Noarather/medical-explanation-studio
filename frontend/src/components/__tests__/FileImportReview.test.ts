import { expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import FileImportReview from '../FileImportReview.vue'

function review() {
  return { draft_id: 'draft', name: '测试', path: 'synthetic.json', total: 2, ready: 1, issues: 1, removed: 0, repaired: 1,
    items: [{ index: 0, number: 1, original: {题干: '原题', 解析: '<script>原文不能执行</script>'},
      row: { id: 'a', subject: '测试', type: 'A1', question: '原题', options: ['A.甲', 'B.乙', 'D.丁', 'E.戊'], answer: 'D' },
      errors: ['缺少选项：C'], removed: false, repaired: false }] }
}
const button = (wrapper: ReturnType<typeof mount>, text: string) => wrapper.findAll('button').find(b => b.text().includes(text))!

it('显示完整原文，补选项后保存，删除仅发出本次预览操作', async () => {
  const wrapper = mount(FileImportReview, {props: {review: review(), busy: false}})
  expect(wrapper.get('[aria-label="题目原文"]').text()).toContain('<script>原文不能执行</script>')
  expect(wrapper.find('script').exists()).toBe(false)
  expect((button(wrapper, '确认导入').element as HTMLButtonElement).disabled).toBe(true)
  await button(wrapper, '补一个选项').trigger('click')
  expect((wrapper.get('[aria-label="异常题选项"]').element as HTMLTextAreaElement).value).toBe('A.甲\nB.乙\nC. \nD.丁\nE.戊')
  await wrapper.get('[aria-label="异常题选项"]').setValue('A.甲\nB.乙\nC.补充内容\nD.丁\nE.戊')
  await button(wrapper, '保存修正').trigger('click')
  expect(wrapper.emitted('resolve')![0]).toEqual(['edit', 0, {...review().items[0]!.row, options: ['A.甲','B.乙','C.补充内容','D.丁','E.戊']}])
  await button(wrapper, '删除本次').trigger('click')
  expect(wrapper.emitted('resolve')![1]).toEqual(['remove', 0, undefined])
  expect(wrapper.text()).toContain('不会修改原文件或已入库题目')
})

it('删除可撤销；确认导入只发出提交请求', async () => {
  const data = review(); data.issues = 0; data.removed = 1
  data.items[0]!.removed = true; data.items[0]!.errors = []
  const wrapper = mount(FileImportReview, {props: {review: data, busy: false}})
  await button(wrapper, '撤销删除').trigger('click')
  expect(wrapper.emitted('resolve')![0]).toEqual(['restore', 0, undefined])
  await button(wrapper, '确认导入').trigger('click')
  expect(wrapper.emitted('commit')).toHaveLength(1)
})

it('未保存编辑阻止提交，刷新其他题的校验不会覆盖输入', async () => {
  const data = review(); data.issues = 0; data.items[0]!.errors = []
  const wrapper = mount(FileImportReview, {props: {review: data, busy: false}})
  await wrapper.get('[aria-label="异常题答案"]').setValue('B')
  await wrapper.setProps({review: JSON.parse(JSON.stringify(data))})
  expect((wrapper.get('[aria-label="异常题答案"]').element as HTMLInputElement).value).toBe('B')
  expect((button(wrapper, '确认导入').element as HTMLButtonElement).disabled).toBe(true)
  await button(wrapper, '保存修正').trigger('click')
  data.items[0]!.row.answer = 'B'
  await wrapper.setProps({review: {...data}})
  expect((button(wrapper, '确认导入').element as HTMLButtonElement).disabled).toBe(false)
})

it('大量异常分页完整展示，处理中禁用表单', async () => {
  const data = review()
  data.items = Array.from({length: 23}, (_, index) => ({...data.items[0]!, index, number: index + 1}))
  const wrapper = mount(FileImportReview, {props: {review: data, busy: false}})
  expect(wrapper.findAll('details')).toHaveLength(20)
  await button(wrapper, '下一页处理项').trigger('click')
  expect(wrapper.findAll('details')).toHaveLength(3)
  expect(wrapper.text()).toContain('第 23 题')
  await wrapper.setProps({busy: true})
  expect((wrapper.get('fieldset').element as HTMLFieldSetElement).disabled).toBe(true)
})
