/**
 * VIP API client — typed fetch wrappers for all backend endpoints.
 */

const BASE = '/api'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}

// ─── Pipeline ────────────────────────────────────────────────────────────────
export const api = {
  pipeline: {
    scan: (folder: string, forceReprocess = false) =>
      request('/pipeline/scan', {
        method: 'POST',
        body: JSON.stringify({ folder, force_reprocess: forceReprocess }),
      }),
    status: () => request<{ status: string; folder: string | null; error: string | null }>('/pipeline/status'),
  },

  // ─── Clusters ─────────────────────────────────────────────────────────────
  clusters: {
    unnamed: () => request<Cluster[]>('/persons/unnamed'),
  },

  // ─── Persons ──────────────────────────────────────────────────────────────
  persons: {
    list: () => request<Person[]>('/persons'),
    namePerson: (id: number, name: string) =>
      request(`/persons/${id}/name`, { method: 'PATCH', body: JSON.stringify({ name }) }),
    merge: (sourceId: number, intoId: number) =>
      request(`/persons/merge?source_id=${sourceId}`, {
        method: 'POST',
        body: JSON.stringify({ into_person_id: intoId }),
      }),
    fromCluster: (clusterId: number, name: string) =>
      request<{ person_id: number; uuid: string }>(`/persons/from-cluster/${clusterId}`, {
        method: 'POST',
        body: JSON.stringify({ name }),
      }),
    addCluster: (personId: number, clusterId: number) =>
      request(`/persons/${personId}/add-cluster/${clusterId}`, { method: 'POST' }),
  },

  // ─── Faces ────────────────────────────────────────────────────────────────
  faces: {
    byCluster: (clusterId: number) => request<FaceRow[]>(`/faces/cluster/${clusterId}`),
    removeFromCluster: (faceId: number) =>
      request(`/faces/${faceId}/from-cluster`, { method: 'DELETE' }),
    thumbnailUrl: (faceId: number) => `${BASE}/faces/${faceId}/thumbnail`,
  },

  // ─── Search ───────────────────────────────────────────────────────────────
  search: {
    query: (params: SearchRequest) =>
      request<SearchResponse>('/search', { method: 'POST', body: JSON.stringify(params) }),
  },

  // ─── Writeback ────────────────────────────────────────────────────────────
  writeback: {
    preview: () => request<WritebackPreview>('/writeback/preview'),
    confirm: (queueIds?: number[]) =>
      request<{ written: number; failed: number }>('/writeback/confirm', {
        method: 'POST',
        body: JSON.stringify({ queue_ids: queueIds ?? null }),
      }),
    status: () => request<Record<string, number>>('/writeback/status'),
  },

  // ─── Admin ──────────────────────────────────────────────────────────────────
  admin: {
    stats: () => request<AdminStats>('/admin/stats'),
    reset: (scope: string) =>
      request<{ status: string; scope: string; detail: string }>(`/admin/reset/${scope}`, {
        method: 'DELETE',
      }),
  },
}

// ─── Types ───────────────────────────────────────────────────────────────────
export interface Cluster {
  id: number
  member_count: number
  intra_similarity: number | null
  is_high_conf: number
  representative_thumbnail: string | null
}

export interface Person {
  id: number
  uuid: string
  name: string | null
  photo_count: number
  is_merged: boolean
  representative_thumbnail: string | null
}

export interface FaceRow {
  id: number
  media_file_id: number
  thumbnail_path: string | null
  detection_conf: number
  person_id: number | null
  cluster_id: number | null
  date_taken: string | null
}

export interface SearchRequest {
  query?: string
  person_ids?: number[]
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
}

export interface SearchResponse {
  results: MediaResult[]
  count: number
}

export interface MediaResult {
  id: number
  file_path: string
  date_taken: string | null
  camera_model: string | null
  persons: string | null
}

export interface WritebackPreview {
  count: number
  items: WritebackItem[]
  warning: string
}

export interface AdminStats {
  media_files: number
  faces: number
  embeddings: number
  clusters: number
  persons: number
  writeback_queue: number
  thumbnail_files: number
  media_by_state: Record<string, number>
}

export interface WritebackItem {
  queue_id: number
  media_file_id: number
  file_path: string
  fields: Record<string, string[]>
}
