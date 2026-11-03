import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import AppPage from '../src/components/AppPage.vue'

beforeAll(() => {
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} })
})
afterEach(() => vi.useRealTimers())

describe('sync task page', () => {
  it('shows configured tasks before any run, starts the selected task, and keeps disabled tasks disabled', async () => {
    const api = {
      get: vi.fn(async path => {
        if (path.endsWith('/tasks')) return { tasks: [
          { id: 'movie', name: '电影', source_path: '/movies', target_path: '/电影', enabled: true, policy: 'skip_same_size' },
          { id: 'tv', name: '剧集', source_path: '/tv', target_path: '/剧集', enabled: false, policy: 'overwrite' },
        ] }
        if (path.endsWith('/history')) return { list: [] }
        return { running: false, phase: 'idle', started_at: null, pairs: {}, files: [] }
      }),
      post: vi.fn(async () => ({ success: true, message: '同步已开始' })),
    }
    const vuetify = createVuetify({ components, directives })
    const wrapper = mount(AppPage, { props: { api }, global: { plugins: [vuetify], stubs: { VSnackbar: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('电影')
    expect(wrapper.text()).toContain('/movies')
    expect(wrapper.text()).toContain('/剧集')
    expect(wrapper.text()).toContain('已停用')
    const buttons = wrapper.findAll('button').filter(button => button.text().trim() === '同步')
    expect(buttons).toHaveLength(2)
    expect(buttons[0].attributes('disabled')).toBeUndefined()
    expect(buttons[1].attributes('disabled')).toBeDefined()
    await buttons[0].trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('plugin/P123Sync/run', { task_ids: ['movie'] })
    wrapper.unmount()
  })
  it('exposes task loading errors rather than claiming there are no configured tasks', async () => {
    const api = { get: vi.fn(async path => {
      if (path.endsWith('/tasks')) throw new Error('unavailable')
      return path.endsWith('/history') ? { list: [] } : { phase: 'idle', running: false, files: [] }
    }) }
    const wrapper = mount(AppPage, { props: { api }, global: { plugins: [createVuetify({ components, directives })], stubs: { VSnackbar: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('部分数据读取失败')
    expect(wrapper.text()).not.toContain('还没有同步任务')
    wrapper.unmount()
  })
})
