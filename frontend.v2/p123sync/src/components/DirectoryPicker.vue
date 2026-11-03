<template>
  <v-menu v-model="open" :close-on-content-click="false" location="bottom" max-width="520">
    <template #activator="{ props: menuProps }">
      <v-text-field :model-value="modelValue" :label="label" :placeholder="placeholder"
                    density="compact" variant="outlined" hide-details
                    @update:model-value="$emit('update:modelValue', $event)">
        <template #append-inner>
          <v-btn v-bind="menuProps" icon="mdi-folder-search-outline" variant="text"
                 size="small" :aria-label="`浏览${label}`" @click="startBrowse" />
        </template>
      </v-text-field>
    </template>
    <v-card min-width="300" max-width="520">
      <v-card-text class="pb-2">
        <div class="d-flex align-center ga-1 mb-2">
          <v-btn icon="mdi-home-outline" variant="text" size="small" aria-label="根目录"
                 :disabled="busy" @click="browse('/')" />
          <v-btn icon="mdi-arrow-up" variant="text" size="small" aria-label="上级目录"
                 :disabled="busy || current === '/'" @click="browse(parentPath(current))" />
          <div class="text-body-2 text-truncate flex-grow-1" :title="current">{{ current }}</div>
          <v-btn icon="mdi-refresh" variant="text" size="small" aria-label="刷新目录"
                 :disabled="busy" @click="browse(current)" />
        </div>
        <v-progress-linear v-if="busy" indeterminate color="primary" />
        <v-alert v-if="error" type="error" density="compact" class="mb-2">{{ error }}</v-alert>
        <div v-if="!busy && !error && !folders.length" class="text-body-2 text-medium-emphasis pa-2">
          当前目录没有子文件夹
        </div>
        <v-list v-else density="compact" class="folder-list">
          <v-list-item v-for="folder in folders" :key="folder" :title="folder"
                       prepend-icon="mdi-folder-outline" append-icon="mdi-chevron-right"
                       :disabled="busy" @click="browse(joinPath(current, folder))" />
        </v-list>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="open = false">取消</v-btn>
        <v-btn color="primary" variant="tonal" :disabled="busy || !!error"
               @click="selectCurrent">选择当前目录</v-btn>
      </v-card-actions>
    </v-card>
  </v-menu>
</template>

<script setup>
import { ref } from 'vue'

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
const busy = ref(false)
const current = ref('/')
const folders = ref([])
const error = ref('')
let requestSequence = 0

function joinPath(path, folder) {
  return `${path.replace(/\/$/, '')}/${folder}`
}
function parentPath(path) {
  const parent = path.replace(/\/+$/, '').replace(/\/[^/]*$/, '')
  return /^[A-Za-z]:$/.test(parent) ? `${parent}/` : (parent || '/')
}
function startBrowse() {
  browse(props.modelValue || '/')
}
async function browse(path) {
  const sequence = ++requestSequence
  busy.value = true
  error.value = ''
  folders.value = []
  try {
    const result = await props.api.post(`plugin/${props.pluginId}/browse_${props.remote ? 'remote' : 'local'}`, {
      path, ...(props.remote ? { passport: props.passport, password: props.password } : {}),
    })
    if (sequence !== requestSequence) return
    if (!result?.success) {
      error.value = result?.message || '读取目录失败'
    } else {
      current.value = result.path
      folders.value = result.folders || []
    }
  } catch (e) {
    if (sequence === requestSequence) error.value = `读取目录失败：${e}`
  } finally {
    if (sequence === requestSequence) busy.value = false
  }
}
function selectCurrent() {
  emit('update:modelValue', current.value)
  open.value = false
}
</script>

<style scoped>
.folder-list { max-height: 260px; overflow-y: auto; }
</style>
