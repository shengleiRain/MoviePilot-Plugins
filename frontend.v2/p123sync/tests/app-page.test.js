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
  it('shows the live file stage and local hash progress without calling it network transfer', async () => {
    const api = { get: vi.fn(async path => {
      if (path.includes('/status')) return { run_id:'r', running:true, phase:'uploading', started_at:1,
        counters:{total_files:1,total_bytes:100,failed:0}, pairs:{}, bytes_transferred:0,
        activity:{key:'p:movie.mkv',path:'movie.mkv',stage:'hashing',elapsed:8,processed_bytes:40,total_bytes:100} }
      if (path.endsWith('/tasks')) return { tasks: [] }
      if (path.endsWith('/history')) return { list: [] }
      return {run_id:'r',page:1,pages:1,total:1,items:[{key:'p:movie.mkv',path:'movie.mkv',state:'pending',size:100,percent:0}]}
    }) }
    const wrapper = mount(AppPage, { props:{api}, global:{plugins:[createVuetify({components, directives})], stubs:{VSnackbar:true}} })
    await flushPromises()
    expect(wrapper.text()).toContain('读取本地文件并计算 MD5')
    expect(wrapper.text()).toContain('movie.mkv')
    expect(wrapper.text()).toContain('本地已读取 40 B / 100 B')
    expect(wrapper.text()).toContain('已确认传输 0 B')
    await wrapper.findAll('button').find(button => button.text().includes('文件明细')).trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('读取本地文件并计算 MD5')
    wrapper.unmount()
  })

  it('labels upload progress as confirmed by completed parts', async () => {
    const api = { get: vi.fn(async path => {
      if (path.includes('/status')) return { run_id:'r', running:true, phase:'uploading', started_at:1,
        counters:{total_files:1,total_bytes:100,failed:0}, pairs:{}, bytes_transferred:30,
        activity:{key:'p:movie.mkv',path:'movie.mkv',stage:'uploading',elapsed:8,processed_bytes:30,total_bytes:100} }
      if (path.endsWith('/tasks')) return { tasks: [] }
      if (path.endsWith('/history')) return { list: [] }
      return {run_id:'r',page:1,pages:1,total:0,items:[]}
    }) }
    const wrapper = mount(AppPage, { props:{api}, global:{plugins:[createVuetify({components, directives})], stubs:{VSnackbar:true}} })
    await flushPromises()
    expect(wrapper.text()).toContain('上传分片')
    expect(wrapper.text()).toContain('已确认上传 30 B / 100 B')
    expect(wrapper.text()).toContain('分片完成后更新')
    wrapper.unmount()
  })

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

it('paginates failures from the API and retries only the selected record', async () => {
  const files = Array.from({length: 25}, (_, index) => ({
    key: `p1:file-${String(index).padStart(2, '0')}.mkv`, path: `file-${String(index).padStart(2, '0')}.mkv`,
    pair_name: '电影', size: 1, state: 'failed', error: 'network', percent: 0,
  }))
  const api = {
    get: vi.fn(async path => {
      if (path.includes('/status')) return { run_id:'run-1', running:false, phase:'finished', started_at:1, counters:{total_files:25,failed:25}, pairs:{} }
      if (path.includes('/tasks')) return {tasks:[]}
      if (path.includes('/history')) return {list:[]}
      if (path.includes('/files?')) {
        const page = Number(new URL(`https://example.test/${path}`).searchParams.get('page'))
        return { run_id:'run-1', page, pages:2, total:25, items:files.slice((page-1)*20, page*20) }
      }
      throw new Error(`Unexpected: ${path}`)
    }),
    post: vi.fn(async () => ({success:true, message:'已开始重试'})),
  }
  const wrapper = mount(AppPage, { props:{api}, global:{plugins:[createVuetify({components, directives})], stubs:{VSnackbar:true}} })
  await flushPromises()
  const tab = wrapper.findAll('button').find(button => button.text().includes('文件明细'))
  await tab.trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('file-00.mkv')
  expect(wrapper.text()).not.toContain('file-20.mkv')
  await wrapper.findAll('button').find(button => button.text().trim() === '下一页').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('file-20.mkv')
  expect(wrapper.text()).not.toContain('file-00.mkv')
  await wrapper.findAll('button').find(button => button.text().trim() === '重试').trigger('click')
  await flushPromises()
  expect(api.post).toHaveBeenCalledWith('plugin/P123Sync/retry', { run_id:'run-1', file_keys:['p1:file-20.mkv'] })
  wrapper.unmount()
})

it('opens durable historical file details without treating them as current retries', async () => {
  const api={get:vi.fn(async path=>{
    if(path.includes('/status')) return {run_id:'current',running:false,phase:'finished',counters:{total_files:0,failed:0},pairs:{}}
    if(path.includes('/tasks')) return {tasks:[]}
    if(path.includes('/history')) return {list:[{run_id:'previous',started_at:1,phase:'finished',counters:{failed:1}}]}
    if(path.includes('/files?')) return path.includes('run_id=previous')
      ? {run_id:'previous',page:1,pages:1,total:1,items:[{key:'p:old',path:'old.mkv',state:'failed',size:3,error:'old error'}]}
      : {run_id:'current',page:1,pages:1,total:0,items:[]}
  })}
  const wrapper=mount(AppPage,{props:{api},global:{plugins:[createVuetify({components,directives})],stubs:{VSnackbar:true}}})
  await flushPromises()
  await wrapper.findAll('button').find(button=>button.text().includes('历史记录')).trigger('click')
  await flushPromises()
  await wrapper.findAll('button').find(button=>button.text().trim()==='查看').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('正在查看历史文件明细')
  expect(wrapper.text()).toContain('old.mkv')
  expect(wrapper.findAll('button').filter(button=>button.text().trim()==='重试')).toHaveLength(0)
  wrapper.unmount()
})
