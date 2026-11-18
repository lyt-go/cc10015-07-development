<template>
  <section class="page" data-module="group_snapshot">
    <header class="page-head">
      <div>
        <h2>对照实验分群清单</h2>
        <p class="page-desc">实验启动时按分流比例生成的对照/实验样本组确认结果；分群基数可回查实验方案，生命周期状态变更、归档、恢复与切换全程保序留痕。</p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出分群清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card" :data-testid="`stat-${item.label}`">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>清单编号</span>
        <input v-model="keyword" data-testid="filter-清单编号" placeholder="按清单编号检索" />
      </label>
      <label class="filter-item">
        <span>生命周期状态</span>
        <select v-model="status" data-testid="filter-status">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">
            <button v-if="column === '清单编号'" class="link" type="button" @click="openDetail(row)">{{ row[column] ?? '—' }}</button>
            <span v-else :title="column === '样本总量核对' ? String(row[column] ?? '') : ''">{{ row[column] ?? '—' }}</span>
          </td>
          <td class="row-actions">
            <template v-if="row.status === '有效'">
              <button class="link" type="button" :data-testid="`action-${row.id}-归档`" @click="runAction('归档', row)">归档</button>
            </template>
            <template v-else-if="row.status === '已归档'">
              <button class="link" type="button" :data-testid="`action-${row.id}-恢复`" @click="runAction('恢复', row)">恢复</button>
              <button class="link" type="button" :data-testid="`action-${row.id}-切换`" @click="runAction('切换', row)">切换</button>
            </template>
            <template v-else>
              <span class="error-text">生成中残留，请在实验设计页重新启动补齐</span>
            </template>
            <button class="link" type="button" :data-testid="`detail-${row.id}`" @click="openDetail(row)">详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无分群清单，启动实验后将自动生成确认结果</td>
        </tr>
      </tbody>
    </table>

    <div v-if="detail" class="modal-backdrop" data-testid="detail-modal">
      <div class="modal" style="max-width:860px">
        <h3>分群清单详情 · {{ detail.清单编号 }}</h3>
        <p class="page-desc">分群基数回查：方案 {{ detail.方案编号 }}（版本 v{{ detail.方案版本 }}）· 比例 {{ detail.分流比例快照 }}（比例版本 v{{ detail.分流比例版本 }}）· {{ detail.样本总量核对 }}</p>
        <div v-if="detail.比例冲突" class="error-text" style="margin:6px 0">
          比例版本冲突已处理：{{ detail.冲突处理说明 }}<template v-if="detail.补算差异">；{{ detail.补算差异 }}</template>
        </div>
        <h4 style="margin:10px 0 4px">样本组（{{ detail.groups?.length ?? 0 }}）</h4>
        <table class="data-table">
          <thead><tr><th>分群编号</th><th>组别</th><th>样本量</th></tr></thead>
          <tbody>
            <tr v-for="g in detail.groups" :key="g.group_id">
              <td>{{ g.分群编号 }}</td><td>{{ g.label }}</td><td>{{ g.样本量 }}</td>
            </tr>
          </tbody>
        </table>
        <h4 style="margin:10px 0 4px">生命周期事件（按先后顺序）</h4>
        <ol class="event-list" data-testid="event-list">
          <li v-for="(ev, idx) in detail.events" :key="idx">
            <strong>{{ ev.action }}</strong> · {{ ev.detail }}
          </li>
        </ol>
        <h4 v-if="detail.factor_layers?.length" style="margin:10px 0 4px">因子层占用（V007 互斥）</h4>
        <ul v-if="detail.factor_layers?.length">
          <li v-for="f in detail.factor_layers" :key="f.factor_id">{{ f.因子编号 }} → {{ f.因子层级 }}</li>
        </ul>
        <div class="page-actions" style="margin-top:12px">
          <button class="btn primary" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条分群清单记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/group_snapshot'
const columns = ['清单编号', '实验编号', '分流比例快照', '对照组样本量', '实验组样本量', '样本总量核对', '互斥规则', '状态']
const statuses = ['生成中', '有效', '已归档']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const status = ref('')
const detail = ref<Row | null>(null)

const stats = computed(() => [
  { label: '分群清单总数', value: total.value },
  { label: '有效清单', value: rows.value.filter((r) => r.status === '有效').length },
  { label: '生成中待补齐', value: rows.value.filter((r) => r.status === '生成中').length },
  { label: '已归档', value: rows.value.filter((r) => r.status === '已归档').length },
])

function resetFilters() {
  keyword.value = ''
  status.value = ''
  void reload()
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (status.value) query.set('status', status.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) throw new Error('分群清单列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? 0
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分群清单列表读取失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) throw new Error('分群清单详情读取失败')
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分群清单详情读取失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '生命周期动作未生效')
    await reload()
    if (detail.value && detail.value.id === row.id) await openDetail(payload.entry)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分群清单操作失败'
  }
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (status.value) query.set('status', status.value)
  try {
    const response = await request(`${ENDPOINT}/export?${query.toString()}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'group_snapshot.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

onMounted(reload)
</script>
