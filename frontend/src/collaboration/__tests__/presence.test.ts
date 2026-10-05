import { describe, expect, it } from 'vitest'
import { mergeCollaborator, presenceColor, presenceName, removeCollaborator, type Collaborator } from '../presence'

const alice: Collaborator = { id: 'alice', username: 'Alice', role: 'editor', selection: ['shape:1'] }

describe('presence state', () => {
  it('merges partial cursor updates without dropping selection', () => {
    const result = mergeCollaborator([alice], { id: 'alice', cursor: { x: 12, y: 8 } })
    expect(result).toEqual([{ ...alice, cursor: { x: 12, y: 8 } }])
  })

  it('merges partial selection updates without dropping cursor', () => {
    const withCursor = { ...alice, cursor: { x: 12, y: 8 } }
    const result = mergeCollaborator([withCursor], { id: 'alice', selection: ['shape:2', 'shape:3'] })
    expect(result).toEqual([{ ...withCursor, selection: ['shape:2', 'shape:3'] }])
  })

  it('replaces an existing collaborator and removes departed collaborators', () => {
    const result = mergeCollaborator([alice], { id: 'bob', username: 'Bob' })
    expect(result).toHaveLength(2)
    expect(removeCollaborator(result, 'alice')).toEqual([{ id: 'bob', username: 'Bob' }])
  })

  it('keeps labels and colors deterministic', () => {
    expect(presenceName({ id: 'abcdefghi' })).toBe('abcdefgh')
    expect(presenceColor('alice')).toBe(presenceColor('alice'))
    expect(presenceColor('alice')).not.toBe(presenceColor('bob'))
  })
})
