import { expect, test, type APIRequestContext, type BrowserContext, type Page } from '@playwright/test'

const password = 'correct-horse-battery-staple'
const unique = `e2e-${Date.now()}`

async function csrf(context: BrowserContext) {
  const response = await context.request.get('/api/v1/auth/csrf/')
  expect(response.ok()).toBeTruthy()
  const cookie = (await context.cookies()).find(item => item.name === 'csrftoken')
  expect(cookie?.value).toBeTruthy()
  return decodeURIComponent(cookie!.value)
}

async function json<T>(request: APIRequestContext, path: string, method = 'GET', data?: unknown, csrfToken?: string) {
  const response = await request.fetch(path, {
    method,
    data,
    headers: csrfToken ? { 'X-CSRFToken': csrfToken } : undefined,
  })
  const body = await response.json().catch(() => undefined)
  expect(response.ok(), `${method} ${path}: ${JSON.stringify(body)}`).toBeTruthy()
  return body as T
}

async function waitForWorkbench(page: Page) {
  await expect(page.locator('.app-shell')).toBeVisible()
}

async function waitForCanvas(page: Page) {
  await expect(page.locator('.canvas-stage')).toBeVisible()
  await expect(page.locator('.canvas-status')).toContainText(/实时协作已连接|只读协作|实时服务不可用/)
}

async function bootstrapOwner(page: Page, runId = unique, checkDialogs = false) {
  await page.goto('/')
  const setup = await page.request.get('/api/v1/auth/bootstrap/')
  expect(setup.ok()).toBeTruthy()
  const { owner_exists: ownerExists } = await setup.json() as { owner_exists: boolean }
  if (ownerExists) {
    // The Playwright web server owns one isolated database for the whole file,
    // so later tests reuse the deterministic owner created by the first test.
    await expect(page.getByRole('heading', { name: '登录工作台' })).toBeVisible()
    await page.getByLabel('用户名').fill(`${unique}-owner`)
    await page.getByLabel('密码').fill(password)
    await page.getByRole('button', { name: '登录' }).click()
  } else {
    await expect(page.getByRole('heading', { name: '创建首个 Owner' })).toBeVisible()
    await page.getByLabel('用户名').fill(`${runId}-owner`)
    await page.getByLabel('邮箱').fill(`${runId}-owner@example.test`)
    await page.getByLabel('密码').fill(password)
    await page.getByRole('button', { name: '创建并进入' }).click()
  }
  await waitForWorkbench(page)

  const welcomeCreate = page.getByRole('button', { name: '创建第一个世界观 →', exact: true })
  if (await welcomeCreate.isVisible()) await welcomeCreate.click()
  else await page.getByRole('button', { name: /新建世界观/ }).click()
  const createDialog = page.getByRole('dialog')
  await expect(createDialog).toBeVisible()
  await expect(createDialog.getByLabel('世界观名称')).toBeFocused()
  await expect(createDialog.getByRole('button', { name: '创建世界观 →', exact: true })).toBeDisabled()
  await createDialog.getByLabel('世界观名称').fill('   ')
  await expect(createDialog.getByRole('button', { name: '创建世界观 →', exact: true })).toBeDisabled()
  await createDialog.getByLabel('世界观名称').press('Escape')
  await expect(createDialog).not.toBeVisible()
  await page.getByRole('button', { name: /新建世界观/ }).click()
  await expect(createDialog.getByLabel('世界观名称')).toHaveValue('')
  await createDialog.getByLabel('世界观名称').fill(`${runId} 世界`)
  // A failed request stays in the application dialog and preserves the name.
  await page.route('**/api/v1/workspaces/', async route => {
    if (route.request().method() === 'POST') await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '暂时无法创建' }) })
    else await route.continue()
  })
  await createDialog.getByLabel('世界观名称').press('Enter')
  await expect(createDialog.getByRole('alert')).toContainText('暂时无法创建')
  await expect(createDialog.getByLabel('世界观名称')).toHaveValue(`${runId} 世界`)
  await page.unroute('**/api/v1/workspaces/')
  await createDialog.getByRole('button', { name: '创建世界观 →', exact: true }).click()
  await expect(createDialog).not.toBeVisible()
  await expect(page.locator('.workspace-picker select option').filter({ hasText: `${runId} 世界` })).toHaveCount(1)
  await expect(page.locator('.canvas-list button')).toHaveCount(1)
  await waitForCanvas(page)
  if (checkDialogs) {
    // Every application action must stay inside styled dialogs.
    page.on('dialog', dialog => { throw new Error(`Unexpected browser dialog: ${dialog.type()}`) })
    await page.getByRole('button', { name: '命名', exact: true }).click()
    const rename = page.getByRole('dialog', { name: '命名画布' })
    await expect(rename.getByLabel('画布名称')).toBeFocused()
    await rename.getByLabel('画布名称').fill('   ')
    await expect(rename.getByRole('button', { name: '保存名称' })).toBeDisabled()
    await rename.getByLabel('画布名称').fill(`${runId} 画布`)
    await rename.getByLabel('画布名称').press('Enter')
    await expect(rename).not.toBeVisible()
    await expect(page.locator('.canvas-list button')).toContainText(`${runId} 画布`)
    await waitForCanvas(page)
    const originalBranch = await page.locator('.branch-controls select').inputValue()
    await page.getByRole('button', { name: '＋ 分支', exact: true }).click()
    const branchDialog = page.getByRole('dialog', { name: '创建工作分支' })
    await branchDialog.getByLabel('分支名称').fill(`${runId}-draft`)
    await branchDialog.getByRole('button', { name: '创建分支', exact: true }).click()
    await expect(page.locator('.branch-controls select option:checked')).toHaveText(`${runId}-draft`)
    await page.getByRole('button', { name: '归档分支', exact: true }).click()
    const archive = page.getByRole('dialog', { name: '归档工作分支' })
    await expect(archive).toContainText(`${runId}-draft`)
    await archive.getByRole('button', { name: '取消', exact: true }).click()
    await expect(page.getByRole('button', { name: '归档分支', exact: true })).toBeVisible()
    await page.getByRole('button', { name: '归档分支', exact: true }).click()
    await page.getByRole('dialog').getByRole('button', { name: '归档分支', exact: true }).click()
    await expect(page.locator('.branch-controls select')).toHaveValue('main')
    await page.locator('.branch-controls select').selectOption(originalBranch)
    await expect(page.locator('.branch-controls select')).toHaveValue(originalBranch)
  }
  const workspaceId = await page.locator('.workspace-picker select').inputValue()
  const canvases = await json<Array<{ id: string; name: string }>>(page.request, `/api/v1/canvases/?workspace=${workspaceId}`)
  expect(canvases).toHaveLength(1)
  return {
    workspaceId,
    canvasId: canvases[0].id,
    canvasName: await page.locator('.canvas-list button').first().innerText(),
  }
}

async function inviteAndSignup(owner: Page, workspaceId: string, role: 'editor' | 'reader', username: string, email: string) {
  const ownerCsrf = await csrf(owner.context())
  const invite = await json<{ token: string }>(owner.request, '/api/v1/members/', 'POST', {
    workspace: workspaceId,
    email,
    role,
  }, ownerCsrf)
  const context = await owner.context().browser()!.newContext({ baseURL: 'http://127.0.0.1:5175' })
  const tokenCsrf = await csrf(context)
  await json(context.request, '/api/v1/invites/signup/', 'POST', {
    token: invite.token,
    username,
    email,
    password,
  }, tokenCsrf)
  return context
}

test.describe('authenticated multi-browser collaboration', () => {
  test('official sync shares edits, enforces reader access and recovers after restart', async ({ browser }) => {
    test.setTimeout(90_000)
    const ownerContext = await browser.newContext({ baseURL: 'http://127.0.0.1:5175' })
    const owner = await ownerContext.newPage()
    const { workspaceId, canvasId } = await bootstrapOwner(owner)

    const editorContext = await inviteAndSignup(owner, workspaceId, 'editor', `${unique}-editor`, `${unique}-editor@example.test`)
    const readerContext = await inviteAndSignup(owner, workspaceId, 'reader', `${unique}-reader`, `${unique}-reader@example.test`)
    const editor = await editorContext.newPage()
    const reader = await readerContext.newPage()

    await Promise.all([editor.goto('/'), reader.goto('/')])
    await Promise.all([waitForWorkbench(editor), waitForWorkbench(reader)])
    await Promise.all([waitForCanvas(editor), waitForCanvas(reader)])

    // The role is granted by the signed ticket, not by a frontend-only flag.
    await expect(editor.getByRole('button', { name: /Markdown/ })).toBeEnabled()
    await expect(reader.getByRole('button', { name: /Markdown/ })).toBeDisabled()
    await expect(reader.locator('.canvas-status')).toContainText(/只读/)

    // REST writes are protected by the backend role check, not only by disabled
    // buttons. A reader session must not be able to mutate the canvas.
    const readerCsrf = await csrf(readerContext)
    const forbiddenWrite = await readerContext.request.fetch(`/api/v1/canvases/${canvasId}/`, {
      method: 'PATCH',
      data: { name: 'reader must not rename this canvas', expected_version: 0 },
      headers: { 'X-CSRFToken': readerCsrf },
    })
    expect(forbiddenWrite.status()).toBe(403)

    // An editor can create a draft through the real TLDraw adapter; the reader
    // cannot even open the editor action.
    await editor.getByRole('button', { name: /Markdown/ }).click()
    await expect(editor.getByRole('heading', { name: '编辑 markdown' })).toBeVisible()
    await editor.getByLabel('卡片内容').fill('来自 editor 的协作草稿')
    await editor.getByRole('button', { name: '放入画布' }).click()
    await expect(editor.locator('.canvas-status')).toContainText(/实时协作已连接|待发送|已同步/)
    // Verify the actual rendered TLDraw shape crosses the WebSocket boundary,
    // rather than only asserting that the editor queued an operation.
    await expect(editor.locator('.oc-card')).toHaveCount(1, { timeout: 10_000 })
    await expect(editor.locator('.oc-card .card-body')).toContainText('来自 editor 的协作草稿', { timeout: 10_000 })
    await expect(reader.locator('.oc-card')).toHaveCount(1, { timeout: 15_000 })
    await expect(reader.locator('.oc-card .card-body')).toContainText('来自 editor 的协作草稿', { timeout: 15_000 })

    // Queue a real canvas edit while the browser is offline. The operation
    // must survive reconnect and be drained only after durable acknowledgement.
    await ownerContext.setOffline(true)
    await expect(owner.locator('.canvas-status')).toContainText(/离线|待重连/, { timeout: 10_000 })
    await owner.getByRole('button', { name: /Markdown/ }).click()
    await expect(owner.getByRole('heading', { name: '编辑 markdown' })).toBeVisible()
    await owner.getByLabel('卡片内容').fill('断网后排队的协作草稿')
    await owner.getByRole('button', { name: '放入画布' }).click()
    await expect(owner.locator('.canvas-status')).toContainText(/离线|待重连/, { timeout: 10_000 })
    await ownerContext.setOffline(false)
    await expect(owner.locator('.canvas-status')).toContainText(/实时协作已连接/, { timeout: 15_000 })
    await expect(owner.locator('.canvas-status')).not.toContainText(/待发送/, { timeout: 15_000 })

    await expect(reader.locator('.oc-card')).toHaveCount(2, { timeout: 15_000 })
    // Wait for PostgreSQL durability before restarting the service.
    await expect.poll(async () => {
      const canvas = await json<any>(owner.request, `/api/v1/canvases/${canvasId}/`)
      return Object.values(canvas.snapshot?.document?.store || {}).filter((record: any) => record.typeName === 'shape' && record.type === 'oc-card').length
    }).toBe(2)
    // Restart the actual sync-service process. The browser must reconnect to
    // a fresh room instance and reconstruct the persisted canvas snapshot.
    const restart = await owner.request.post('http://127.0.0.1:8789/__test/restart')
    expect(restart.ok()).toBeTruthy()
    await reader.reload({ waitUntil: 'domcontentloaded' })
    await waitForWorkbench(reader)
    await waitForCanvas(reader)
    await expect(reader.locator('.oc-card')).toHaveCount(2, { timeout: 20_000 })
    await expect(reader.locator('.oc-card').filter({ hasText: '断网后排队的协作草稿' })).toHaveCount(1, { timeout: 20_000 })

    await owner.reload({ waitUntil: 'domcontentloaded' })
    await waitForWorkbench(owner)
    await waitForCanvas(owner)
    await expect(owner.locator('.canvas-status')).toContainText(/实时协作已连接/, { timeout: 15_000 })

    await Promise.all([ownerContext.close(), editorContext.close(), readerContext.close()])
  })

})


test('styled input and confirmation dialogs support keyboard and cancellation', async ({ page }) => {
  await bootstrapOwner(page, `${unique}-dialogs`, true)
  await page.getByRole('button', { name: '◷ 时间线', exact: true }).click()
  await page.getByRole('button', { name: '＋ 时间体系', exact: true }).click()
  const system = page.getByRole('dialog', { name: '创建时间体系' })
  await expect(system.getByLabel('时间体系名称')).toBeFocused()
  await expect(system.getByLabel('最小计量单位')).toHaveValue('日')
  await system.getByLabel('时间体系名称').fill('王历')
  await system.getByLabel('最小计量单位').fill('   ')
  await expect(system.getByRole('button', { name: '创建体系' })).toBeDisabled()
  await system.getByLabel('最小计量单位').fill('日')
  await system.getByRole('button', { name: '取消', exact: true }).click()
  await expect(system).not.toBeVisible()
})
