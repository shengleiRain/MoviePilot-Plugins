<template>
  <v-card>
    <v-card-title>123云盘同步</v-card-title>
    <v-card-subtitle>本地目录单向同步到123云盘（免识别整树复制）</v-card-subtitle>
    <v-card-text>
      <p class="text-body-2">
        在左侧栏打开「123云盘同步」页面可查看任务状态与实时进度；插件设置中配置账号、
        同步任务对与定时。同步不经过 MoviePilot 的识别整理链，未识别内容会原样上传。
      </p>
      <p class="text-body-2 text-medium-emphasis">
        同名策略支持：相同大小跳过 / 覆盖（旧文件进123回收站）/ 保留两者（自动改名）。
        秒传命中时零流量完成。
      </p>
    </v-card-text>
  </v-card>
</template>

<script setup>
</script>
