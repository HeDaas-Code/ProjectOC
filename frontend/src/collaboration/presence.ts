export type Cursor = { x: number; y: number }

export type Collaborator = {
  id: string
  username?: string
  role?: string
  cursor?: Cursor | null
  selection?: string[]
}

/** Merge a partial presence update without dropping fields sent by an earlier update. */
export function mergeCollaborator(
  collaborators: Collaborator[],
  update: Collaborator,
): Collaborator[] {
  const previous = collaborators.find((collaborator) => collaborator.id === update.id)
  const merged = { ...previous, ...update }
  return [...collaborators.filter((collaborator) => collaborator.id !== update.id), merged]
}

export function removeCollaborator(collaborators: Collaborator[], id: string): Collaborator[] {
  return collaborators.filter((collaborator) => collaborator.id !== id)
}

export function presenceName(collaborator: Collaborator): string {
  return collaborator.username || collaborator.id.slice(0, 8)
}

export function presenceColor(id: string): string {
  const palette = ['#cf6f4d', '#5b7cba', '#8b6bb1', '#3f9a83', '#c18a3d', '#b65f88']
  let hash = 0
  for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  return palette[hash % palette.length]
}
