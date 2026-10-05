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

async function bootstrapOwner(page: Page, runId = unique) {
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

  page.once('dialog', dialog => dialog.accept(`${runId} 世界`))
  await page.getByRole('button', { name: /新建世界观/ }).click()
  await expect(page.locator('.workspace-picker select option').filter({ hasText: `${runId} 世界` })).toHaveCount(1)
  await expect(page.locator('.canvas-list button')).toHaveCount(1)
  await waitForCanvas(page)
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
  test('owner invites editor and reader; editor syncs presence while reader stays read-only', async ({ browser }) => {
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

    // Both browser sessions are in the same room. Presence is rendered by
    // the real websocket service and is independent of the REST session.
    await expect(owner.locator('.canvas-status')).toContainText(/3 位协作者/, { timeout: 15_000 })
    await expect(editor.locator('.canvas-status')).toContainText(/3 位协作者/, { timeout: 15_000 })

    // REST writes are protected by the backend role check, not only by disabled
    // buttons. A reader session must not be able to mutate the canvas.
    const readerCsrf = await csrf(readerContext)
    const forbiddenWrite = await readerContext.request.fetch(`/api/v1/canvases/${canvasId}/`, {
      method: 'PATCH',
      data: { name: 'reader must not rename this canvas', expected_version: 0 },
      headers: { 'X-CSRFToken': readerCsrf },
    })
    expect(forbiddenWrite.status()).toBe(403)

    // Exercise the actual signed reader WebSocket ticket. Even if a malicious
    // client bypasses the UI, the sync service must reject mutation messages.
    const ticket = await json<{ ticket: string; url: string }>(readerContext.request, `/api/v1/canvases/${canvasId}/sync-ticket/`, 'POST', {}, readerCsrf)
    const websocketResult = await reader.evaluate(async ({ url, signedTicket }) => {
      return await new Promise<{ code?: string; detail?: string }>((resolve) => {
        const separator = url.includes('?') ? '&' : '?'
        const socket = new WebSocket(`${url}${separator}ticket=${encodeURIComponent(signedTicket)}`)
        const timer = window.setTimeout(() => { socket.close(); resolve({ detail: 'timeout' }) }, 5_000)
        socket.onmessage = event => {
          const message = JSON.parse(event.data)
          if (message.type === 'hello') {
            socket.send(JSON.stringify({
              type: 'ops',
              ops: [{ kind: 'remove', recordId: 'shape:reader-attempt', clock: 1, opId: 'reader-attempt' }],
            }))
          } else if (message.type === 'error') {
            window.clearTimeout(timer)
            socket.close()
            resolve({ code: message.code, detail: message.detail })
          }
        }
        socket.onerror = () => { window.clearTimeout(timer); resolve({ detail: 'socket error' }) }
      })
    }, { url: ticket.url, signedTicket: ticket.ticket })
    expect(websocketResult.code).toBe('read_only')

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
    await expect(owner.locator('.canvas-status')).toContainText(/协作已断开|自动重连|连接错误/, { timeout: 10_000 })
    await owner.getByRole('button', { name: /Markdown/ }).click()
    await expect(owner.getByRole('heading', { name: '编辑 markdown' })).toBeVisible()
    await owner.getByLabel('卡片内容').fill('断网后排队的协作草稿')
    await owner.getByRole('button', { name: '放入画布' }).click()
    await expect(owner.locator('.canvas-status')).toContainText(/待发送|离线|待同步/, { timeout: 10_000 })
    await ownerContext.setOffline(false)
    await expect(owner.locator('.canvas-status')).toContainText(/实时协作已连接/, { timeout: 15_000 })
    await expect(owner.locator('.canvas-status')).not.toContainText(/待发送/, { timeout: 15_000 })

    // Restart the actual sync-service process. The browser must reconnect to
    // a fresh room instance and reconstruct the persisted canvas snapshot.
    const restart = await owner.request.post('http://127.0.0.1:8789/__test/restart')
    expect(restart.ok()).toBeTruthy()
    await reader.reload()
    await waitForWorkbench(reader)
    await waitForCanvas(reader)
    await expect(reader.locator('.oc-card')).toHaveCount(2, { timeout: 20_000 })
    await expect(reader.locator('.oc-card').filter({ hasText: '断网后排队的协作草稿' })).toHaveCount(1, { timeout: 20_000 })

    await owner.reload()
    await waitForWorkbench(owner)
    await waitForCanvas(owner)
    await expect(owner.locator('.canvas-status')).toContainText(/实时协作已连接/, { timeout: 15_000 })

    await Promise.all([ownerContext.close(), editorContext.close(), readerContext.close()])
  })

  test('surfaces a stale offline field and lets the editor explicitly retry it', async ({ browser }) => {
    const runId = `${unique}-conflict-${Math.random().toString(16).slice(2)}`
    const ownerContext = await browser.newContext({ baseURL: 'http://127.0.0.1:5175' })
    const owner = await ownerContext.newPage()
    const { workspaceId, canvasId } = await bootstrapOwner(owner, runId)
    const editorContext = await inviteAndSignup(owner, workspaceId, 'editor', `${runId}-editor`, `${runId}-editor@example.test`)
    const editor = await editorContext.newPage()

    try {
      await Promise.all([editor.goto('/'), waitForWorkbench(editor)])
      await Promise.all([waitForCanvas(owner), waitForCanvas(editor)])

      await editor.getByRole('button', { name: /Markdown/ }).click()
      await editor.getByRole('heading', { name: '编辑 markdown' }).waitFor()
      await editor.getByLabel('卡片内容').fill('冲突测试基线')
      await editor.getByRole('button', { name: '放入画布' }).click()
      await expect(owner.locator('.oc-card .card-body')).toContainText('冲突测试基线', { timeout: 15_000 })

      const canvasResponse = await owner.request.get(`/api/v1/canvases/${canvasId}/`)
      expect(canvasResponse.ok()).toBeTruthy()
      const canvas = await canvasResponse.json() as { snapshot: { document?: { store?: Record<string, any> } } }
      const card = Object.values(canvas.snapshot?.document?.store || {}).find((record: any) => record?.typeName === 'shape' && record?.props?.kind === 'markdown') as any
      expect(card?.id).toBeTruthy()

      await owner.locator('.oc-card').first().click()
      await owner.getByRole('button', { name: '编辑卡片' }).click()
      await expect(owner.getByRole('heading', { name: '编辑 markdown' })).toBeVisible()
      await owner.getByLabel('卡片内容').fill('本地离线冲突')

      const ownerClientId = await owner.evaluate((id) => sessionStorage.getItem(`oc:sync-client:${id}`), canvasId)
      const localClock = await owner.evaluate((id) => Number(sessionStorage.getItem(`oc:sync-clock:${id}`) || 0), canvasId)
      expect(ownerClientId).toBeTruthy()

      await ownerContext.setOffline(true)
      await expect(owner.locator('.canvas-status')).toContainText(/协作已断开|自动重连|连接错误/, { timeout: 10_000 })
      await owner.getByRole('button', { name: '放入画布' }).click()
      await expect(owner.locator('.canvas-status')).toContainText(/待发送|离线|待同步/, { timeout: 10_000 })

      const csrfToken = await csrf(editorContext)
      const ticket = await json<{ ticket: string; url: string }>(editor.request, `/api/v1/canvases/${canvasId}/sync-ticket/`, 'POST', { client_id: 'remote-conflict-client' }, csrfToken)
      const remoteResult = await editor.evaluate(async ({ url, signedTicket, record, clock }) => {
        return await new Promise<any>((resolve) => {
          const separator = url.includes('?') ? '&' : '?'
          const socket = new WebSocket(`${url}${separator}ticket=${encodeURIComponent(signedTicket)}`)
          const timer = window.setTimeout(() => { socket.close(); resolve({ detail: 'timeout' }) }, 10_000)
          socket.onmessage = event => {
            const message = JSON.parse(event.data)
            if (message.type === 'hello') {
              socket.send(JSON.stringify({ type: 'ops', ops: [{
                kind: 'put',
                clock,
                opId: '0',
                record: { ...record, props: { ...record.props, text: '远端冲突值' } },
              }] }))
            } else if (message.type === 'ops_ack') {
              window.clearTimeout(timer)
              socket.close()
              resolve(message)
            }
          }
          socket.onerror = () => { window.clearTimeout(timer); socket.close(); resolve({ detail: 'socket error' }) }
        })
      }, { url: ticket.url, signedTicket: ticket.ticket, record: card, clock: Math.max(1, localClock + 1) })
      expect(remoteResult.durable).toBe(true)
      expect(remoteResult.accepted).toHaveLength(1)

      await ownerContext.setOffline(false)
      // Reconnect can briefly hit the service's anti-flood guard while the
      // offline queue is being replayed; the conflict panel is the durable
      // assertion we actually need here.
      await expect(owner.locator('[aria-label="协作冲突审核"]')).toBeVisible({ timeout: 20_000 })
      await expect(owner.locator('[aria-label="协作冲突审核"]')).toContainText('本地离线冲突')
      await expect(owner.locator('[aria-label="协作冲突审核"]')).toContainText('远端冲突值')

      // The retry gets the next local Lamport clock, which is equal to the
      // remote clock here; opId ordering makes the explicit retry win.
      await owner.locator('[aria-label="协作冲突审核"]').getByRole('button', { name: '重试本地值' }).click()
      await expect(owner.locator('[aria-label="协作冲突审核"]')).not.toBeVisible({ timeout: 10_000 })
      await expect(owner.locator('.oc-card .card-body')).toContainText('本地离线冲突', { timeout: 15_000 })
    } finally {
      await ownerContext.close()
      await editorContext.close()
    }
  })
})
