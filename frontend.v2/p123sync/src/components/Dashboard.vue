<template>
  <v-card class="p123sync-dashboard">
    <v-card-title class="d-flex align-center ga-2 text-subtitle-1">
      <v-icon icon="mdi-cloud-sync-outline" />
      123云盘同步
      <v-spacer />
      <v-chip v-if="phase" :color="phaseColor" size="small" label>{{ phaseText }}</v-chip>
    </v-card-title>
    <v-card-text v-if="status">
      <div v-if="running" class="text-body-2 mb-2">
        <div class="mb-1">{{ status.phase_detail || '处理中' }} · {{ status.pairs && Object.values(status.pairs).length }} 个任务对</div>
        <v-progress-linear :model-value="overallPercent" height="10" rounded color="primary" />
        <div class="mt-1 text-medium-emphasis">
          {{ fmtBytes(doneBytes) }} / {{ fmtBytes(totalBytes) }} · {{ fmtSpeed(status.speed) }}
          <span v-if="status.eta">· 剩余 {{ fmtDuration(status.eta) }}</span>
        </div>
      </div>
      <div v-else class="text-body-2 text-medium-emphasis">
        {{ lastSummary }}
      </div>
    </v-card-text>
    <v-card-text v-else class="text-body-2 text-medium-emphasis">暂无同步记录</v-card-text>
  </v-card>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { fmtBytes, fmtSpeed, fmtDuration, PHASE_TEXT, PHASE_COLOR } from '../utils'

const props = defineProps({
  api: { type: Object, required: true },
  pluginId: { type: String, default: 'P123Sync' },
})

const status = ref(null)
let timer = null

const running = computed(() => !!status.value?.running)
const phase = computed(() => status.value?.phase || null)
const phaseText = computed(() => PHASE_TEXT[phase.value] || phase.value || '')
const phaseColor = computed(() => PHASE_COLOR[phase.value] || 'grey')
const doneBytes = computed(() => (status.value?.bytes_done || 0) + (status.value?.bytes_skipped || 0))
const totalBytes = computed(() => status.value?.counters?.total_bytes || 0)
const overallPercent = computed(() => {
  if (!totalBytes.value) return 0
  return Math.min((doneBytes.value / totalBytes.value) * 100, 100)
})
const lastSummary = computed(() => {
  const s = status.value
  if (!s || !s.counters || !s.started_at) return '空闲'
  const c = s.counters
  return `上次：上传 ${c.done || 0} · 秒传 ${c.instant || 0} · 跳过 ${c.skipped || 0} · 失败 ${c.failed || 0}`
})

async function refresh() {
  try {
    status.value = await props.api.get(`plugin/${props.pluginId}/status`)
  } catch (e) {
    /* host not ready yet */
  }
}

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 5000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>
