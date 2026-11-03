<template>
  <v-card class="p123sync-config">
    <v-card-text class="config-scroll">
      <v-alert v-if="message" :type="message.type" density="compact" class="mb-4">{{ message.text }}</v-alert>
      <v-alert v-if="pendingConfig" type="info" variant="tonal" density="compact" class="mb-4">
        当前显示待生效设置；同步及未解决的失败处理完成后自动应用。
        <v-btn size="small" variant="text" :loading="discarding" @click="discardPending">取消待生效设置</v-btn>
      </v-alert>

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
          <v-text-field v-model="config.cron" label="手动任务定时 Cron" placeholder="0 3 * * *"
                        hint="仅对手动同步任务生效；留空则不定时" persistent-hint
                        variant="outlined" density="compact" />
        </v-col>
        <v-col cols="12" sm="6" class="d-flex align-center flex-wrap ga-3">
          <v-switch v-model="config.notify" label="同步完成发送通知" color="primary" hide-details density="compact" />
          <v-btn variant="tonal" color="primary" size="small" :loading="testing" @click="testLogin">验证登录</v-btn>
        </v-col>
        <v-col cols="12" sm="6" class="d-flex align-center flex-wrap ga-3">
          <v-switch v-model="config.verify_md5" label="严格MD5校验" color="primary" hide-details density="compact" />
          <span class="text-caption text-medium-emphasis" style="max-width: 26rem;">
            默认关闭：同名同大小直接跳过，速度快。开启后跳过前仍读取文件比对MD5（更慢更严格）。上传与安全覆盖始终校验MD5。
          </span>
        </v-col>
      </v-row>

      <v-divider class="my-5" />
      <div class="d-flex align-center flex-wrap ga-2 mb-1">
        <div class="text-subtitle-1 font-weight-medium">同步任务</div><v-spacer />
        <v-btn size="small" variant="tonal" color="primary" prepend-icon="mdi-plus" @click="addTask">添加任务</v-btn>
      </div>
      <div class="text-body-2 text-medium-emphasis mb-4">源目录使用 MoviePilot 容器内路径；目标目录不存在时会自动创建。同名且大小一致的文件默认直接跳过；如需逐文件比对MD5，请开启严格MD5校验。隐藏文件、下载临时文件和系统垃圾会被忽略。</div>

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
            <v-col cols="12" sm="6">
              <v-select v-model="task.trigger_mode" :items="triggerItems" item-title="title" item-value="value"
                        label="同步方式" variant="outlined" density="compact" hide-details />
            </v-col>
            <v-col v-if="task.trigger_mode !== 'manual'" cols="12" sm="6">
              <v-text-field v-model.number="task.settle_seconds" type="number" min="30" max="3600"
                            label="文件稳定时间（秒）" variant="outlined" density="compact" hide-details />
            </v-col>
            <v-col v-if="task.trigger_mode !== 'manual'" cols="12">
              <div class="text-caption text-medium-emphasis">
                {{ task.trigger_mode === 'directory' ? '周期检查目录，首次启用也会处理已有文件；稳定时间仅是启发式，下载未完成的文件建议使用下载器监控。' : '仅处理下载器确认完成且已选择、并能映射到本地源目录的文件。' }}
              </div>
            </v-col>
            <v-col cols="12">
              <v-text-field v-model="task.exclude_ext" label="排除扩展名（逗号分隔）" placeholder=".nfo,.txt"
                            variant="outlined" density="compact" hide-details />
            </v-col>
          </v-row>
        </v-card-text>
      </v-card>
      <v-alert v-if="!config.tasks.length" type="info" variant="tonal" density="compact">还没有任务，点击“添加任务”开始配置。</v-alert>
    </v-card-text>
    <v-card-actions class="config-actions px-4 py-3 d-flex flex-wrap ga-2">
      <v-btn color="primary" variant="flat" :loading="saving" @click="save">保存</v-btn>
      <v-btn variant="tonal" :loading="loading" @click="load">重新加载</v-btn>
      <v-spacer />
      <div class="space-summary">
        <span class="text-caption text-medium-emphasis">{{ pendingConfig ? '待生效账号已用 / 总空间' : '云盘已用 / 总空间' }}</span>
        <span class="text-body-2 ml-2">{{ space ? `${fmtBytes(space.used)} / ${fmtBytes(space.total)}` : '— / —' }}</span>
        <v-btn size="small" variant="text" :loading="spaceLoading" @click="refreshSpace">刷新</v-btn>
        <div v-if="spaceError" class="text-caption text-medium-emphasis">{{ spaceError }}</div>
      </div>
    </v-card-actions>
  </v-card>
</template>

<script setup>
import { nextTick, onMounted, reactive, ref, watch } from 'vue'
import DirectoryPicker from './DirectoryPicker.vue'
import { fmtBytes } from '../utils'

const props = defineProps({
  api: { type: Object, required: true },
  pluginId: { type: String, default: 'P123Sync' },
})

const policyItems = [
  { title: '大小一致时跳过（推荐）', value: 'skip_same_size' },
  { title: '覆盖（旧文件进回收站）', value: 'overwrite' },
  { title: '保留两者（自动改名）', value: 'keep_both' },
]
const triggerItems = [
  { title: '手动同步', value: 'manual' },
  { title: '目录监控', value: 'directory' },
  { title: '下载器监控', value: 'downloader' },
]

let taskKeySeq = 1
const config = reactive({ enabled: false, passport: '', password: '', cron: '', notify: true, verify_md5: false, tasks: [] })
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const discarding = ref(false)
const pendingConfig = ref(false)
const message = ref(null)
const space = ref(null)
const spaceLoading = ref(false)
const spaceError = ref('')
let spaceSequence = 0
watch(() => [config.passport, config.password], () => {
  spaceSequence++
  space.value = null
  spaceError.value = ''
  spaceLoading.value = false
})

async function refreshSpace() {
  const sequence = ++spaceSequence
  if (!config.passport || !config.password) {
    space.value = null
    spaceError.value = '填写账号和密码后可查看空间用量'
    return
  }
  spaceLoading.value = true
  spaceError.value = ''
  try {
    const result = await props.api.post(`plugin/${props.pluginId}/space`, { passport: config.passport, password: config.password })
    if (sequence !== spaceSequence) return
    if (!result?.success || !result?.space) throw new Error(result?.message || '空间信息暂不可用')
    space.value = result.space
  } catch (e) {
    if (sequence === spaceSequence) { space.value = null; spaceError.value = `空间信息读取失败：${e?.message || e}` }
  } finally {
    if (sequence === spaceSequence) spaceLoading.value = false
  }
}

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
    trigger_mode: task?.trigger_mode || 'manual',
    settle_seconds: task?.settle_seconds || 120,
  }
}

async function load() {
  loading.value = true
  message.value = null
  try {
    const remote = await props.api.get(`plugin/${props.pluginId}/get_config`)
    pendingConfig.value = !!remote?.pending_config
    config.enabled = !!remote?.enabled
    config.passport = remote?.passport || ''
    config.password = remote?.password || ''
    config.cron = remote?.cron || ''
    config.notify = remote?.notify !== false
    config.verify_md5 = !!remote?.verify_md5
    config.tasks = (remote?.tasks || []).map(normalizeTask)
    await nextTick()
    if (config.passport && config.password && !space.value) void refreshSpace()
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
    verify_md5: config.verify_md5,
    tasks: config.tasks.map(task => ({
      id: task.id || undefined,
      name: task.name,
      source_path: task.source_path,
      target_path: task.target_path,
      policy: task.policy,
      exclude_ext: task.exclude_ext,
      enabled: task.enabled,
      trigger_mode: task.trigger_mode,
      settle_seconds: task.settle_seconds,
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
      message.value = { type: result?.schedule_error ? 'warning' : (result?.deferred ? 'info' : 'success'), text: result?.message || '已保存' }
    }
  } catch (e) {
    message.value = { type: 'error', text: `保存失败：${e}` }
  } finally {
    saving.value = false
  }
}

async function discardPending() {
  discarding.value = true
  try {
    const result = await props.api.post(`plugin/${props.pluginId}/discard_pending_config`)
    if (!result?.success) message.value = { type: 'error', text: result?.message || '取消失败' }
    else {
      await load()
      message.value = { type: 'success', text: result?.message || '已取消待生效设置' }
    }
  } catch (e) {
    message.value = { type: 'error', text: `取消失败：${e}` }
  } finally {
    discarding.value = false
  }
}

async function testLogin() {
  const sequence = ++spaceSequence
  spaceLoading.value = false
  testing.value = true
  message.value = null
  try {
    const result = await props.api.post(`plugin/${props.pluginId}/test_login`, { passport: config.passport, password: config.password })
    if (sequence !== spaceSequence) return
    if (result?.success && result?.space) { space.value = result.space; spaceError.value = '' }
    message.value = {
      type: result?.success ? 'success' : 'error',
      text: result?.message || (result?.success ? '登录成功' : '登录失败'),
    }
  } catch (e) {
    if (sequence === spaceSequence) message.value = { type: 'error', text: `验证失败：${e}` }
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.p123sync-config { max-height: min(82vh, 880px); display: flex; flex-direction: column; }
.config-scroll { overflow-y: auto; min-height: 0; flex: 1 1 auto; }
.space-summary { min-width: 0; }
.config-actions { flex: 0 0 auto; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
</style>
