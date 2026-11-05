<template>
  <v-card class="p123sync-dashboard" variant="outlined"><v-card-text>
    <div class="d-flex align-center ga-2 mb-3"><v-icon icon="mdi-cloud-sync-outline" color="primary" /><span class="text-subtitle-1 font-weight-medium">123云盘同步</span><v-spacer /><v-chip v-if="phase" :color="phaseColor" size="small" variant="tonal" label>{{ phaseText }}</v-chip></div>
    <div v-if="error" class="text-body-2 text-warning">状态暂时不可用，正在重试</div>
    <template v-else-if="status && status.started_at">
      <div v-if="running" class="text-body-2 mb-2">{{ status.phase_detail || '正在处理' }}</div>
      <div v-if="running && status.activity" class="text-caption mb-2">
        {{ activityText(status.activity) }}<span v-if="status.activity.path"> · {{ status.activity.path }}</span>
        <span v-if="status.activity.stage === 'hashing' && status.activity.total_bytes"> · {{ Math.round(activityPercent(status.activity)) }}%</span>
      </div>
      <template v-if="running"><v-progress-linear :model-value="overallPercent" height="8" rounded color="primary" /><div class="text-caption text-medium-emphasis mt-2">{{ fmtBytes(doneBytes) }} / {{ fmtBytes(totalBytes) }} · {{ fmtSpeed(status.speed) }}<span v-if="status.eta"> · 剩余 {{ fmtDuration(status.eta) }}</span></div></template>
      <div v-else class="text-caption text-medium-emphasis mb-2">最近一次 · {{ fmtTime(status.started_at) }}</div>
      <div class="d-flex flex-wrap ga-4 mt-3"><div v-for="item in metrics" :key="item.key"><div class="text-subtitle-1 font-weight-medium">{{ status.counters?.[item.key] ?? 0 }}</div><div class="text-caption text-medium-emphasis">{{ item.label }}</div></div></div>
      <v-alert v-if="status.error" type="error" variant="tonal" density="compact" class="mt-3">{{ status.error }}</v-alert>
    </template>
    <div v-else class="text-body-2 text-medium-emphasis">暂无同步记录，在侧栏“123云盘同步”中开始任务。</div>
  </v-card-text></v-card>
</template>
<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { fmtBytes, fmtSpeed, fmtDuration, fmtTime, PHASE_TEXT, PHASE_COLOR, activityText, activityPercent } from '../utils'
const props = defineProps({ api: { type: Object, required: true }, pluginId: { type: String, default: 'P123Sync' } })
const status = ref(null)
const error = ref(false)
let timer = null
let pending = false
const running = computed(() => !!status.value?.running)
const phase = computed(() => status.value?.phase || null)
const phaseText = computed(() => phase.value === 'finished' && status.value?.counters?.failed
  ? '已完成（有失败）' : (PHASE_TEXT[phase.value] || phase.value || ''))
const phaseColor = computed(() => phase.value === 'finished' && status.value?.counters?.failed
  ? 'warning' : (PHASE_COLOR[phase.value] || 'grey'))
const doneBytes = computed(() => (status.value?.bytes_done || 0) + (status.value?.bytes_skipped || 0) + (status.value?.bytes_inflight || 0))
const totalBytes = computed(() => status.value?.counters?.total_bytes || 0)
const overallPercent = computed(() => totalBytes.value ? Math.min(doneBytes.value / totalBytes.value * 100, 100) : 0)
const metrics = [{key:'done',label:'已上传'}, {key:'instant',label:'秒传'}, {key:'skipped',label:'跳过'}, {key:'failed',label:'失败'}]
async function refresh() {
  if (pending) return
  pending = true
  try { status.value = await props.api.get(`plugin/${props.pluginId}/status?include_files=false`); error.value = false }
  catch { error.value = true }
  finally { pending = false }
}
onMounted(() => { refresh(); timer = setInterval(refresh, 5000) })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>
