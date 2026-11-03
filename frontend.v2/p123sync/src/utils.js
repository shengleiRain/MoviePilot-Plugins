export function fmtBytes(value) {
  const n = Number(value) || 0
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(2)} GB`
}

export function fmtSpeed(value) {
  const n = Number(value) || 0
  if (n <= 0) return '-'
  return `${fmtBytes(n)}/s`
}

export function fmtDuration(seconds) {
  const s = Math.max(Math.round(Number(seconds) || 0), 0)
  if (s < 60) return `${s} 秒`
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`
  return `${Math.floor(s / 3600)} 时 ${Math.floor((s % 3600) / 60)} 分`
}

export function fmtTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  const pad = v => String(v).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export const PHASE_TEXT = {
  idle: '未运行',
  scanning: '扫描本地',
  listing: '列举远端',
  uploading: '上传中',
  finished: '已完成',
  cancelled: '已取消',
  error: '出错',
}

export const PHASE_COLOR = {
  idle: 'grey',
  scanning: 'info',
  listing: 'info',
  uploading: 'primary',
  finished: 'success',
  cancelled: 'warning',
  error: 'error',
}

export const FILE_STATE_TEXT = {
  pending: '排队',
  uploading: '上传中',
  done: '已上传',
  instant: '秒传',
  skipped: '跳过',
  failed: '失败',
  cancelled: '已取消',
}

export const FILE_STATE_COLOR = {
  pending: 'grey',
  uploading: 'primary',
  done: 'success',
  instant: 'cyan-darken-1',
  skipped: 'secondary',
  failed: 'error',
  cancelled: 'warning',
}

export const POLICY_TEXT = {
  skip_same_size: '相同大小跳过',
  overwrite: '覆盖（旧文件进回收站）',
  keep_both: '保留两者（自动改名）',
}
