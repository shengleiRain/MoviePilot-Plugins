// Dev-only mount for local iteration (vite dev on :5007).
// In production MoviePilot loads the federated remoteEntry.js directly.
import { createApp } from 'vue'
import App from './App.vue'

createApp(App).mount('#app')
