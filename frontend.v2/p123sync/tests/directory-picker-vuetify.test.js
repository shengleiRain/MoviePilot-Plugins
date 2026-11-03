import { afterEach, beforeAll, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import DirectoryPicker from '../src/components/DirectoryPicker.vue'

beforeAll(() => {
  HTMLElement.prototype.scrollIntoView = () => {}
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })
  globalThis.visualViewport = { addEventListener() {}, removeEventListener() {}, width:1024, height:768, offsetLeft:0, offsetTop:0 }
})
afterEach(() => { document.body.innerHTML = '' })

it('opens a real Vuetify tree under the field and updates the chosen folder', async () => {
  const api = { post: vi.fn(async (_path, body) => ({ success: true, path: body.path, folders: body.path === '/' ? ['电影'] : body.path === '/电影' ? ['动作'] : [] })) }
  const wrapper = mount(DirectoryPicker, {
    attachTo: document.body,
    props: { api, pluginId:'P123Sync', label:'本地目录', modelValue:'/' },
    global: { plugins: [createVuetify({ components, directives })] },
  })
  await wrapper.get('input').trigger('click')
  await flushPromises()
  const folder = [...document.querySelectorAll('.v-list-item')].find(el => el.textContent.trim() === '电影')
  expect(folder).toBeTruthy()
  folder.click()
  await flushPromises()
  expect(wrapper.emitted('update:modelValue')?.some(event => event[0] === '/电影')).toBe(true)
  expect([...document.querySelectorAll('.v-list-item')].some(el => el.textContent.trim() === '动作')).toBe(true)
  wrapper.unmount()
})
