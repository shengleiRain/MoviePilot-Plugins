<template>
  <div class="p123sync-apppage pa-4">
    <div class="d-flex align-center flex-wrap ga-3 mb-4">
      <div>
        <div class="text-h5 font-weight-medium">123云盘同步</div>
        <div class="text-body-2 text-medium-emphasis mt-1">查看任务、传输进度和最近的同步记录</div>
      </div>
      <v-spacer />
      <v-btn variant="tonal" prepend-icon="mdi-refresh" :loading="refreshing" @click="refresh(true)">刷新</v-btn>
      <v-btn v-if="running" color="warning" variant="tonal" prepend-icon="mdi-stop" @click="cancelRun">取消同步</v-btn>
      <v-btn color="primary" variant="flat" prepend-icon="mdi-play" :loading="starting"
             :disabled="running || !tasksLoaded || !hasEnabledTasks" @click="runAll">全部同步</v-btn>
    </div>

    <v-alert v-if="loadError" type="warning" variant="tonal" density="compact" class="mb-4">
      {{ loadError }} <v-btn variant="text" size="small" @click="refresh(true)">重试</v-btn>
    </v-alert>
    <v-card variant="outlined" class="mb-4">
      <v-card-text>
        <div class="d-flex align-center flex-wrap ga-2 mb-3">
          <span class="text-subtitle-1 font-weight-medium">{{ status?.started_at ? '同步概览' : '准备就绪' }}</span>
          <v-chip v-if="status" :color="phaseColor" variant="tonal" size="small" label>{{ phaseText }}</v-chip>
          <v-chip v-if="status?.started_at" size="small" variant="tonal">{{ triggerText(status.trigger) }}</v-chip>
          <v-spacer />
          <span v-if="status?.started_at" class="text-caption text-medium-emphasis">{{ fmtTime(status.started_at) }}</span>
        </div>
        <template v-if="status?.started_at">
          <div class="d-flex justify-space-between ga-2 text-body-2 mb-2">
            <span>{{ status.phase_detail || phaseText }}</span><span>{{ Math.round(overallPercent) }}%</span>
          </div>
          <v-progress-linear :model-value="overallPercent" height="10" rounded color="primary" />
          <div class="mt-2 text-body-2 text-medium-emphasis d-flex flex-wrap ga-3">
            <span>{{ fmtBytes(doneBytes) }} / {{ fmtBytes(totalBytes) }}</span><span>速度 {{ fmtSpeed(status.speed) }}</span>
            <span v-if="status.eta">剩余 {{ fmtDuration(status.eta) }}</span><span v-if="status.elapsed">已用 {{ fmtDuration(status.elapsed) }}</span>
          </div>
          <v-row dense class="mt-3">
            <v-col v-for="item in counterItems" :key="item.key" cols="6" sm="3">
              <div class="stat-box pa-3"><div class="text-h6">{{ status.counters?.[item.key] ?? 0 }}</div>
                <div class="text-caption text-medium-emphasis">{{ item.label }}</div></div>
            </v-col>
          </v-row>
          <v-alert v-if="status.error" type="error" density="compact" class="mt-3">{{ status.error }}</v-alert>
          <div v-if="status.phase === 'error' && !failedCount" class="text-caption text-medium-emphasis mt-2">请到下方同步任务中重新运行相应任务。</div>
        </template>
        <div v-else class="text-body-2 text-medium-emphasis">{{ hasEnabledTasks ? '选择“全部同步”或运行单个任务。' : '请先在插件设置中添加并启用任务。' }}</div>
      </v-card-text>
    </v-card>

    <v-tabs v-model="tab" density="compact" color="primary" class="mb-3">
      <v-tab value="tasks">同步任务 <v-chip size="x-small" class="ml-2">{{ pairs.length }}</v-chip></v-tab>
      <v-tab value="files">文件明细 <v-chip v-if="fileCount" size="x-small" class="ml-2">{{ fileCount }}</v-chip></v-tab>
      <v-tab value="history">历史记录</v-tab>
    </v-tabs>
    <v-window v-model="tab">
      <v-window-item value="tasks">
        <div v-if="pairs.length" class="task-grid">
          <v-card v-for="pair in pairs" :key="pair.id" variant="outlined">
            <v-card-text>
              <div class="d-flex align-center flex-wrap ga-2 mb-3">
                <v-icon :icon="pair.enabled ? 'mdi-folder-sync-outline' : 'mdi-folder-off-outline'" :color="pair.enabled ? 'primary' : 'grey'" />
                <span class="text-subtitle-1 font-weight-medium task-title">{{ pair.name || pair.source_path }}</span>
                <v-chip v-if="!pair.enabled" size="x-small">已停用</v-chip><v-spacer />
                <v-btn size="small" variant="tonal" color="primary" prepend-icon="mdi-play"
                       :disabled="running || starting || !pair.enabled" @click="runOne(pair.id)">同步</v-btn>
              </div>
              <div class="path-row"><span class="text-caption text-medium-emphasis">本地源目录</span><span :title="pair.source_path">{{ pair.source_path }}</span></div>
              <div class="path-row mt-2"><span class="text-caption text-medium-emphasis">123云盘目标</span><span :title="pair.target_path">{{ pair.target_path }}</span></div>
              <v-chip size="small" variant="tonal" class="mt-3">{{ policyText(pair.policy) }}</v-chip>
              <div v-if="pairProgress(pair.id)" class="mt-3">
                <div class="text-caption text-medium-emphasis mb-1">{{ pairProgress(pair.id).current || pairPhaseText(pair.id) }}</div>
                <v-progress-linear :model-value="pairPercent(pair.id)" height="6" rounded color="primary" />
              </div>
            </v-card-text>
          </v-card>
        </div>
        <v-card v-else variant="outlined"><v-card-text class="text-body-2 text-medium-emphasis">{{ tasksLoaded ? '还没有同步任务，请在插件设置中添加。' : '正在读取任务…' }}</v-card-text></v-card>
      </v-window-item>
      <v-window-item value="files">
        <v-card variant="outlined">
          <v-card-text class="d-flex align-center flex-wrap ga-3">
            <v-switch v-model="onlyActive" density="compact" hide-details label="只看进行中 / 失败 / 已取消" />
            <v-spacer />
            <v-btn v-if="failedCount" variant="tonal" color="error" size="small" prepend-icon="mdi-refresh"
                   :disabled="running || starting" :loading="starting" @click="retryFiles()">重试全部失败（{{ failedCount }}）</v-btn>
            <span class="text-caption text-medium-emphasis">共 {{ fileTotal }} 条</span>
          </v-card-text>
          <v-alert v-if="filesError" type="warning" variant="tonal" density="compact" class="mx-4 mb-2">文件明细读取失败 <v-btn size="small" variant="text" @click="refreshFiles">重试</v-btn></v-alert>
          <v-table density="compact"><thead><tr><th>文件</th><th>大小</th><th style="min-width: 160px">进度</th><th>速度</th><th>状态</th><th>操作</th></tr></thead>
            <tbody><tr v-for="file in visibleFiles" :key="file.key">
              <td :title="file.path"><div class="text-body-2">{{ shortName(file.path) }}</div><div class="text-caption text-medium-emphasis">{{ file.pair_name }}</div><div v-if="file.error" class="text-caption text-error">{{ file.error }}</div></td>
              <td>{{ fmtBytes(file.size) }}</td><td><v-progress-linear :model-value="file.percent || 0" height="6" rounded :color="fileStateColor(file.state)" /></td>
              <td>{{ fmtSpeed(file.speed) }}</td><td><v-chip :color="fileStateColor(file.state)" size="small" variant="tonal" label>{{ fileStateText(file.state) }}</v-chip></td>
              <td><v-btn v-if="file.state === 'failed'" size="small" variant="text" color="primary" :disabled="running || starting" @click="retryFiles([file.key])">重试</v-btn></td>
            </tr><tr v-if="!visibleFiles.length"><td colspan="6" class="text-body-2 text-medium-emphasis">{{ filesLoading ? '正在读取文件…' : (filesError ? '文件明细暂时不可用' : (fileCount ? '没有符合筛选条件的文件' : '暂无文件记录')) }}</td></tr></tbody>
          </v-table>
          <v-card-text v-if="fileTotal" class="d-flex align-center flex-wrap ga-2">
            <span class="text-caption text-medium-emphasis">第 {{ filePage }} / {{ filePages }} 页</span><v-spacer />
            <v-btn size="small" variant="tonal" :disabled="filePage <= 1 || filesLoading" @click="filePage--">上一页</v-btn>
            <v-btn size="small" variant="tonal" :disabled="filePage >= filePages || filesLoading" @click="filePage++">下一页</v-btn>
          </v-card-text>
        </v-card>
      </v-window-item>
      <v-window-item value="history">
        <v-card variant="outlined"><v-table density="compact"><thead><tr><th>开始时间</th><th>触发</th><th>结果</th><th>上传 / 秒传 / 跳过 / 失败</th><th>流量</th></tr></thead>
          <tbody><tr v-for="item in history" :key="item.run_id"><td>{{ fmtTime(item.started_at) }}</td><td>{{ triggerText(item.trigger) }}</td>
            <td><v-chip :color="item.phase === 'finished' && item.counters?.failed ? 'warning' : phaseColorOf(item.phase)" size="small" variant="tonal" label>{{ phaseTextOf(item.phase, item.counters?.failed) }}</v-chip></td>
            <td>{{ item.counters?.done ?? 0 }} / {{ item.counters?.instant ?? 0 }} / {{ item.counters?.skipped ?? 0 }} / {{ item.counters?.failed ?? 0 }}</td><td>{{ fmtBytes(item.bytes_done) }}</td></tr>
            <tr v-if="!history.length"><td colspan="5" class="text-body-2 text-medium-emphasis">暂无历史记录</td></tr>
          </tbody></v-table></v-card>
      </v-window-item>
    </v-window>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" :timeout="3000">
      {{ snackbar.text }}
    </v-snackbar>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  fmtBytes, fmtSpeed, fmtDuration, fmtTime,
  PHASE_TEXT, PHASE_COLOR, FILE_STATE_TEXT, FILE_STATE_COLOR, POLICY_TEXT,
} from '../utils'

const props = defineProps({
  api: { type: Object, required: true },
  pluginId: { type: String, default: 'P123Sync' },
  navKey: { type: String, default: '' },
})

const status = ref(null)
const history = ref([])
const tasks = ref([])
const tasksLoaded = ref(false)
const loadError = ref('')
const refreshing = ref(false)
const tab = ref('tasks')
const onlyActive = ref(true)
const starting = ref(false)
const filePage = ref(1)
const fileTotal = ref(0)
const filePages = ref(1)
const visibleFiles = ref([])
const filesLoading = ref(false)
const filesError = ref(false)
let fileRequestSequence = 0
const snackbar = reactive({ show: false, text: '', color: 'success' })
let timer = null

const running = computed(() => !!status.value?.running)
const counters = computed(() => status.value?.counters || {})
const doneBytes = computed(() => (status.value?.bytes_done || 0) + (status.value?.bytes_skipped || 0))
const totalBytes = computed(() => counters.value.total_bytes || 0)
const overallPercent = computed(() => {
  if (!totalBytes.value) return 0
  return Math.min((doneBytes.value / totalBytes.value) * 100, 100)
})
const phaseText = computed(() => status.value?.phase === 'finished' && failedCount.value
  ? '已完成（有失败）' : (PHASE_TEXT[status.value?.phase] || status.value?.phase || ''))
const phaseColor = computed(() => status.value?.phase === 'finished' && failedCount.value
  ? 'warning' : (PHASE_COLOR[status.value?.phase] || 'grey'))
const counterItems = [
  { key: 'done', label: '已上传' },
  { key: 'instant', label: '秒传' },
  { key: 'skipped', label: '跳过' },
  { key: 'failed', label: '失败' },
]

const pairs = computed(() => tasks.value)
const hasEnabledTasks = computed(() => pairs.value.some(pair => pair.enabled))

const fileCount = computed(() => status.value?.counters?.total_files || 0)
const failedCount = computed(() => status.value?.counters?.failed || 0)

function pairProgress(pairId) {
  return status.value?.pairs?.[pairId] || null
}
function pairPercent(pairId) {
  const pair = pairProgress(pairId)
  if (!pair || !pair.total) return 0
  const finished = (pair.done || 0) + (pair.instant || 0) + (pair.skipped || 0) + (pair.failed || 0)
  return Math.min((finished / pair.total) * 100, 100)
}
function pairPhaseText(pairId) {
  const pair = pairProgress(pairId)
  return PHASE_TEXT[pair?.phase] || ''
}
function policyText(policy) {
  return POLICY_TEXT[policy] || policy
}
function fileStateText(state) {
  return FILE_STATE_TEXT[state] || state
}
function fileStateColor(state) {
  return FILE_STATE_COLOR[state] || 'grey'
}
function phaseTextOf(phase, failed) {
  return phase === 'finished' && failed ? '已完成（有失败）' : (PHASE_TEXT[phase] || phase)
}
function phaseColorOf(phase) {
  return PHASE_COLOR[phase] || 'grey'
}
function triggerText(trigger) {
  return ({ schedule: '定时触发', manual: '手动触发', retry: '重试失败文件' })[trigger] || trigger || '-'
}
function shortName(path) {
  const parts = String(path).split('/')
  return parts.length > 1 ? `${parts[parts.length - 2]}/${parts[parts.length - 1]}` : path
}

function toast(text, color = 'success') {
  snackbar.text = text
  snackbar.color = color
  snackbar.show = true
}

async function refresh(full = false) {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const calls = [props.api.get(`plugin/${props.pluginId}/status?include_files=false`)]
    if (full || !tasksLoaded.value) {
      calls.push(props.api.get(`plugin/${props.pluginId}/tasks`), props.api.get(`plugin/${props.pluginId}/history`))
    }
    const results = await Promise.allSettled(calls)
    if (results[0].status === 'fulfilled') status.value = results[0].value
    if (results.length > 1 && results[1].status === 'fulfilled') {
      tasks.value = results[1].value?.tasks || []
      tasksLoaded.value = true
    }
    if (results.length > 2 && results[2].status === 'fulfilled') history.value = results[2].value?.list || []
    loadError.value = results.some(result => result.status === 'rejected') ? '部分数据读取失败，页面可能不是最新状态。' : ''
    await refreshFiles()
  } finally {
    refreshing.value = false
  }
}

async function refreshFiles() {
  const seq = ++fileRequestSequence
  filesLoading.value = true
  try {
    const result = await props.api.get(`plugin/${props.pluginId}/files?page=${filePage.value}&page_size=20&active_only=${onlyActive.value}`)
    if (seq !== fileRequestSequence) return
    // A new run can start while status and page requests are in flight.
    if (status.value?.run_id && result?.run_id !== status.value.run_id) {
      filesError.value = true
      return
    }
    visibleFiles.value = result?.items || []
    fileTotal.value = result?.total || 0
    filePages.value = result?.pages || 1
    if (result?.page && filePage.value !== result.page) filePage.value = result.page
    filesError.value = false
  } catch (e) {
    if (seq === fileRequestSequence) filesError.value = true
  } finally {
    if (seq === fileRequestSequence) filesLoading.value = false
  }
}

async function retryFiles(fileKeys = []) {
  if (!status.value?.run_id || running.value || starting.value) return
  starting.value = true
  try {
    const result = await props.api.post(`plugin/${props.pluginId}/retry`, {
      run_id: status.value.run_id, file_keys: fileKeys,
    })
    toast(result?.message || (result?.success ? '重试已开始' : '重试失败'), result?.success ? 'success' : 'error')
    await refresh(true)
  } catch (e) {
    toast(`重试失败：${e}`, 'error')
  } finally {
    starting.value = false
  }
}

watch(onlyActive, () => {
  if (filePage.value !== 1) filePage.value = 1
  else refreshFiles()
})
watch(filePage, refreshFiles)

async function triggerRun(payload) {
  starting.value = true
  try {
    const result = await props.api.post(`plugin/${props.pluginId}/run`, payload)
    if (result?.success) {
      toast(result.message || '同步已开始')
      tab.value = 'tasks'
    } else {
      toast(result?.message || '启动失败', 'error')
    }
    await refresh()
  } catch (e) {
    toast(String(e), 'error')
  } finally {
    starting.value = false
  }
}

function runAll() {
  triggerRun({})
}

function runOne(taskId) {
  triggerRun({ task_ids: [taskId] })
}

async function cancelRun() {
  try {
    const result = await props.api.post(`plugin/${props.pluginId}/cancel`)
    toast(result?.message || '已请求取消', result?.success ? 'warning' : 'error')
    await refresh()
  } catch (e) {
    toast(String(e), 'error')
  }
}

onMounted(() => {
  refresh(true)
  timer = setInterval(() => {
    refresh(!running.value)
  }, 2000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.p123sync-apppage {
  max-width: 1200px;
  margin: 0 auto;
}
.stat-box { background: rgba(var(--v-theme-on-surface), .04); border-radius: 10px; }
.task-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 380px), 1fr)); gap: 12px; }
.task-title { min-width: 0; overflow-wrap: anywhere; }
.path-row { display: flex; flex-direction: column; gap: 2px; font-size: .875rem; overflow-wrap: anywhere; }

</style>
