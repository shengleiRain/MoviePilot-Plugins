import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

// Strip vuetify/mdi selector rules so the plugin never duplicates host styles.
function stripVuetifyCss() {
  return {
    postcssPlugin: 'strip-vuetify-css',
    Rule(rule) {
      if (/^\.v-|^\.mdi-|\.v-|\.mdi-/.test(rule.selector)) {
        rule.remove()
      }
    },
    AtRule(rule) {
      if (rule.name === 'media' || rule.name === 'supports') {
        rule.each(child => {
          if (child.type === 'rule' && /(^|[\s>+~,.])\.?(v-|mdi-)/.test(child.selector || '')) {
            child.remove()
          }
        })
        if (!rule.nodes || rule.nodes.length === 0) rule.remove()
      }
    },
  }
}
stripVuetifyCss.postcss = true

export default defineConfig({
  plugins: [
    vue(),
    federation({
      name: 'p123sync',
      filename: 'remoteEntry.js',
      exposes: {
        './Page': './src/components/Page.vue',
        './Config': './src/components/Config.vue',
        './Dashboard': './src/components/Dashboard.vue',
        './AppPage': './src/components/AppPage.vue',
      },
      shared: {
        vue: { requiredVersion: false, generate: false },
        vuetify: { requiredVersion: false, generate: false, singleton: true },
        'vuetify/styles': { requiredVersion: false, generate: false, singleton: true },
      },
    }),
  ],
  build: {
    target: 'esnext',
    minify: 'terser',
    cssCodeSplit: true,
    outDir: 'dist',
  },
  css: {
    postcss: {
      plugins: [stripVuetifyCss()],
    },
  },
  server: {
    port: 5007,
    cors: true,
  },
})
