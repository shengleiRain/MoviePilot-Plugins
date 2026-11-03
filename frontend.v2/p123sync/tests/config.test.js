import { beforeAll, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import Config from '../src/components/Config.vue'

beforeAll(() => {
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  window.matchMedia = () => ({ matches:false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })
})

it('keeps queued settings visible with a pending notice and allows discarding them', async () => {
  let pending = false
  const api = {
    get:vi.fn(async () => ({ enabled:true, passport: pending ? 'new' : 'old', password:'secret', tasks:[], pending_config:pending })),
    post:vi.fn(async path => {
      if (path.endsWith('/save_config')) { pending=true; return {success:true,deferred:true,message:'设置待生效'} }
      if (path.endsWith('/discard_pending_config')) { pending=false; return {success:true,message:'已取消待生效设置'} }
    }),
  }
  const wrapper = mount(Config, {props:{api}, global:{plugins:[createVuetify({components,directives})]}})
  await flushPromises()
  await wrapper.findAll('button').find(button => button.text().trim() === '保存').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('当前显示待生效设置')
  expect(wrapper.text()).toContain('设置待生效')
  expect(wrapper.findAll('input').some(input => input.element.value === 'new')).toBe(true)
  await wrapper.findAll('button').find(button => button.text().includes('取消待生效设置')).trigger('click')
  await flushPromises()
  expect(wrapper.text()).not.toContain('当前显示待生效设置')
  expect(wrapper.findAll('input').some(input => input.element.value === 'old')).toBe(true)
  wrapper.unmount()
})

it('shows used and total space without a remaining-space success banner', async () => {
  const api = {
    get:vi.fn(async () => ({enabled:true,passport:'account',password:'secret',tasks:[]})),
    post:vi.fn(async path => path.endsWith('/space') || path.endsWith('/test_login')
      ? {success:true,message:'登录成功',space:{used:1024**4,total:2*1024**4}} : {success:true}),
  }
  const wrapper = mount(Config, {props:{api}, global:{plugins:[createVuetify({components,directives})]}})
  await flushPromises()
  expect(wrapper.text()).toContain('1.00 TB / 2.00 TB')
  await wrapper.findAll('button').find(button => button.text().includes('验证登录')).trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('登录成功')
  expect(wrapper.text()).not.toContain('剩余空间')
  expect(wrapper.find('.config-scroll').exists()).toBe(true)
  expect(wrapper.find('.config-actions').exists()).toBe(true)
  expect(wrapper.find('.config-actions').text()).toContain('1.00 TB / 2.00 TB')
  wrapper.unmount()
})

it('persists each task monitoring strategy and stable interval', async () => {
  const api={get:vi.fn(async()=>({enabled:true,passport:'',password:'',tasks:[{id:'p',name:'Test',source_path:'/data',target_path:'/',trigger_mode:'manual'}]})),
             post:vi.fn(async()=>({success:true,message:'已保存'}))}
  const wrapper=mount(Config,{props:{api},global:{plugins:[createVuetify({components,directives})]}})
  await flushPromises()
  const strategy=wrapper.findAllComponents({name:'VSelect'}).find(component=>component.props('label')==='同步方式')
  expect(strategy).toBeTruthy()
  strategy.vm.$emit('update:modelValue','directory')
  await flushPromises()
  expect(wrapper.text()).toContain('周期检查目录')
  expect(wrapper.findAllComponents({name:'VTextField'}).some(component=>component.props('label')==='文件稳定时间（秒）')).toBe(true)
  await wrapper.findAll('button').find(button=>button.text().trim()==='保存').trigger('click')
  await flushPromises()
  const request=api.post.mock.calls.find(call=>call[0].endsWith('/save_config'))
  expect(request[1].config.tasks[0]).toMatchObject({trigger_mode:'directory',settle_seconds:120})
  wrapper.unmount()
})

it('round-trips the strict MD5 verification toggle', async () => {
  const api={get:vi.fn(async()=>({enabled:true,passport:'',password:'',verify_md5:true,tasks:[]})),
             post:vi.fn(async()=>({success:true,message:'已保存'}))}
  const wrapper=mount(Config,{props:{api},global:{plugins:[createVuetify({components,directives})]}})
  await flushPromises()
  expect(wrapper.text()).toContain('严格MD5校验')
  await wrapper.findAll('button').find(button=>button.text().trim()==='保存').trigger('click')
  await flushPromises()
  let request=api.post.mock.calls.find(call=>call[0].endsWith('/save_config'))
  expect(request[1].config.verify_md5).toBe(true)
  const strict=wrapper.findAllComponents({name:'VSwitch'}).find(component=>component.props('label')==='严格MD5校验')
  expect(strict).toBeTruthy()
  strict.vm.$emit('update:modelValue',false)
  await flushPromises()
  await wrapper.findAll('button').find(button=>button.text().trim()==='保存').trigger('click')
  await flushPromises()
  request=api.post.mock.calls.filter(call=>call[0].endsWith('/save_config'))
  expect(request[1][1].config.verify_md5).toBe(false)
  wrapper.unmount()
})
