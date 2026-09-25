import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import OcrReviewDialog from '../OcrReviewDialog.vue'
const invoke = vi.hoisted(() => vi.fn())
vi.mock('../../lib/bridge', () => ({ invoke }))
beforeEach(() => {
  invoke.mockReset()
  invoke.mockImplementation(async (_domain: string, method: string) => {
    if (method === 'ocr_review_history') return { items: [] }
    if (method === 'ocr_review_active') return { task: null }
    if (method === 'ocr_review_start') return { token: 'synthetic' }
    if (method === 'ocr_review_status') return {status:'completed',result:{id:'x',pdf_page:1,source_name:'synthetic.pdf',image:'data:image/png;base64,eA==',total_tokens:12,identical:false,results:[{model:'qwen3.5-ocr',ok:true,text:'<script>unsafe</script>'},{model:'qwen3.8-max',ok:false,error:'HTTP 403'}]}}
  })
})
it('requires consent and renders both outputs as plain text, not HTML', async () => {
  const wrapper=mount(OcrReviewDialog,{props:{library:{id:1,name:'合成教材'}}})
  await flushPromises()
  const start=wrapper.findAll('button').find(b=>b.text()==='开始单页复核')!
  expect(start.attributes('disabled')).toBeDefined()
  await wrapper.find('input[type="checkbox"]').setValue(true)
  await start.trigger('click'); await flushPromises()
  expect(invoke).toHaveBeenCalledWith('library','ocr_review_start',{library_id:1,pdf_page:1,model:'qwen3.8-max',consent:true})
  expect(wrapper.text()).toContain('HTTP 403')
  expect(wrapper.text()).toContain('<script>unsafe</script>')
  expect(wrapper.find('script').exists()).toBe(false)
  expect(wrapper.find('img').attributes('alt')).toContain('原始页面')
  wrapper.unmount()
})
it('does not invoke paid endpoints when opening or changing model', async () => {
  const wrapper=mount(OcrReviewDialog,{props:{library:{id:1,name:'合成教材'}}})
  await flushPromises()
  await wrapper.find('select').setValue('qwen3.7-plus')
  expect(invoke.mock.calls.some(c=>c[1]==='ocr_review_start')).toBe(false)
  wrapper.unmount()
})
