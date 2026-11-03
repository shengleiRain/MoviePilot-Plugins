import { describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import DirectoryPicker from '../src/components/DirectoryPicker.vue'

const passthrough = { template: '<div><slot /></div>' }
const stubs = {
  VMenu: { template: '<div><slot name="activator" :props="{}" /><slot /></div>' },
  VTextField: { props: ['modelValue'], template: '<div><input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" /><slot name="append-inner" /></div>' },
  VBtn: { props: ['disabled', 'ariaLabel'], template: '<button :disabled="disabled" :aria-label="ariaLabel" @click="$emit(\'click\')"><slot /></button>' },
  VListItem: { props: ['title', 'disabled'], template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ title }}</button>' },
  VCard: passthrough, VCardText: passthrough, VCardActions: passthrough, VSpacer: passthrough,
  VProgressLinear: passthrough, VAlert: passthrough, VList: passthrough,
}

describe('directory picker', () => {
  it.each([['local', false], ['remote', true]])('browses and selects existing %s directories', async (kind, remote) => {
    const api = { post: vi.fn(async (path, body) => ({ success: true, path: body.path, folders: body.path === '/' ? ['电影'] : [] })) }
    const wrapper = mount(DirectoryPicker, { props: { api, pluginId: 'P123Sync', label: '目录', remote, passport: 'account', password: 'secret' }, global: { stubs } })
    await wrapper.get('[aria-label="浏览目录"]').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(`plugin/P123Sync/browse_${kind}`, remote ? { path:'/', passport:'account', password:'secret' } : { path:'/' })
    await wrapper.findAll('button').find(button => button.text() === '电影').trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '选择当前目录').trigger('click')
    expect(wrapper.emitted('update:modelValue').at(-1)).toEqual(['/电影'])
  })
  it('prevents selection after a listing error and keeps manual entry available', async () => {
    const api = { post: vi.fn(async () => ({ success: false, message: '目录读取失败' })) }
    const wrapper = mount(DirectoryPicker, { props: { api, pluginId:'P123Sync', label:'目录' }, global: { stubs } })
    await wrapper.get('[aria-label="浏览目录"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('目录读取失败')
    expect(wrapper.findAll('button').find(button => button.text() === '选择当前目录').attributes('disabled')).toBeDefined()
    await wrapper.get('input').setValue('/new/path')
    expect(wrapper.emitted('update:modelValue').at(-1)).toEqual(['/new/path'])
  })
})
