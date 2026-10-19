<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'

import './style.css'

const props = defineProps({
  plugin: {
    type: Object,
    required: true,
  },
  requestBackend: {
    type: Function,
    required: true,
  },
})

const colmonStatus = ref(null)
const colmonStatusError = ref('')
const refreshIntervalInput = ref(60)
const refreshIntervalDirty = ref(false)
const refreshIntervalSaving = ref(false)
const refreshIntervalMessage = ref('')
const refreshIntervalMessageTone = ref('success')
let colmonStatusTimer = null

async function loadColmonStatus() {
  try {
    const nextStatus = await props.requestBackend('/api/plugins/colmon/status')
    colmonStatus.value = nextStatus
    if (!refreshIntervalDirty.value && !refreshIntervalSaving.value) {
      refreshIntervalInput.value = nextStatus.refresh_interval_seconds
    }
    colmonStatusError.value = ''
  } catch (error) {
    colmonStatusError.value = error instanceof Error ? error.message : String(error)
  }
}

function markRefreshIntervalDirty() {
  refreshIntervalDirty.value = true
  refreshIntervalMessage.value = ''
}

async function saveRefreshInterval() {
  refreshIntervalSaving.value = true
  refreshIntervalMessage.value = ''
  try {
    const nextStatus = await props.requestBackend('/api/plugins/colmon/settings', {
      method: 'POST',
      body: JSON.stringify({
        refresh_interval_seconds: refreshIntervalInput.value,
      }),
    })
    colmonStatus.value = nextStatus
    refreshIntervalInput.value = nextStatus.refresh_interval_seconds
    refreshIntervalDirty.value = false
    refreshIntervalMessageTone.value = 'success'
    refreshIntervalMessage.value = '刷新频率已经保存'
  } catch (error) {
    refreshIntervalMessageTone.value = 'error'
    refreshIntervalMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    refreshIntervalSaving.value = false
  }
}

function runtimeLabel(status) {
  return {
    running: '运行中',
    stopped: '已停止',
    error: '运行错误',
  }[status] ?? '等待中'
}

function windowStatusLabel(status) {
  return {
    ready: '已更新',
    stale: '数据过期',
    waiting: '等待数据',
    stopped: '已停止',
    error: '读取错误',
  }[status] ?? '等待中'
}

function formatUpdatedAt(value) {
  if (!value) return '尚未写入'
  return value.replace('T', ' ').replace(/([+-]\d\d:\d\d|Z)$/, '')
}

onMounted(async () => {
  await loadColmonStatus()
  colmonStatusTimer = window.setInterval(loadColmonStatus, 2000)
})

onBeforeUnmount(() => {
  if (colmonStatusTimer !== null) {
    window.clearInterval(colmonStatusTimer)
  }
})
</script>

<template>
  <div class="colmon-content">
    <div class="colmon-overview">
      <div>
        <p class="eyebrow">TASKBAR MONITOR</p>
        <h3>三个任务栏窗口</h3>
        <p>Colpilot 直接管理三个任务栏窗口，插件运行在 Colpilot 后端中。</p>
      </div>
      <span
        class="colmon-runtime-pill"
        :class="`is-${colmonStatus?.runtime?.status ?? 'stopped'}`"
      >
        {{ runtimeLabel(colmonStatus?.runtime?.status) }}
      </span>
    </div>

    <div v-if="colmonStatusError" class="colmon-message is-error">
      {{ colmonStatusError }}
    </div>

    <div v-if="colmonStatus" class="colmon-window-grid">
      <article v-for="window in colmonStatus.windows" :key="window.id" class="colmon-window-card">
        <div class="colmon-window-heading">
          <span>{{ window.title }}</span>
          <strong :class="`is-${window.status}`">{{ windowStatusLabel(window.status) }}</strong>
        </div>
        <div class="colmon-window-value">{{ window.value }}</div>
        <p>{{ window.detail }}</p>
        <small>
          更新于 {{ formatUpdatedAt(window.updated_at) }} · {{ window.refresh_interval_seconds }} 秒
        </small>
      </article>
    </div>

    <form class="colmon-settings" @submit.prevent="saveRefreshInterval">
      <div>
        <label for="colmon-refresh-interval">信息更新频率</label>
        <span>三个任务栏窗口使用相同的更新间隔</span>
      </div>
      <div class="colmon-settings-control">
        <input
          id="colmon-refresh-interval"
          v-model.number="refreshIntervalInput"
          type="number"
          :min="colmonStatus?.minimum_refresh_interval_seconds ?? 10"
          :max="colmonStatus?.maximum_refresh_interval_seconds ?? 3600"
          step="10"
          @input="markRefreshIntervalDirty"
        />
        <span>秒</span>
        <button type="submit" :disabled="refreshIntervalSaving || !refreshIntervalDirty">
          {{ refreshIntervalSaving ? '保存中' : '保存' }}
        </button>
      </div>
    </form>

    <div
      v-if="refreshIntervalMessage"
      class="colmon-message"
      :class="`is-${refreshIntervalMessageTone}`"
    >
      {{ refreshIntervalMessage }}
    </div>

    <div v-if="colmonStatus" class="colmon-footnote">
      colmon 插件管理 {{ colmonStatus.window_count }} 个任务栏窗口，插件进程和窗口由 Colpilot 统一管理。
    </div>
  </div>
</template>
