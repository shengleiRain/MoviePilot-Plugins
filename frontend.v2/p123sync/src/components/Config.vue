<template>
  <v-card class="p123sync-config">
    <v-card-text>
      <v-alert v-if="message" :type="message.type" density="compact" class="mb-4">{{ message.text }}</v-alert>

      <div class="d-flex align-center flex-wrap ga-2 mb-3">
        <div class="text-subtitle-1 font-weight-medium">基本设置</div><v-spacer />
        <v-switch v-model="config.enabled" label="启用插件" color="primary" hide-details density="compact" />
      </div>
      <v-row dense>
        <v-col cols="12" sm="6">
          <v-text-field v-model="config.passport" label="123云盘账号（手机号/邮箱）"
                        variant="outlined" density="compact" hide-details />
        </v-col>
        <v-col cols="12" sm="6">
          <v-text-field v-model="config.password" label="密码" type="password"
                        variant="outlined" density="compact" hide-details />
        </v-col>
        <v-col cols="12" sm="6">
          <v-text-field v-model="config.cron" label="定时同步 Cron" placeholder="0 3 * * *"
                        hint="留空时仅手动同步；填写 5 位表达式" persistent-hint
                        variant="outlined" density="compact" />
        </v-col>
        <v-col cols="12" sm="6" class="d-flex align-center flex-wrap ga-3">
          <v-switch v-model="config.notify" label="同步完成发送通知" color="primary" hide-details density="compact" />
          <v-btn variant="tonal" color="primary" size="small" :loading="testing" @click="testLogin">验证登录</v-btn>
        </v-col>
      </v-row>

      <v-divider class="my-5" />
      <div class="d-flex align-center flex-wrap ga-2 mb-1">
        <div class="text-subtitle-1 font-weight-medium">同步任务</div><v-spacer />
        <v-btn size="small" variant="tonal" color="primary" prepend-icon="mdi-plus" @click="addTask">添加任务</v-btn>
      </div>
      <div class="text-body-2 text-medium-emphasis mb-4">本地目录使用 MoviePilot 容器内的路径；云盘目标目录不存在时会自动创建。</div>

      <v-card v-for="(task, index) in config.tasks" :key="task._key" variant="outlined" class="mb-3">
        <v-card-text>
          <div class="d-flex align-center ga-2 mb-3">
            <span class="text-subtitle-2">任务 {{ index + 1 }}</span><v-spacer />
            <v-switch v-model="task.enabled" label="启用" color="primary" density="compact" hide-details />
            <v-btn icon="mdi-delete-outline" variant="text" color="error" size="small"
                   :aria-label="`删除任务 ${index + 1}`" @click="removeTask(index)" />
          </div>
          <v-row dense>
            <v-col cols="12" sm="6">
              <v-text-field v-model="task.name" label="任务名称（可选）" variant="outlined" density="compact" hide-details />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select v-model="task.policy" :items="policyItems" item-title="title" item-value="value"
                        label="同名策略" variant="outlined" density="compact" hide-details />
            </v-col>
            <v-col cols="12" md="6">
              <DirectoryPicker v-model="task.source_path" label="本地源目录" placeholder="/mp/movies"
                               :api="api" :plugin-id="pluginId" />
            </v-col>
            <v-col cols="12" md="6">
              <DirectoryPicker v-model="task.target_path" label="123云盘目标目录" placeholder="/test/movies"
                               :api="api" :plugin-id="pluginId" remote :passport="config.passport" :password="config.password" />
            </v-col>
            <v-col cols="12">
              <v-text-field v-model="task.exclude_ext" label="排除扩展名（逗号分隔）" placeholder=".nfo,.txt"
                            variant="outlined" density="compact" hide-details />
            </v-col>
          </v-row>
        </v-card-text>
      </v-card>
      <v-alert v-if="!config.tasks.length" type="info" variant="tonal" density="compact">还没有任务，点击“添加任务”开始配置。</v-alert>
      <v-divider class="my-5" />
      <div class="d-flex flex-wrap ga-2">
        <v-btn color="primary" variant="flat" :loading="saving" @click="save">保存</v-btn>
        <v-btn variant="tonal" :loading="loading" @click="load">重新加载</v-btn>
      </div>
    </v-card-text>
  </v-card>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import DirectoryPicker from './DirectoryPicker.vue'

const props = defineProps({
  api: { type: Object, required: true },
  pluginId: { type: String, default: 'P123Sync' },
})

const policyItems = [
  { title: '相同大小跳过（推荐）', value: 'skip_same_size' },
  { title: '覆盖（旧文件进回收站）', value: 'overwrite' },
  { title: '保留两者（自动改名）', value: 'keep_both' },
]

let taskKeySeq = 1
const config = reactive({ enabled: false, passport: '', password: '', cron: '', notify: true, tasks: [] })
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const message = ref(null)

function normalizeTask(task) {
  return {
    id: task?.id || '',
    _key: `t${taskKeySeq++}`,
    name: task?.name || '',
    source_path: task?.source_path || '',
    target_path: task?.target_path || '',
    policy: task?.policy || 'skip_same_size',
    exclude_ext: Array.isArray(task?.exclude_ext) ? task.exclude_ext.join(',') : (task?.exclude_ext || ''),
    enabled: task?.enabled !== false,
  }
}

async function load() {
  loading.value = true
  message.value = null
  try {
    const remote = await props.api.get(`plugin/${props.pluginId}/get_config`)
    config.enabled = !!remote?.enabled
    config.passport = remote?.passport || ''
    config.password = remote?.password || ''
    config.cron = remote?.cron || ''
    config.notify = remote?.notify !== false
    config.tasks = (remote?.tasks || []).map(normalizeTask)
  } catch (e) {
    message.value = { type: 'error', text: `读取配置失败：${e}` }
  } finally {
    loading.value = false
  }
}

function addTask() {
  config.tasks.push(normalizeTask({}))
}

function removeTask(index) {
  config.tasks.splice(index, 1)
}

function buildPayload() {
  return {
    enabled: config.enabled,
    passport: config.passport,
    password: config.password,
    cron: String(config.cron || '').trim(),
    notify: config.notify,
    tasks: config.tasks.map(task => ({
      id: task.id || undefined,
      name: task.name,
      source_path: task.source_path,
      target_path: task.target_path,
      policy: task.policy,
      exclude_ext: task.exclude_ext,
      enabled: task.enabled,
    })),
  }
}

async function save() {
  saving.value = true
  message.value = null
  try {
    const result = await props.api.post(`plugin/${props.pluginId}/save_config`, { config: buildPayload() })
    if (result?.success === false) {
      message.value = { type: 'error', text: result?.message || '保存失败' }
    } else {
      await load()
      message.value = { type: 'success', text: '已保存' }
    }
  } catch (e) {
    message.value = { type: 'error', text: `保存失败：${e}` }
  } finally {
    saving.value = false
  }
}

async function testLogin() {
  testing.value = true
  message.value = null
  try {
    const result = await props.api.post(`plugin/${props.pluginId}/test_login`, { passport: config.passport, password: config.password })
    message.value = {
      type: result?.success ? 'success' : 'error',
      text: result?.message || (result?.success ? '登录成功' : '登录失败'),
    }
  } catch (e) {
    message.value = { type: 'error', text: `验证失败：${e}` }
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>
