<template>
  <div class="p123sync-apppage pa-4">
    <!-- 顶部概览 -->
    <v-card class="mb-4">
      <v-card-title class="d-flex flex-wrap align-center ga-3">
        <v-icon icon="mdi-cloud-sync-outline" />
        123云盘同步
        <v-chip v-if="status" :color="phaseColor" label size="small">{{ phaseText }}</v-chip>
        <v-chip v-if="status?.trigger" size="small" variant="tonal">
          {{ status.trigger === 'schedule' ? '定时触发' : '手动触发' }}
        </v-chip>
        <v-spacer />
        <v-btn color="primary" variant="flat" prepend-icon="mdi-play"
               :loading="starting" :disabled="running" @click="runAll">
          全部同步
        </v-btn>
        <v-btn color="warning" variant="tonal" prepend-icon="mdi-stop"
               :disabled="!running" @click="cancelRun">
          取消
        </v-btn>
      </v-card-title>

      <v-card-text v-if="status && status.started_at">
        <v-row>
          <v-col cols="12" md="6">
            <div class="text-body-2 mb-1">
              {{ status.phase_detail || (running ? '处理中…' : '空闲') }}
            </div>
            <v-progress-linear :model-value="overallPercent" height="14" rounded color="primary" />
            <div class="mt-2 text-body-2 text-medium-emphasis">
              {{ fmtBytes(doneBytes) }} / {{ fmtBytes(totalBytes) }}
              · 速度 {{ fmtSpeed(status.speed) }}
              <span v-if="status.eta">· 预计剩余 {{ fmtDuration(status.eta) }}</span>
              <span v-if="status.elapsed">· 已用 {{ fmtDuration(status.elapsed) }}</span>
            </div>
          </v-col>
          <v-col cols="6" sm="3" md="2" v-for="item in counterItems" :key="item.key">
            <div class="text-h6">{{ status.counters?.[item.key] ?? 0 }}</div>
            <div class="text-caption text-medium-emphasis">{{ item.label }}</div>
          </v-col>
        </v-row>
        <v-alert v-if="status.error" type="error" density="compact" class="mt-2">
          {{ status.error }}
        </v-alert>
      </v-card-text>
      <v-card-text v-else class="text-body-2 text-medium-emphasis">
        尚未运行过同步。配置好任务对后点「全部同步」。
      </v-card-text>
    </v-card>

    <v-tabs v-model="tab" density="compact" class="mb-2">
      <v-tab value="tasks">任务对</v-tab>
      <v-tab value="files">
        文件明细
        <v-chip v-if="fileCount" size="x-small" class="ml-2">{{ fileCount }}</v-chip>
      </v-tab>
      <v-tab value="history">历史</v-tab>
    </v-tabs>

    <!-- 任务对 -->
    <v-window v-model="tab">
      <v-window-item value="tasks">
        <v-card v-if="pairs.length" class="mb-3">
          <v-list lines="three">
            <v-list-item v-for="pair in pairs" :key="pair.id">
              <template #prepend>
                <v-icon :icon="pair.enabled ? 'mdi-folder-sync-outline' : 'mdi-folder-off-outline'"
                        :color="pair.enabled ? 'primary' : 'grey'" />
              </template>
              <v-list-item-title class="d-flex align-center ga-2 flex-wrap">
                <span>{{ pair.name || pair.source_path }}</span>
                <v-chip size="x-small" variant="tonal">{{ policyText(pair.policy) }}</v-chip>
                <v-chip v-if="!pair.enabled" size="x-small" color="grey">已停用</v-chip>
              </v-list-item-title>
              <v-list-item-subtitle>
                {{ pair.source_path }} → 123云盘:{{ pair.target_path }}
              </v-list-item-subtitle>
              <template #append>
                <v-btn size="small" variant="tonal" color="primary"
                       :disabled="running || !pair.enabled" @click="runOne(pair.id)">
                  同步
                </v-btn>
              </template>
              <div v-if="pairProgress(pair.id)" class="pr-14">
                <v-progress-linear :model-value="pairPercent(pair.id)" height="8" rounded class="my-1" />
                <div class="text-caption text-medium-emphasis">
                  {{ pairProgress(pair.id).current || pairPhaseText(pair.id) }}
                </div>
              </div>
            </v-list-item>
          </v-list>
        </v-card>
        <v-card v-else>
          <v-card-text class="text-body-2 text-medium-emphasis">
            还没有配置任务对，请到插件设置中添加。
          </v-card-text>
        </v-card>
      </v-window-item>

      <!-- 文件明细 -->
      <v-window-item value="files">
        <v-card>
          <v-card-text class="d-flex align-center ga-3">
            <v-switch v-model="onlyActive" density="compact" hide-details
                      label="只看进行中/失败" />
            <v-spacer />
            <span class="text-caption text-medium-emphasis">共 {{ visibleFiles.length }} 条</span>
          </v-card-text>
          <v-table density="compact">
            <thead>
              <tr>
                <th class="text-left">文件</th>
                <th class="text-left">大小</th>
                <th class="text-left" style="min-width: 180px">进度</th>
                <th class="text-left">速度</th>
                <th class="text-left">状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="file in visibleFiles" :key="file.key">
                <td class="text-body-2" :title="file.path">
                  {{ shortName(file.path) }}
                  <div class="text-caption text-medium-emphasis">{{ file.pair_name }}</div>
                  <div v-if="file.error" class="text-caption text-error">{{ file.error }}</div>
                </td>
                <td class="text-body-2">{{ fmtBytes(file.size) }}</td>
                <td>
                  <v-progress-linear :model-value="file.percent || 0" height="6" rounded
                                     :color="fileStateColor(file.state)" />
                </td>
                <td class="text-body-2">{{ fmtSpeed(file.speed) }}</td>
                <td>
                  <v-chip :color="fileStateColor(file.state)" size="small" label>
                    {{ fileStateText(file.state) }}
                  </v-chip>
                </td>
              </tr>
              <tr v-if="!visibleFiles.length">
                <td colspan="5" class="text-body-2 text-medium-emphasis">暂无记录</td>
              </tr>
            </tbody>
          </v-table>
        </v-card>
      </v-window-item>

      <!-- 历史 -->
      <v-window-item value="history">
        <v-card>
          <v-table density="compact">
            <thead>
              <tr>
                <th class="text-left">开始时间</th>
                <th class="text-left">触发</th>
                <th class="text-left">结果</th>
                <th class="text-left">上传/秒传/跳过/失败</th>
                <th class="text-left">流量</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in history" :key="item.run_id">
                <td class="text-body-2">{{ fmtTime(item.started_at) }}</td>
                <td class="text-body-2">{{ item.trigger === 'schedule' ? '定时' : '手动' }}</td>
                <td>
                  <v-chip :color="phaseColorOf(item.phase)" size="small" label>
                    {{ phaseTextOf(item.phase) }}
                  </v-chip>
                </td>
                <td class="text-body-2">
                  {{ item.counters?.done ?? 0 }} / {{ item.counters?.instant ?? 0 }} /
                  {{ item.counters?.skipped ?? 0 }} / {{ item.counters?.failed ?? 0 }}
                </td>
                <td class="text-body-2">{{ fmtBytes(item.bytes_done) }}</td>
              </tr>
              <tr v-if="!history.length">
                <td colspan="5" class="text-body-2 text-medium-emphasis">暂无历史</td>
              </tr>
            </tbody>
          </v-table>
        </v-card>
      </v-window-item>
    </v-window>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" :timeout="3000">
      {{ snackbar.text }}
    </v-snackbar>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
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
const tab = ref('tasks')
const onlyActive = ref(true)
const starting = ref(false)
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
const phaseText = computed(() => PHASE_TEXT[status.value?.phase] || status.value?.phase || '')
const phaseColor = computed(() => PHASE_COLOR[status.value?.phase] || 'grey')
const counterItems = [
  { key: 'done', label: '已上传' },
  { key: 'instant', label: '秒传' },
  { key: 'skipped', label: '跳过' },
  { key: 'failed', label: '失败' },
]

const pairs = computed(() => {
  const map = status.value?.pairs || {}
  return Object.values(map)
})

const fileCount = computed(() => status.value?.files?.length || 0)
const visibleFiles = computed(() => {
  const files = status.value?.files || []
  if (!onlyActive.value) return files
  return files.filter(f => ['uploading', 'failed', 'cancelled'].includes(f.state))
})

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
function phaseTextOf(phase) {
  return PHASE_TEXT[phase] || phase
}
function phaseColorOf(phase) {
  return PHASE_COLOR[phase] || 'grey'
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
  try {
    status.value = await props.api.get(`plugin/${props.pluginId}/status`)
    if (full) {
      const hist = await props.api.get(`plugin/${props.pluginId}/history`)
      history.value = hist?.list || []
    }
  } catch (e) {
    /* transient */
  }
}

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
</style>
