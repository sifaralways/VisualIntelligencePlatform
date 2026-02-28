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

  // ─── Media ────────────────────────────────────────────────────────────────
  media: {
    list: (params: MediaFilter = {}) => {
      const q = new URLSearchParams()
      if (params.limit)        q.set('limit',        String(params.limit))
      if (params.offset)       q.set('offset',       String(params.offset))
      if (params.state)        q.set('state',        params.state)
      if (params.person_id != null) q.set('person_id', String(params.person_id))
      if (params.tag_category) q.set('tag_category', params.tag_category)
      if (params.tag_label)    q.set('tag_label',    params.tag_label)
      return request<MediaFile[]>(`/media?${q}`)
    },
    count: (params: Omit<MediaFilter, 'limit' | 'offset'> = {}) => {
      const q = new URLSearchParams()
      if (params.state)        q.set('state',        params.state)
      if (params.person_id != null) q.set('person_id', String(params.person_id))
      if (params.tag_category) q.set('tag_category', params.tag_category)
      if (params.tag_label)    q.set('tag_label',    params.tag_label)
      return request<{ count: number }>(`/media/count?${q}`)
    },
    get: (id: number) => request<MediaFile>(`/media/${id}`),
    tags: (id: number) => request<TagsByCategory>(`/tags/${id}`),
    thumbnailUrl: (id: number) => `${BASE}/media/${id}/thumbnail`,
    previewUrl:   (id: number) => `${BASE}/media/${id}/preview`,
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
    byPerson: (personId: number) => request<FaceRow[]>(`/persons/${personId}/faces`),
    byMedia: (mediaId: number) => request<FaceRow[]>(`/faces/media/${mediaId}`),
    removeFromCluster: (faceId: number) =>
      request(`/faces/${faceId}/from-cluster`, { method: 'DELETE' }),
    removeFromPerson: (faceId: number) =>
      request(`/faces/${faceId}/from-person`, { method: 'DELETE' }),
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

  // ─── Tags ────────────────────────────────────────────────────────────────
  tags: {
    /** All ML tags for one media file, grouped by category. */
    byFile: (mediaFileId: number) =>
      request<TagsByCategory>(`/tags/${mediaFileId}`),
    /** Most frequent tags across the whole library, optional category filter. */
    top: (category?: string, limit = 50) => {
      const q = category ? `?category=${category}&limit=${limit}` : `?limit=${limit}`
      return request<TopTag[]>(`/tags/summary/top${q}`)
    },
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

export interface MediaFilter {
  limit?: number
  offset?: number
  state?: string
  person_id?: number
  tag_category?: string
  tag_label?: string
}

export interface MediaFile {
  id: number
  file_path: string
  file_hash: string
  file_size: number | null
  file_format: string | null
  camera_make: string | null
  camera_model: string | null
  date_taken: string | null
  gps_lat: number | null
  gps_lon: number | null
  width: number | null
  height: number | null
  is_stub: number
  ingest_state: string
  writeback_done: number
}

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
  media_file_id?: number
  thumbnail_path: string | null
  detection_conf: number
  person_id: number | null
  person_name: string | null
  cluster_id?: number | null
  date_taken?: string | null
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

export type TagCategory = 'object' | 'animal' | 'geography' | 'place'

export interface TagsByCategory {
  object?: string[]
  animal?: string[]
  geography?: string[]
  place?: string[]
}

export interface TopTag {
  category?: string
  label: string
  count: number
}
