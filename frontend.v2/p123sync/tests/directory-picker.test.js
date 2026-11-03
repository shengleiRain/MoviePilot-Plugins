import { describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import DirectoryPicker from '../src/components/DirectoryPicker.vue'

const passthrough = { template: '<div><slot /></div>' }
const stubs = {
  VMenu: { props: ['modelValue'], template: '<div><slot name="activator" :props="{ onClick: () => $emit(\'update:modelValue\', true) }" /><slot v-if="modelValue" /></div>' },
  VTextField: { props: ['modelValue'], inheritAttrs: false, template: '<input :value="modelValue" v-bind="$attrs" @input="$emit(\'update:modelValue\', $event.target.value)" />' },
  VTreeview: { props: ['items', 'opened', 'activated', 'loadChildren'], template: `<div><div v-for="item in items" :key="item.path"><button @click="$emit('click:open', { id: item.path })">{{ item.name }}</button><button v-for="child in item.children" :key="child.path" @click="$emit('click:open', { id: child.path }); loadChildren(child)">{{ child.name }}</button><template v-for="child in item.children" :key="child.path"><button v-for="sub in child.children" :key="sub.path" @click="$emit('update:activated', [sub])">{{ sub.name }}</button></template></div></div>` },
  VBtn: { template: '<button @click="$emit(\'click\')"><slot /></button>' },
  VCard: passthrough, VProgressLinear: passthrough,
}
function makeApi() {
  return { post: vi.fn(async (path, body) => ({ success: true, path: body.path, folders: body.path === '/' ? ['电影'] : body.path === '/电影' ? ['动作'] : [] })) }
}

describe('directory tree input', () => {
  it.each([['local', false], ['remote', true]])('opens from the field and selects an existing %s directory immediately', async (kind, remote) => {
    const api = makeApi()
    const wrapper = mount(DirectoryPicker, { props: { api, pluginId:'P123Sync', label:'目录', remote, passport:'account', password:'secret' }, global:{ stubs } })
    await wrapper.get('input').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(`plugin/P123Sync/browse_${kind}`, remote ? { path:'/', passport:'account', password:'secret' } : { path:'/' })
    await wrapper.findAll('button').find(button => button.text() === '电影').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('update:modelValue').at(-1)).toEqual(['/电影'])
    await wrapper.findAll('button').find(button => button.text() === '动作').trigger('click')
    expect(wrapper.emitted('update:modelValue').at(-1)).toEqual(['/电影/动作'])
  })
  it('expands the existing path when opening and preserves a manually typed new path', async () => {
    const api = makeApi()
    const wrapper = mount(DirectoryPicker, { props: { api, pluginId:'P123Sync', label:'目录', modelValue:'/电影/新建目录' }, global:{ stubs } })
    await wrapper.get('input').trigger('click')
    await flushPromises()
    expect(api.post.mock.calls.map(call => call[1].path)).toEqual(['/', '/电影'])
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await wrapper.get('input').setValue('/新路径')
    expect(wrapper.emitted('update:modelValue').at(-1)).toEqual(['/新路径'])
  })
  it('shows errors and lets the user retry without losing manual entry', async () => {
    const api = { post: vi.fn().mockResolvedValueOnce({ success:false, message:'目录读取失败' }).mockResolvedValueOnce({ success:true, path:'/', folders:['电影'] }) }
    const wrapper = mount(DirectoryPicker, { props: { api, pluginId:'P123Sync', label:'目录' }, global:{ stubs } })
    await wrapper.get('input').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('目录读取失败')
    await wrapper.findAll('button').find(button => button.text() === '重试').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('电影')
    expect(wrapper.text()).not.toContain('目录读取失败')
  })
})
