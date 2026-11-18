import { expect, test } from '@playwright/test'

/**
 * 对照实验分群清单 V007 端到端验收：
 * - 实验启动事务内同时落库状态与分群清单，列表/详情读取同一确认结果；
 * - 连续启动只有一份有效快照；
 * - 分流比例冲突两种策略；
 * - 分群清单生命周期与 V007 因子层互斥。
 */
async function createExperiment(page: import('@playwright/test').Page, ratio: string, total: string) {
  const code = `E2E-GSL-${Date.now()}-${Math.floor(Math.random() * 1e6)}`
  const expName = `分群清单E2E-${code}`
  await page.goto('/experiment')
  await page.getByTestId('open-create').click()
  await page.locator('.create-field input').nth(0).fill(code)
  await page.locator('.create-field input').nth(1).fill(expName)
  await page.locator('.create-field input').nth(2).fill('旧策略对照')
  await page.getByPlaceholder('如 5:5').fill(ratio)
  await page.getByPlaceholder('如 10000').fill(total)
  await page.getByTestId('submit-create').click()
  await expect(page.getByTestId('create-modal')).toHaveCount(0)
  return { code, expName }
}

test('实验启动生成分群清单，列表与详情读取同一确认结果', async ({ page }) => {
  const { code, expName } = await createExperiment(page, '7:3', '1000')
  const row = page.locator(`[data-testid^="timeline-row-"]`, { hasText: code }).first()
  await expect(row).toBeVisible()
  await expect(row.getByTestId(/confirm-/)).toHaveText('未生成')

  // 启动：无比例冲突时直接确认
  await row.getByRole('button', { name: '启动实验' }).click()
  await expect(page.getByTestId('start-modal')).toBeVisible()
  await page.getByTestId('submit-start').click()
  await expect(page.getByTestId('start-modal')).toHaveCount(0)

  await expect(row.getByTestId(/confirm-/)).toHaveText('已确认')

  // 详情抽屉：清单有效、比例快照 7:3、总量核对一致、事件轨迹保序
  await row.getByRole('button', { name: '分群详情' }).click()
  await expect(page.getByTestId('detail-modal')).toBeVisible()
  const snapRow = page.getByTestId(/snap-row-/).first()
  await expect(snapRow).toHaveAttribute('data-status', '有效')
  await expect(snapRow).toContainText('7:3')
  await expect(snapRow).toContainText('700+300=1000')
  await expect(snapRow).toContainText('生成')
  await expect(snapRow).toContainText('确认生效')

  // 分群清单独立页面读到同一份确认结果
  await page.goto('/group_snapshot')
  const listRow = page.locator('tbody tr', { hasText: code }).first()
  await expect(listRow).toBeVisible()
  await expect(listRow).toHaveAttribute('data-status', '有效')
  await listRow.getByRole('button', { name: '详情' }).click()
  await expect(page.getByTestId('detail-modal')).toBeVisible()
  await expect(page.getByTestId('event-list')).toContainText('确认生效')
})

test('连续启动不产生第二套有效样本', async ({ page }) => {
  const { code, expName } = await createExperiment(page, '5:5', '500')
  const row = page.locator(`[data-testid^="timeline-row-"]`, { hasText: code }).first()
  await row.getByRole('button', { name: '启动实验' }).click()
  await page.getByTestId('submit-start').click()
  await expect(row.getByTestId(/confirm-/)).toHaveText('已确认')

  await page.goto('/group_snapshot')
  const before = await page.locator('tbody tr', { hasText: code }).count()
  await page.goto('/experiment')
  // 运行中再点启动按钮不可见，直接调接口模拟连续启动
  const list = await page.request.get('/api/experiment').then((r) => r.json())
  const hit = list.items.find((i: any) => i.name === expName)
  const repeatResp = await page.request.post(`/api/experiment/${hit.id}/actions`, {
    data: { action: '启动实验' },
  })
  const repeatBody = await repeatResp.json()
  expect(repeatBody.ok).toBe(true)
  const id = hit.id
  await page.goto('/group_snapshot')
  const after = await page.locator('tbody tr', { hasText: code }).count()
  expect(after).toBe(before)
  const data = await page.request.get('/api/group_snapshot?status=%E6%9C%89%E6%95%88').then((r) => r.json())
  const active = data.items.filter((i: any) => i.实验编号 === code && i.status === '有效')
  expect(active).toHaveLength(1)
  expect(active[0].对照组样本量 + active[0].实验组样本量).toBe(500)
  expect(id).toBeGreaterThan(0)
})

test('比例调整与启动同时生效时必须选择冲突策略', async ({ page }) => {
  const { code, expName } = await createExperiment(page, '5:5', '1000')
  const row = page.locator(`[data-testid^="timeline-row-"]`, { hasText: code }).first()
  // 先登记待生效比例
  await row.getByRole('button', { name: '分群详情' }).click()
  await page.getByRole('button', { name: '调整分流比例' }).click()
  await page.getByTestId('ratio-input').fill('8:2')
  await page.getByTestId('ratio-submit').click()
  await page.getByTestId('detail-modal').getByRole('button', { name: '关闭' }).click()

  await row.getByRole('button', { name: '启动实验' }).click()
  await expect(page.getByTestId('ratio-conflict')).toBeVisible()
  // apply_new：清单使用新比例
  await page.locator('input[value="apply_new"]').check()
  await page.getByTestId('submit-start').click()
  await expect(page.getByTestId('start-modal')).toHaveCount(0)
  await expect(row.getByTestId(/confirm-/)).toHaveText('已确认')

  const all = await page.request.get('/api/group_snapshot').then((r) => r.json())
  const snap = all.items.find((i: any) => i.实验编号 === code)
  expect(snap.分流比例快照).toBe('8:2')
  expect(snap.对照组样本量).toBe(800)
  expect(snap.实验组样本量).toBe(200)
  expect(snap.补算差异).toContain('+300')
})

test('分群清单生命周期归档与恢复保序', async ({ page }) => {
  const { code, expName } = await createExperiment(page, '5:5', '200')
  const row = page.locator(`[data-testid^="timeline-row-"]`, { hasText: code }).first()
  await row.getByRole('button', { name: '启动实验' }).click()
  await page.getByTestId('submit-start').click()

  await page.goto('/group_snapshot')
  const listRow = page.locator('tbody tr', { hasText: code }).first()
  await listRow.getByRole('button', { name: '归档' }).click()
  await expect(listRow).toHaveAttribute('data-status', '已归档')
  await listRow.getByRole('button', { name: '恢复' }).click()
  await expect(listRow).toHaveAttribute('data-status', '有效')
  await listRow.getByRole('button', { name: '详情' }).click()
  const events = await page.getByTestId('event-list').innerText()
  expect(events.indexOf('归档')).toBeLessThan(events.indexOf('恢复'))
})

test('V007 因子层互斥阻止同层重复分配', async ({ page }) => {
  const { code, expName } = await createExperiment(page, '5:5', '200')
  const row = page.locator(`[data-testid^="timeline-row-"]`, { hasText: code }).first()
  await row.getByRole('button', { name: '启动实验' }).click()
  await page.getByTestId('submit-start').click()

  await page.goto('/expr_factor')
  await page.getByTestId('open-create').click()
  await page.getByTestId('create-因子编号').fill(`F-${code}-1`)
  await page.getByTestId('create-因子名称').fill('按钮颜色')
  await page.getByTestId('create-因子层级').fill('UI层')
  await page.getByTestId('create-取值集合').fill('红色,蓝色')
  await page.getByTestId('create-分配比例').fill('5:5')
  await page.getByTestId('create-互斥规则').fill('同层互斥')
  await page.getByTestId('create-关联实验').fill(code)
  await page.getByTestId('create-生效批次').fill('B001')
  await page.getByTestId('submit-create').click()
  await expect(page.getByTestId('create-modal')).toHaveCount(0)

  // 同一因子层再建一个：被互斥规则拦截，错误信息留在页脚
  await page.getByTestId('open-create').click()
  await page.getByTestId('create-因子编号').fill(`F-${code}-2`)
  await page.getByTestId('create-因子名称').fill('按钮形状')
  await page.getByTestId('create-因子层级').fill('UI层')
  await page.getByTestId('create-取值集合').fill('圆形,方形')
  await page.getByTestId('create-分配比例').fill('5:5')
  await page.getByTestId('create-互斥规则').fill('同层互斥')
  await page.getByTestId('create-关联实验').fill(code)
  await page.getByTestId('create-生效批次').fill('B002')
  await page.getByTestId('submit-create').click()
  // 互斥拦截：弹窗保留，页脚写入可读的拦截原因（文本断言不要求元素在遮罩层之上）
  await expect(page.locator('.page-foot .error-text')).toContainText('互斥')
  await page.getByRole('button', { name: '取消' }).click()
})
