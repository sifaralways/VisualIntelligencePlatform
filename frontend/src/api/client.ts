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
    rescan: () =>
      request<{ status: string; folder: string }>('/pipeline/rescan', { method: 'POST' }),
    status: () => request<{ status: string; folder: string | null; error: string | null }>('/pipeline/status'),
  },

  // ─── Media ────────────────────────────────────────────────────────────────
  media: {
    list: (params: MediaFilter = {}) => {
      const q = new URLSearchParams()
      if (params.limit)             q.set('limit',        String(params.limit))
      if (params.offset)            q.set('offset',       String(params.offset))
      if (params.state)             q.set('state',        params.state)
      if (params.person_id != null) q.set('person_id',    String(params.person_id))
      if (params.tag_category)      q.set('tag_category', params.tag_category)
      if (params.tag_label)         q.set('tag_label',    params.tag_label)
      if (params.folder_id != null) q.set('folder_id',    String(params.folder_id))
      return request<MediaFile[]>(`/media?${q}`)
    },
    count: (params: Omit<MediaFilter, 'limit' | 'offset'> = {}) => {
      const q = new URLSearchParams()
      if (params.state)             q.set('state',        params.state)
      if (params.person_id != null) q.set('person_id',    String(params.person_id))
      if (params.tag_category)      q.set('tag_category', params.tag_category)
      if (params.tag_label)         q.set('tag_label',    params.tag_label)
      if (params.folder_id != null) q.set('folder_id',    String(params.folder_id))
      return request<{ count: number }>(`/media/count?${q}`)
    },
    get: (id: number) => request<MediaFile>(`/media/${id}`),
    tags: (id: number) => request<TagsByCategory>(`/tags/${id}`),
    thumbnailUrl: (id: number) => `${BASE}/media/${id}/thumbnail`,
    previewUrl:   (id: number) => `${BASE}/media/${id}/preview`,
    quality: (issue: 'blurry' | 'closed_eyes' | 'all' = 'all') =>
      request<QualityIssue[]>(`/media/quality?issue=${issue}`),
    bulkDelete: (mediaIds: number[]) =>
      request<{ deleted: number }>('/media/bulk', {
        method: 'DELETE',
        body: JSON.stringify({ media_ids: mediaIds }),
      }),
    removeFromApp: (mediaIds: number[], force = false) =>
      request<RemoveResult>('/media/remove-from-app', {
        method: 'POST',
        body: JSON.stringify({ media_ids: mediaIds, force }),
      }),
  },

  // ─── Folders ──────────────────────────────────────────────────────────────
  folders: {
    list: () => request<FolderItem[]>('/folders'),
    removeFromApp: (folderId: number, force = false) =>
      request<RemoveResult>(`/folders/${folderId}/remove-from-app?force=${force}`, {
        method: 'POST',
      }),
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
    mergeSuggestions: (personId: number) =>
      request<MergeSuggestion[]>(`/persons/${personId}/merge-suggestions?limit=1`),
    rejectSuggestion: (personId: number, clusterId: number) =>
      request(`/persons/${personId}/reject-suggestion/${clusterId}`, { method: 'POST' }),
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
    /** Write EXIF for a single photo immediately (bypasses queue). */
    writeOne: (mediaId: number) =>
      request<{ status: string; media_id: number; fields_written?: string[]; reason?: string }>(
        `/writeback/single/${mediaId}`,
        { method: 'POST' },
      ),
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

  // ─── Analysis ────────────────────────────────────────────────────────────
  analysis: {
    /** Merged effective document (model + user amendments, person_id resolved to name). */
    get: (mediaId: number) => request<AnalysisDocument>(`/analysis/${mediaId}`),
    /** Raw unmodified model document. */
    raw: (mediaId: number) => request<AnalysisDocument>(`/analysis/${mediaId}/raw`),
    /** Trigger a rebuild for a single photo (background). */
    rebuild: (mediaId: number) => request(`/analysis/${mediaId}/rebuild`, { method: 'POST' }),
    /** List all user amendments for a photo. */
    amendments: (mediaId: number) => request<AnalysisAmendment[]>(`/analysis/${mediaId}/amendments`),
    /** Add or update a user amendment. */
    amend: (mediaId: number, req: AmendRequest) =>
      request(`/analysis/${mediaId}/amend`, { method: 'PUT', body: JSON.stringify(req) }),
    /** Remove an amendment (restores original model label). */
    deleteAmend: (mediaId: number, labelName: string) =>
      request(`/analysis/${mediaId}/amend/${encodeURIComponent(labelName)}`, { method: 'DELETE' }),
  },

  // ─── Admin ──────────────────────────────────────────────────────────────────
  admin: {
    stats: () => request<AdminStats>('/admin/stats'),
    reset: (scope: string) =>
      request<{ status: string; scope: string; detail: string }>(`/admin/reset/${scope}`, {
        method: 'DELETE',
      }),
  },

  // ─── Settings ────────────────────────────────────────────────────────────
  settings: {
    getAll: () => request<AppSetting[]>('/settings'),
    update: (updates: Record<string, number | string>) =>
      request<{ status: string; updated: string[] }>('/settings', {
        method: 'PATCH',
        body: JSON.stringify({ updates }),
      }),
    reset: () => request<{ status: string; detail: string }>('/settings/reset', { method: 'POST' }),
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
  folder_id?: number
}

export interface FolderItem {
  id: number
  folder_path: string
  last_scan_at: string | null
  file_count: number
  status: string
  active_count: number
  pending_writeback_count: number
}

export interface RemoveResult {
  status: 'ok' | 'warning'
  removed?: number
  unwritten_count?: number
  unwritten_paths?: string[]
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

export interface QualityIssue {
  id: number
  file_path: string
  date_taken: string | null
  blur_score: number | null
  is_blurry: number | null
  long_exposure: number | null
  has_closed_eyes: number | null
  width: number | null
  height: number | null
  thumbnail_url: string | null
}

// ─── WebSocket notification payloads (emitted by backend pipeline events) ─────

export interface MergeSuggestionItem {
  person_id: number
  person_name: string
  person_face_id: number | null
  cluster_id: number
  cluster_face_id: number | null
  similarity: number
  member_count: number
}

export interface WsEvent {
  event: string
  // merge_suggestions
  suggestions?: MergeSuggestionItem[]
  // quality_issues_found
  count?: number
  // generic progress fields
  phase?: string
  done?: number
  total?: number
  processed?: number
  clusters?: number
  scanned?: number
  skipped?: number
  merged?: number
  message?: string
  folder?: string
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
  merge_sources_count: number
  is_merged: boolean
  representative_thumbnail: string | null
}

export interface MergeSuggestion {
  cluster_id: number
  member_count: number
  intra_similarity: number | null
  is_high_conf: number
  representative_thumbnail: string | null
  similarity: number   // cosine similarity to the named person's centroid
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
  fields: Record<string, string | number | string[]>
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
// ─── Analysis document types ──────────────────────────────────────────────────

export interface BoundingBox {
  Left: number; Top: number; Width: number; Height: number
}

export interface AnalysisLabel {
  Name: string
  Confidence: number            // 0–100 (Rekognition scale)
  Source: string                // 'yolov11' | 'places365' | 'bioclip' | 'clip' | 'user'
  Instances: { BoundingBox: BoundingBox; Confidence: number }[]
  Parents: { Name: string }[]
  Categories: { Name: string }[]
  Aliases: { Name: string }[]
  UserEdited: boolean
  UserConfirmed: boolean
  OriginalName?: string         // set when action='rename'
}

export interface AnalysisFaceAttribute {
  Value: boolean | string; Confidence: number
}

export interface AnalysisFace {
  face_id: number
  person_id: number | null
  person_name: string | null    // resolved at read time from persons table
  detection_conf: number
  bbox: BoundingBox
  // Rekognition-format attributes (null = model not available yet)
  AgeRange: { Low: number; High: number } | null
  Gender: { Value: string; Confidence: number } | null
  Pose: { Yaw: number; Pitch: number; Roll: number } | null
  Landmarks: { Type: string; X: number; Y: number }[] | null
  Quality: { Brightness: number; Sharpness: number } | null
  Smile: AnalysisFaceAttribute | null
  Eyeglasses: AnalysisFaceAttribute | null
  Sunglasses: AnalysisFaceAttribute | null
  EyesOpen: AnalysisFaceAttribute | null
  MouthOpen: AnalysisFaceAttribute | null
  Beard: AnalysisFaceAttribute | null
  Emotions: { Type: string; Confidence: number }[] | null
  FaceOccluded: AnalysisFaceAttribute | null
}

export interface AnalysisDocument {
  schema_version: string
  vip_id: string | null
  media_id: number
  file_path: string
  date_taken: string | null
  camera: string | null
  image_size: { width: number | null; height: number | null }
  file_format: string | null
  Labels: AnalysisLabel[]
  Faces: AnalysisFace[]
  Geography: { gps_lat: number | null; gps_lon: number | null; labels: string[] }
  model_version: string
  generated_at: string
  updated_at?: string
}

export interface AnalysisAmendment {
  label_name: string
  action: 'rename' | 'delete' | 'add' | 'confirm'
  user_value: string | null
  user_confidence: number | null
  amended_at: string
}

export interface AmendRequest {
  label_name: string
  action: 'rename' | 'delete' | 'add' | 'confirm'
  user_value?: string
  user_confidence?: number
}

// ─── Settings ─────────────────────────────────────────────────────────────

export interface AppSetting {
  key: string
  value: number
  default: number
  type: 'float' | 'int'
  min: number
  max: number
  step: number
  label: string
  description: string
  group: string
}