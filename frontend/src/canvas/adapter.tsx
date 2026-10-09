import React from 'react'
import { BaseBoxShapeUtil, HTMLContainer, T, createShapeId, getSnapshot, loadSnapshot, toRichText, type Editor, type TLBaseShape, type TLShapeId } from '@tldraw/tldraw'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import katex from 'katex'
import type { Proposal } from '../types'
declare module '@tldraw/tlschema' { interface TLGlobalShapePropsMap { 'oc-card': { w: number; h: number; kind: string; text: string; proposalId: string } } }
export type Card = TLBaseShape<'oc-card', { w: number; h: number; kind: string; text: string; proposalId: string }>
declare module '@tldraw/tlschema' { interface TLGlobalShapePropsMap { 'oc-entity': { w: number; h: number; entityId: string; title: string; entityType: string; stale: boolean } } }
export type EntityShape = TLBaseShape<'oc-entity', { w: number; h: number; entityId: string; title: string; entityType: string; stale: boolean }>
const entityColors: Record<string, string> = { canonical_setting: '#658675', character: '#9b83bb', timeline: '#699eb0', event: '#c68d76', item: '#c0ad71', location: '#7ea187', faction: '#9b8c82', floating_tip: '#d0a15c' }
export class EntityUtil extends BaseBoxShapeUtil<EntityShape> {
  static override type = 'oc-entity' as const
  static override props = { w: T.number, h: T.number, entityId: T.string, title: T.string, entityType: T.string, stale: T.boolean }
  getDefaultProps() { return { w: 220, h: 90, entityId: '', title: '未命名实体', entityType: 'floating_tip', stale: false } }
  component(shape: EntityShape) { return <HTMLContainer className="oc-entity-node" style={{ borderColor: entityColors[shape.props.entityType] || '#658675', opacity: shape.props.stale ? .55 : 1 }}><span className="oc-entity-type">{shape.props.entityType}</span><strong>{shape.props.title}</strong></HTMLContainer> }
  getIndicatorPath(shape: EntityShape) { const path = new Path2D(); path.roundRect(0, 0, shape.props.w, shape.props.h, 12); return path }
}
export class CardUtil extends BaseBoxShapeUtil<Card> {
  static override type = 'oc-card' as const
  static override props = { w: T.number, h: T.number, kind: T.string, text: T.string, proposalId: T.string }
  getDefaultProps() { return { w: 290, h: 210, kind: 'markdown', text: '# 新的想法', proposalId: '' } }
  component(shape: Card) {
    const p = shape.props
    const text = typeof p.text === 'string' ? p.text : ''
    let content: React.ReactNode = text
    if (p.kind === 'latex') content = <div dangerouslySetInnerHTML={{ __html: katex.renderToString(text, { throwOnError: false, trust: false }) }} />
    else if (p.kind === 'markdown') content = <div dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(marked.parse(text, { async: false })) }} />
    else if (p.kind === 'draft') content = <DraftContent id={p.proposalId} />
    else if (p.kind === 'mermaid') content = <MermaidContent text={text} />
    return <HTMLContainer className={`oc-card oc-${p.kind}`}><div className="card-kind">{({ markdown: 'MARKDOWN', latex: '公式', draft: '实体草稿 · 等待审核', mermaid: 'MERMAID 图表' } as Record<string,string>)[p.kind]}</div><div className="card-body">{content}</div></HTMLContainer>
  }
  getIndicatorPath(shape: Card) { const path = new Path2D(); path.rect(0, 0, shape.props.w, shape.props.h); return path }
}
let proposalCache: Proposal[] = []
let callbacks = new Set<() => void>()
export function setProposals(proposals: Proposal[]) { proposalCache = proposals; callbacks.forEach(f => f()) }
function DraftContent({ id }: { id: string }) {
  React.useSyncExternalStore(cb => { callbacks.add(cb); return () => callbacks.delete(cb) }, () => proposalCache)
  const p = proposalCache.find(p => p.id === id)
  return p ? <><h3>{p.title}</h3><p>{p.content}</p><small>{p.status === 'accepted' ? '✓ 已入库' : p.status === 'rejected' ? '已拒绝' : '暂存 · 尚未入库'}</small></> : <p>草稿引用未找到</p>
}
function MermaidContent({ text }: { text: string }) {
  const [svg, setSvg] = React.useState('')
  React.useEffect(() => { let live = true; import('mermaid').then(async ({ default: mermaid }) => {
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral' })
    try { const r = await mermaid.render(`mermaid-${crypto.randomUUID()}`, text); if (live) setSvg(DOMPurify.sanitize(r.svg)) } catch { if (live) setSvg('<p>图表语法无效，请编辑</p>') }
  }); return () => { live = false } }, [text])
  return <div dangerouslySetInnerHTML={{ __html: svg }} />
}
export interface CanvasAdapter {
  loadSnapshot(snapshot: unknown): void; getSnapshot(): unknown; createDraftElement(data: Proposal): string
  updateElement(id: string, data: unknown): void; deleteElement(id: string): void; undo(): void; redo(): void
  arrangeGrid(columns?: number): void
}
export class TldrawAdapter implements CanvasAdapter {
  constructor(public editor: Editor) {}
  loadSnapshot(snapshot: unknown) { if (snapshot && typeof snapshot === 'object' && 'document' in snapshot) loadSnapshot(this.editor.store, snapshot as Parameters<typeof loadSnapshot>[1]) }
  getSnapshot() { return getSnapshot(this.editor.store) }
  createDraftElement(data: Proposal) {
    const id = createShapeId(`proposal-${data.id}`)
    if (!this.editor.getShape(id)) {
      const n = this.editor.getCurrentPageShapes().length
      this.editor.createShape<Card>({ id, type: 'oc-card', x: 100 + n % 3 * 320, y: 100 + Math.floor(n / 3) * 240, props: { w: 290, h: 210, kind: 'draft', text: data.content || data.title || '', proposalId: data.id } })
    }
    return id
  }
  updateElement(id: string, data: unknown) { this.editor.updateShape({ id: id as TLShapeId, type: 'oc-card', props: data as Card['props'] }) }
  deleteElement(id: string) { this.editor.deleteShape(id as TLShapeId) }
  undo() { this.editor.undo() }
  redo() { this.editor.redo() }
  arrangeGrid(columns = 4) {
    const shapes = this.editor.getCurrentPageShapes()
    shapes.forEach((shape, index) => this.editor.updateShape({ id: shape.id, type: shape.type, x: (index % columns) * 280, y: Math.floor(index / columns) * 180 }))
  }
  addCard(kind: string, text: string) {
    const center = this.editor.getViewportPageBounds().center
    this.editor.createShape<Card>({ type: 'oc-card', x: center.x - 145, y: center.y - 105, props: { w: 290, h: 210, kind, text, proposalId: '' } })
  }
}
export function syncGraphProjection(editor: Editor, nodes: { id: string; title: string; type: string; status?: string }[], edges: { id: string; source: string; target: string; label: string }[]) {
  const existing = new Set(editor.getCurrentPageShapes().map(shape => shape.id))
  nodes.forEach((node, index) => {
    const id = createShapeId(`entity:${node.id}`)
    const current = editor.getShape(id) as EntityShape | undefined
    if (current) {
      editor.updateShape<EntityShape>({ id, type: 'oc-entity', props: { ...current.props, title: node.title, entityType: node.type, stale: node.status === 'archived' } })
      return
    }
    editor.createShape<EntityShape>({ id, type: 'oc-entity', x: 120 + (index % 4) * 280, y: 120 + Math.floor(index / 4) * 150, props: { w: 220, h: 90, entityId: node.id, title: node.title, entityType: node.type, stale: node.status === 'archived' } })
  })
  edges.forEach(edge => {
    const id = createShapeId(`relation:${edge.id}`)
    if (existing.has(id)) return
    const source = editor.getShape(createShapeId(`entity:${edge.source}`))
    const target = editor.getShape(createShapeId(`entity:${edge.target}`))
    if (!source || !target) return
    editor.createShape({ id, type: 'arrow', x: source.x, y: source.y, props: { kind: 'elbow', color: 'grey', fill: 'none', dash: 'draw', size: 'm', arrowheadStart: 'none', arrowheadEnd: 'triangle', font: 'draw', labelColor: 'grey', start: { x: 0, y: 0 }, end: { x: target.x - source.x, y: target.y - source.y }, bend: 0, richText: toRichText(edge.label), labelPosition: .5, scale: 1, elbowMidPoint: .5 } })
    try {
      editor.createBinding({ type: 'arrow', fromId: id, toId: source.id, props: { terminal: 'start' } })
      editor.createBinding({ type: 'arrow', fromId: id, toId: target.id, props: { terminal: 'end' } })
    } catch { /* older TLDraw schemas can still render the coordinate fallback */ }
  })
}
