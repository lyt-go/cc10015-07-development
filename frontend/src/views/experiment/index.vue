<template>
  <section class="page" data-module="experiment">
    <header class="page-head">
      <div><h2>实验设计</h2><p class="page-desc">维护实验方案的输入、执行约束与结果记录；实验启动在同一事务内落库状态与对照实验分群清单，运行中实验始终有可消费样本。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记实验方案</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
      </div>
    </header>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>
    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row" :data-testid="`timeline-row-${item.id}`">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
        <span class="confirm-tag" :data-testid="`confirm-${item.id}`" :class="confirmClass(item)">{{ item.分群确认 }}</span>
        <span class="row-actions">
          <button class="link" type="button" data-testid="detail-btn" @click.stop="openDetail(item)">分群详情</button>
          <button v-if="item.status !== '运行中'" class="link" type="button" :data-testid="`action-${item.id}-启动实验`" @click.stop="confirmStart(item)">启动实验</button>
          <button v-if="item.status === '运行中'" class="link" type="button" :data-testid="`action-${item.id}-终止实验`" @click.stop="runAction('终止实验', item)">终止实验</button>
        </span>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
      <div v-if="selectedItem.有效清单编号" style="margin-top:4px;">有效清单：{{ selectedItem.有效清单编号 }} · 比例 {{ selectedItem.分流比例快照 }} · {{ selectedItem.样本总量核对 }}</div>
    </div>

    <!-- 启动确认：比例冲突时必须选择策略 -->
    <div v-if="startTarget" class="modal-backdrop" data-testid="start-modal">
      <form class="modal" @submit.prevent="doStart">
        <h3>启动实验 · {{ startTarget.name }}</h3>
        <p class="page-desc">实验状态与对照实验分群清单将在同一事务同时落库，失败整体回滚。</p>
        <div v-if="startTarget.待生效分流比例" class="ratio-conflict" data-testid="ratio-conflict">
          <p class="error-text">检测到分流比例调整与启动同时生效：当前比例 {{ startTarget.分流比例快照 || currentRatio(startTarget) }}，待生效 {{ startTarget.待生效分流比例 }}，请选择版本冲突处理策略：</p>
          <label class="create-field"><input type="radio" value="freeze_old" v-model="ratioPolicy" /> 先冻结旧比例再启动（清单用旧比例，新比例标记待下一批）</label>
          <label class="create-field"><input type="radio" value="apply_new" v-model="ratioPolicy" /> 先应用新比例再生成分群（清单用新比例，从旧方案补算差异）</label>
        </div>
        <div v-else><p>将按当前实验方案的分流比例生成分群清单。</p></div>
        <p v-if="startMessage" class="error-text" data-testid="start-message">{{ startMessage }}</p>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="cancelStart">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-start">确认启动</button>
        </div>
      </form>
    </div>

    <!-- 分群确认详情：列表页与详情页读同一份确认结果 -->
    <div v-if="detail" class="modal-backdrop" data-testid="detail-modal">
      <div class="modal" style="max-width:900px">
        <h3>分群确认详情 · {{ detail.实验名称 }}</h3>
        <p class="page-desc">方案 {{ detail.方案编号 }}（v{{ detail.方案版本 }}）· 状态 {{ detail.status }}</p>
        <div class="stat-row">
          <article class="stat-card"><span class="stat-label">分群确认</span><strong class="stat-value">{{ detail.分群确认结果.分群确认 }}</strong></article>
          <article class="stat-card"><span class="stat-label">有效清单</span><strong class="stat-value" style="font-size:14px">{{ detail.分群确认结果.有效清单编号 || '—' }}</strong></article>
          <article class="stat-card"><span class="stat-label">比例快照</span><strong class="stat-value">{{ detail.分群确认结果.分流比例快照 || '—' }}</strong></article>
          <article class="stat-card"><span class="stat-label">历史清单数</span><strong class="stat-value">{{ detail.分群确认结果.历史清单数 }}</strong></article>
        </div>
        <p v-if="detail.分群确认结果.比例冲突" class="error-text">比例冲突处理：{{ detail.分群确认结果.冲突处理说明 }}</p>
        <h4 style="margin:10px 0 4px">对照实验分群清单（生命周期先后顺序）</h4>
        <table class="data-table">
          <thead><tr><th>清单编号</th><th>比例快照</th><th>对照组</th><th>实验组</th><th>总量核对</th><th>状态</th><th>事件轨迹</th></tr></thead>
          <tbody>
            <tr v-for="snap in detail.分群清单" :key="snap.id" :data-testid="`snap-row-${snap.id}`" :data-status="snap.status">
              <td>{{ snap.清单编号 }}</td>
              <td>{{ snap.分流比例快照 }}<br /><span style="font-size:11px;color:var(--muted)">v{{ snap.分流比例版本 }}</span></td>
              <td>{{ snap.对照组样本量 }}</td>
              <td>{{ snap.实验组样本量 }}</td>
              <td style="font-size:12px">{{ snap.样本总量核对 }}</td>
              <td>{{ snap.status }}</td>
              <td style="font-size:12px">
                <span v-for="(ev, i) in snap.events" :key="i">{{ ev.action }}<template v-if="i < snap.events.length - 1"> → </template></span>
              </td>
            </tr>
            <tr v-if="!detail.分群清单.length"><td colspan="7" class="empty-state">尚未生成分群清单，请启动实验</td></tr>
          </tbody>
        </table>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="adjustRatioOpen = !adjustRatioOpen">调整分流比例</button>
          <button class="btn primary" type="button" @click="detail = null">关闭</button>
        </div>
        <form v-if="adjustRatioOpen" class="filter-bar" style="margin-top:10px" @submit.prevent="submitRatio">
          <label><span>新分流比例（对照:实验）</span><input v-model="newRatio" placeholder="如 8:2" data-testid="ratio-input" required /></label>
          <button class="btn primary" type="submit" data-testid="ratio-submit">登记待生效</button>
          <span v-if="ratioMessage" class="error-text">{{ ratioMessage }}</span>
        </form>
      </div>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记实验方案</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <label class="create-field"><span>分流比例</span><input v-model="createValues['分流比例']" placeholder="如 5:5" /></label>
        <label class="create-field"><span>样本总量</span><input v-model="createValues['样本总量']" placeholder="如 10000" /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
    <p v-if="errorMessage" class="error-text" style="padding:0 16px">{{ errorMessage }}</p>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/experiment'
const columns = ["方案编号", "实验名称", "对照组说明", "实验组说明", "样本总量", "分流比例", "启动日期", "预期时长"]
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])
const startTarget = ref<any>(null)
const ratioPolicy = ref<'freeze_old' | 'apply_new'>('freeze_old')
const startMessage = ref('')
const detail = ref<any>(null)
const adjustRatioOpen = ref(false)
const newRatio = ref('')
const ratioMessage = ref('')
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
function confirmClass(item: any) {
  if (item.分群确认 === '已确认') return 'tag-ok'
  if (item.分群确认 === '待补齐') return 'tag-warn'
  return 'tag-muted'
}
function currentRatio(item: any) { return item.分流比例快照 || '5:5' }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); allItems.value = data.items || []; applyFilter() } catch(e) { errorMessage.value = '加载失败' }
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function confirmStart(item: any) {
  startTarget.value = item
  startMessage.value = ''
  ratioPolicy.value = 'freeze_old'
}
function cancelStart() { startTarget.value = null }
async function doStart() {
  if (!startTarget.value) return
  startMessage.value = ''
  const body: Record<string, any> = { action: '启动实验' }
  if (startTarget.value.待生效分流比例) body.options = { ratio_policy: ratioPolicy.value }
  try {
    const res = await request(`${ENDPOINT}/${startTarget.value.id}/actions`, { method: 'POST', body: JSON.stringify(body) })
    const payload = await res.json()
    if (!res.ok || !payload.ok) { startMessage.value = payload.message || '启动失败'; return }
    startTarget.value = null
    await reload()
  } catch (e: any) { startMessage.value = e.message }
}
async function runAction(action: string, item: any) {
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/${item.id}/actions`, { method: 'POST', body: JSON.stringify({ action }) })
    const payload = await res.json()
    if (!res.ok || !payload.ok) { errorMessage.value = payload.message || '动作未生效'; return }
    await reload()
    if (detail.value && detail.value.id === item.id) await openDetail(item)
  } catch (e: any) { errorMessage.value = e.message }
}
async function openDetail(item: any) {
  try {
    const res = await request(`${ENDPOINT}/${item.id}`)
    if (!res.ok) throw new Error('详情读取失败')
    detail.value = await res.json()
    adjustRatioOpen.value = false
    ratioMessage.value = ''
  } catch (e: any) { errorMessage.value = e.message }
}
async function submitRatio() {
  if (!detail.value) return
  ratioMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/${detail.value.id}/ratio`, { method: 'POST', body: JSON.stringify({ values: { 分流比例: newRatio.value } }) })
    const payload = await res.json()
    if (!res.ok || !payload.ok) { ratioMessage.value = payload.message || '比例调整失败'; return }
    ratioMessage.value = payload.message
    newRatio.value = ''
    await reload()
    await openDetail({ id: detail.value.id })
  } catch (e: any) { ratioMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>
<style scoped>
.confirm-tag { font-size: 12px; padding: 2px 8px; border-radius: 10px; margin-left: 8px; white-space: nowrap; }
.tag-ok { background: rgba(46, 160, 67, 0.15); color: #2ea043; }
.tag-warn { background: rgba(210, 153, 34, 0.18); color: #d29922; }
.tag-muted { background: rgba(139, 148, 158, 0.18); color: var(--muted); }
.ratio-conflict { border: 1px solid rgba(210, 153, 34, 0.5); border-radius: 8px; padding: 10px; margin: 8px 0; }
</style>
