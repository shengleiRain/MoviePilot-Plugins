<template>
  <v-card variant="flat"><v-card-text>
    <div class="d-flex align-center ga-3 mb-4"><v-avatar color="primary" variant="tonal" rounded="lg"><v-icon icon="mdi-cloud-sync-outline" /></v-avatar><div><div class="text-h6">123云盘同步</div><div class="text-body-2 text-medium-emphasis">本地目录 → 123云盘</div></div></div>
    <p class="text-body-2 mb-3">按目录原样同步文件，无需经过识别和整理。先在插件设置中添加任务，再通过侧栏页面查看进度或手动同步。</p>
    <v-row dense>
      <v-col cols="12" sm="4"><v-card variant="tonal" class="pa-3"><div class="text-subtitle-2">选择目录</div><div class="text-caption mt-1">浏览已有文件夹，也可手动输入</div></v-card></v-col>
      <v-col cols="12" sm="4"><v-card variant="tonal" class="pa-3"><div class="text-subtitle-2">灵活同步</div><div class="text-caption mt-1">手动执行或设置定时计划</div></v-card></v-col>
      <v-col cols="12" sm="4"><v-card variant="tonal" class="pa-3"><div class="text-subtitle-2">查看进度</div><div class="text-caption mt-1">追踪任务、文件和历史记录</div></v-card></v-col>
    </v-row>
    <div class="text-caption text-medium-emphasis mt-4">同名文件可选择跳过、覆盖或保留两者；覆盖时旧文件会移入 123 云盘回收站。</div>
  </v-card-text></v-card>
</template>
