<template>
  <v-menu v-model="open" :close-on-content-click="false" location="bottom"
          content-class="p123sync-directory-menu">
    <template #activator="{ props: menuProps }">
      <v-text-field v-bind="menuProps" :model-value="modelValue" :label="label"
                    :placeholder="placeholder" variant="underlined" density="compact" hide-details
                    @update:model-value="emit('update:modelValue', $event)" />
    </template>
    <v-card class="directory-dropdown" elevation="4">
      <v-progress-linear v-if="initializing" indeterminate color="primary" height="2" />
      <v-treeview v-model:activated="activated" v-model:opened="opened" :items="tree"
                  :load-children="loadChildren" item-title="name" item-value="path"
                  activatable return-object open-on-click color="primary" density="comfortable"
                  expand-icon="mdi-folder" collapse-icon="mdi-folder-open"
                  @click:open="activateOpened" @update:activated="activateSelected" />
      <div v-if="error" class="px-4 pb-3 text-body-2 text-error" role="alert">
        {{ error }}
        <v-btn variant="text" color="primary" size="small" @click="retry">重试</v-btn>
      </div>
    </v-card>
  </v-menu>
</template>

<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  label: { type: String, required: true },
  placeholder: { type: String, default: '' },
  api: { type: Object, required: true },
  pluginId: { type: String, required: true },
  remote: { type: Boolean, default: false },
  passport: { type: String, default: '' },
  password: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])
const open = ref(false)
const tree = ref([])
const opened = ref([])
const activated = ref([])
const initializing = ref(false)
const error = ref('')
let failedNode = null
let generation = 0
const inFlight = new WeakMap()

function node(name, path) {
  return { name, path, children: [], loaded: false }
}
function joinPath(path, name) {
  return `${path.replace(/\/$/, '')}/${name}`
}
function findNode(value, items = tree.value) {
  const path = typeof value === 'string' ? value : value?.path
  for (const item of items) {
    if (item.path === path) return item
    const found = findNode(path, item.children)
    if (found) return found
  }
  return null
}
function choose(value) {
  const item = findNode(value)
  if (!item) return
  activated.value = [item]
  emit('update:modelValue', item.path)
}
function activateOpened(event) {
  choose(event?.id)
}
function activateSelected(items) {
  if (items?.length) choose(items[0])
}

async function loadChildren(item) {
  const current = findNode(item)
  if (!current || current.loaded) return
  if (inFlight.has(current)) return inFlight.get(current)
  const currentGeneration = generation
  const request = (async () => {
    try {
      const result = await props.api.post(`plugin/${props.pluginId}/browse_${props.remote ? 'remote' : 'local'}`, {
        path: current.path,
        ...(props.remote ? { passport: props.passport, password: props.password } : {}),
      })
      if (currentGeneration !== generation) return
      if (!result?.success) throw new Error(result?.message || '读取目录失败')
      const base = result.path || current.path
      current.children = (result.folders || []).map(name => node(name, joinPath(base, name)))
      current.loaded = true
      if (failedNode === current) { error.value = ''; failedNode = null }
    } catch (e) {
      if (currentGeneration === generation) {
        failedNode = current
        error.value = e?.message || String(e)
      }
    } finally {
      inFlight.delete(current)
    }
  })()
  inFlight.set(current, request)
  return request
}
async function retry() {
  if (failedNode) await loadChildren(failedNode)
}
async function initialize() {
  const currentGeneration = ++generation
  const root = node('/', '/')
  tree.value = [root]
  opened.value = []
  activated.value = []
  error.value = ''
  failedNode = null
  initializing.value = true
  try {
    // Resolve the entered path only as far as existing directories. A typed
    // new destination remains untouched and can still be saved as-is.
    let current = tree.value[0]
    await loadChildren(current)
    if (currentGeneration !== generation) return
    opened.value = [current]
    const segments = String(props.modelValue || '').split('/').filter(Boolean)
    for (let index = 0; index < segments.length; index++) {
      const child = current.children.find(entry => entry.name === segments[index])
      if (!child) break
      current = child
      if (index === segments.length - 1) activated.value = [current]
      else {
        await loadChildren(current)
        if (currentGeneration !== generation) return
        opened.value.push(current)
      }
    }
  } finally {
    if (currentGeneration === generation) initializing.value = false
  }
}
watch(open, visible => {
  if (visible) initialize()
  else generation++
})
watch(() => [props.passport, props.password, props.remote], () => {
  if (open.value) initialize()
})
</script>

<style scoped>
.directory-dropdown { min-width: 260px; max-height: 20rem; overflow-y: auto; }
</style>
