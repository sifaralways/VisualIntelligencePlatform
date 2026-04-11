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
    rescan: (forceRetag = false) =>
      request<{ status: string; folder: string }>('/pipeline/rescan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force_retag: forceRetag }),
      }),
    migrateModel: () =>
      request<{ status: string }>('/pipeline/migrate_model', { method: 'POST' }),
    reprocessPhoto: (mediaId: number) =>
      request<{ status: string; media_id: number }>(`/pipeline/reprocess/${mediaId}`, { method: 'POST' }),
    reprocessBatch: (mediaIds: number[]) =>
      request<{ status: string; count: number }>('/pipeline/reprocess_batch', {
        method: 'POST',
        body: JSON.stringify({ media_ids: mediaIds }),
      }),
    status: () => request<{ status: string; folder: string | null; error: string | null }>('/pipeline/status'),
  },

  // ─── Media ────────────────────────────────────────────────────────────────
  media: {
    list: (params: MediaFilter = {}) => {
      const q = new URLSearchParams()
      if (params.limit)             q.set('limit',        String(params.limit))
      if (params.offset)            q.set('offset',       String(params.offset))
      if (params.state)             q.set('state',        params.state)
      if (params.person_id != null)  q.set('person_id',    String(params.person_id))
      if (params.cluster_id != null)  q.set('cluster_id',   String(params.cluster_id))
      if (params.tag_category)        q.set('tag_category', params.tag_category)
      if (params.tag_label)           q.set('tag_label',    params.tag_label)
      if (params.folder_id != null)   q.set('folder_id',    String(params.folder_id))
      return request<MediaFile[]>(`/media?${q}`)
    },
    count: (params: Omit<MediaFilter, 'limit' | 'offset'> = {}) => {
      const q = new URLSearchParams()
      if (params.state)               q.set('state',        params.state)
      if (params.person_id != null)   q.set('person_id',    String(params.person_id))
      if (params.cluster_id != null)  q.set('cluster_id',   String(params.cluster_id))
      if (params.tag_category)        q.set('tag_category', params.tag_category)
      if (params.tag_label)           q.set('tag_label',    params.tag_label)
      if (params.folder_id != null)   q.set('folder_id',    String(params.folder_id))
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
    unnamed: () => request<Cluster[]>('/persons/unnamed'),    delete: (clusterId: number) =>
      request(`/persons/clusters/${clusterId}`, { method: 'DELETE' }),
    ignore: (clusterId: number) =>
      request<{ status: string; cluster_id: number; person_id: number }>(
        `/persons/clusters/${clusterId}/ignore`, { method: 'POST' }
      ),  },

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
    mergePersons: (personAId: number, personBId: number, newName?: string) =>
      request<MergePersonsResult>(`/persons/${personAId}/merge-with/${personBId}`, {
        method: 'POST',
        body: JSON.stringify({ new_name: newName ?? null }),
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
    listIgnored: () => request<IgnoredPerson[]>('/persons/ignored'),
    unignore: (personId: number) =>
      request<{ status: string; person_id: number }>(`/persons/${personId}/unignore`, { method: 'POST' }),
    findSimilarAll: (autoThreshold: number) =>
      request<FindSimilarAllResult>('/persons/find-similar-all', {
        method: 'POST',
        body: JSON.stringify({ auto_threshold: autoThreshold }),
      }),
    frequentlyWith: (personId: number, limit = 5) =>
      request<FrequentlyWithEntry[]>(`/persons/${personId}/frequently-with?limit=${limit}`),
    connectionsGraph: (personId: number, depth = 2) =>
      request<ConnectionGraph>(`/persons/${personId}/connections-graph?depth=${depth}`),
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
    retryFailed: () =>
      request<{ written: number; failed: number; retried: number }>('/writeback/retry-failed', {
        method: 'POST',
      }),
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
    contactsMatch: (threshold: number) =>
      request<ContactsMatchResult>('/admin/contacts-match', {
        method: 'POST',
        body: JSON.stringify({ threshold }),
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

  // ─── Remote servers ──────────────────────────────────────────────────────
  remote: {
    list: () => request<RemoteServer[]>('/remote/servers'),
    create: (cfg: RemoteServerConfig) =>
      request<RemoteServer>('/remote/servers', { method: 'POST', body: JSON.stringify(cfg) }),
    update: (id: number, cfg: RemoteServerConfig) =>
      request<RemoteServer>(`/remote/servers/${id}`, { method: 'PUT', body: JSON.stringify(cfg) }),
    delete: (id: number) =>
      request<{ status: string }>(`/remote/servers/${id}`, { method: 'DELETE' }),
    toggle: (id: number) =>
      request<RemoteServer>(`/remote/servers/${id}/toggle`, { method: 'PATCH' }),
    generateKey: (host: string) =>
      request<{ ssh_key_path: string; public_key: string; already_existed: boolean }>(
        '/remote/generate-key', { method: 'POST', body: JSON.stringify({ host }) }
      ),
    deployKey: (params: { host: string; port: number; user: string; password: string }) =>
      request<{ status: string; message: string }>(
        '/remote/deploy-key', { method: 'POST', body: JSON.stringify(params) }
      ),
    testSSH: (params: { host: string; port: number; user: string; ssh_key_path: string }) =>
      request<{ status: string; message: string }>(
        '/remote/test-ssh', { method: 'POST', body: JSON.stringify(params) }
      ),
    testExiftool: (params: { host: string; port: number; user: string; ssh_key_path: string }) =>
      request<{ status: string; version: string; message: string }>(
        '/remote/test-exiftool', { method: 'POST', body: JSON.stringify(params) }
      ),
    testPath: (params: {
      host: string; port: number; user: string; ssh_key_path: string
      local_path_prefix: string; remote_path_prefix: string; sample_local_path: string
    }) =>
      request<{ status: string; local_path: string; remote_path: string; found: boolean; message: string }>(
        '/remote/test-path', { method: 'POST', body: JSON.stringify(params) }
      ),
    checkWrite: (serverId: number, path?: string) =>
      request<{ status: string; path: string; readable: boolean; writable: boolean; exists: boolean; stat: string; message: string }>(
        `/remote/servers/${serverId}/check-write`,
        { method: 'POST', body: JSON.stringify(path ? { path } : {}) },
      ),
  },
}

// ─── Types ───────────────────────────────────────────────────────────────────

export interface MediaFilter {
  limit?: number
  offset?: number
  state?: string
  person_id?: number
  cluster_id?: number
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
  /** 1 if the person's name has been written to at least one photo file via ExifTool; 0 otherwise. */
  name_written: number
}

export interface IgnoredPerson {
  id: number
  uuid: string
  created_at: string | null
  photo_count: number
  cluster_count: number
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

export interface FindSimilarSuggestion extends MergeSuggestion {
  person_id: number
  person_name: string
  person_thumbnail: string | null
}

export interface FindSimilarAllResult {
  auto_merged: Array<{ person_id: number; person_name: string; cluster_id: number; similarity: number; member_count: number }>
  suggestions: FindSimilarSuggestion[]
}

export interface MergePersonsResult {
  status: string
  survivor_id: number
  survivor_name: string
  absorbed_id: number
  photos_queued_for_writeback: number
}

export interface ConnectionGraphNode {
  id: string
  type: 'person' | 'cluster'
  raw_id: number
  name: string | null
  photo_count: number
  thumbnail: string | null
  depth: number
}

export interface ConnectionGraphEdge {
  source: string
  target: string
  weight: number
}

export interface ConnectionGraph {
  center_id: string
  nodes: ConnectionGraphNode[]
  edges: ConnectionGraphEdge[]
}

export interface FrequentlyWithEntry {
  id: number
  name: string
  shared_photos: number
  representative_thumbnail: string | null
}

export interface SimilarCluster {
  cluster_id: number
  member_count: number
  intra_similarity: number | null
  is_high_conf: number
  representative_thumbnail: string | null
  similarity: number
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

export interface ExifHistory {
  /** XMP:Identifier written by VIP — present only in vip_history */
  identifier?: string
  /** Named persons from XMP:PersonInImage */
  persons?: string[]
  /** VIP-namespaced keywords (obj:/geo:/place:/animal:) */
  vip_keywords?: string[]
  /** Plain keywords from XMP:Subject / IPTC:Keywords */
  plain_keywords?: string[]
  /** XMP:Location free-text */
  location?: string
  /** MWG face regions with names */
  face_regions?: { name: string; type: string | null; area: { X: number; Y: number; W: number; H: number; Unit: string } | null }[]
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
  /** Data previously written to this file by VIP (XMP:Identifier matched) */
  vip_history: ExifHistory | null
  /** Data found in file that was written by an external app, not VIP */
  external_history: ExifHistory | null
  /** True when VIP analysis exists but has not been written to the file yet */
  vip_pending: boolean
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
  type: 'float' | 'int' | 'bool'
  min: number
  max: number
  step: number
  label: string
  description: string
  group: string
  options?: { value: number; label: string }[]  // present → render segmented control
}

// ─── Remote servers ────────────────────────────────────────────────────────

export interface RemoteServer {
  id: number
  label: string
  host: string
  port: number
  user: string
  ssh_key_path: string
  local_path_prefix: string
  remote_path_prefix: string
  writeback_concurrency: number
  enabled: number   // 0 | 1 from SQLite
  created_at: string
  updated_at: string
}

export interface RemoteServerConfig {
  label: string
  host: string
  port: number
  user: string
  ssh_key_path: string
  local_path_prefix: string
  remote_path_prefix: string
  writeback_concurrency: number
  enabled: boolean
}

// ─── Contacts Face Match ─────────────────────────────────────────────────────

export interface ContactsMatchSuggestion {
  contact_name: string
  cluster_id: number
  cluster_size: number
  similarity_pct: number
  auto_name: boolean
  /** Absolute path stored in DB — convert via '/thumbnails/' + path.split('/thumbnails/').pop() */
  thumbnail_path: string | null
}

export interface ContactsMatchStats {
  total_contacts: number
  contacts_with_face: number
  unnamed_clusters: number
  elapsed_seconds: number
}

export interface ContactsMatchResult {
  matches: ContactsMatchSuggestion[]
  stats: ContactsMatchStats
}