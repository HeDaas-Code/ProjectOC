import http from 'node:http'
import { spawn } from 'node:child_process'

const servicePort = Number(process.env.PORT || 8788)
const controlPort = Number(process.env.SYNC_CONTROL_PORT || 8789)
let child
let restartChain = Promise.resolve()

function startChild() {
  child = spawn(process.execPath, ['src/server.js'], {
    cwd: process.cwd(),
    env: process.env,
    stdio: 'inherit',
  })
  child.once('exit', (code, signal) => {
    if (child?.pid === undefined) return
    if (code !== 0 && signal !== 'SIGTERM') {
      console.error(`sync service exited unexpectedly (code=${code}, signal=${signal})`)
    }
    child = undefined
  })
}

function stopChild() {
  const current = child
  if (!current) return Promise.resolve()
  return new Promise(resolve => {
    const finish = () => resolve()
    current.once('exit', finish)
    current.kill('SIGTERM')
    setTimeout(() => {
      if (!current.killed) current.kill('SIGKILL')
      resolve()
    }, 5_000).unref()
  })
}

async function waitForHealth(timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${servicePort}/health`)
      if (response.ok) return
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  throw new Error('sync service did not become healthy after restart')
}

function restartChild() {
  restartChain = restartChain.then(async () => {
    await stopChild()
    startChild()
    await waitForHealth()
  })
  return restartChain
}

const control = http.createServer(async (request, response) => {
  if (request.method === 'GET' && request.url === '/__test/health') {
    response.writeHead(200, {'content-type': 'application/json'})
    response.end(JSON.stringify({ok: true, child: Boolean(child)}))
    return
  }
  if (request.method === 'POST' && request.url === '/__test/restart') {
    try {
      await restartChild()
      response.writeHead(200, {'content-type': 'application/json'})
      response.end(JSON.stringify({ok: true}))
    } catch (error) {
      response.writeHead(500, {'content-type': 'application/json'})
      response.end(JSON.stringify({ok: false, detail: String(error)}))
    }
    return
  }
  response.writeHead(404)
  response.end()
})

startChild()
control.listen(controlPort, '127.0.0.1')

async function shutdown() {
  control.close()
  await stopChild()
  process.exit(0)
}
process.once('SIGTERM', shutdown)
process.once('SIGINT', shutdown)
