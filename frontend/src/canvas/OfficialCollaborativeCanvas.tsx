import React, { useEffect } from 'react'
import { Tldraw, defaultShapeUtils, inlineBase64AssetStore, type Editor } from '@tldraw/tldraw'
import { useSync } from '@tldraw/sync'
import { CardUtil, EntityUtil } from './adapter'

// Keep this array referentially stable. useSync derives its schema from shapeUtils;
// recreating the array on every React render makes the schema look changed, which
// tears down and recreates the room on every status update (an infinite render loop).
const officialShapeUtils = [...defaultShapeUtils, CardUtil, EntityUtil]

export interface OfficialCollaborativeCanvasProps {
  uri: string | (() => Promise<string>)
  readOnly: boolean
  licenseKey?: string
  onMount?: (editor: Editor) => void
  onStatus?: (status: string, error?: Error) => void
}

/**
 * Official TLDraw sync-core client, the only supported canvas transport. The server grants read-only access during the handshake;
 * the local editor is also locked immediately for readers to avoid a misleading
 * editable frame while the room is loading.
 */
export function OfficialCollaborativeCanvas({
  uri,
  readOnly,
  licenseKey,
  onMount,
  onStatus,
}: OfficialCollaborativeCanvasProps) {
  const remote = useSync({
    uri,
    assets: inlineBase64AssetStore,
    shapeUtils: officialShapeUtils,
  })

  useEffect(() => {
    if (remote.status === 'loading') onStatus?.('连接中…')
    else if (remote.status === 'error') onStatus?.('实时协作连接失败', remote.error)
    else onStatus?.(remote.status === 'synced-remote' && remote.connectionStatus === 'offline' ? '离线 · 等待重连' : '实时协作已连接')
  }, [remote.status, remote.status === 'error' ? remote.error : undefined, remote.status === 'synced-remote' ? remote.connectionStatus : undefined, onStatus])

  if (remote.status === 'loading') return <div className="canvas-sync-loading">正在连接画布同步…</div>
  if (remote.status === 'error') return <div className="canvas-sync-error">实时协作连接失败：{remote.error.message}</div>

  return (
    <Tldraw
      store={remote.store}
      licenseKey={licenseKey}
      shapeUtils={officialShapeUtils}
      onMount={(editor) => {
        editor.updateInstanceState({ isReadonly: readOnly })
        onMount?.(editor)
      }}
    />
  )
}
