import { useState, useEffect, useRef, useCallback } from 'react'
import LibraryPage from './pages/LibraryPage'
import DashboardPage from './pages/DashboardPage'
import PeoplePage from './pages/PeoplePage'
import DiscoverPage from './pages/DiscoverPage'
import SearchPage from './pages/SearchPage'
import AssistantPage from './pages/AssistantPage'
import TagsPage from './pages/TagsPage'
import WritebackPage from './pages/WritebackPage'
import AdminPage from './pages/AdminPage'
import QualityPage from './pages/QualityPage'
import ExplicitPage from './pages/ExplicitPage'
import PhotoGrid from './components/PhotoGrid'
import PipelinePanel from './components/PipelinePanel'
import ConnectionsGraph from './components/ConnectionsGraph'
import { api, buildProfileWebSocketUrl, setCurrentProfileId } from './api/client'
import type { MediaFilter, WsEvent, MergeSuggestionItem, FolderItem, SubfolderItem, RemoveResult, ProfileSummary, Person, Cluster } from './api/client'
import './index.css'

// ---------------------------------------------------------------------------
// View state machine
// ---------------------------------------------------------------------------

type SidebarSection = 'dashboard' | 'library' | 'people' | 'animals' | 'places' | 'things' | 'search' | 'assistant' | 'assistant_v2' | 'tags' | 'writeback' | 'quality' | 'explicit'

interface FilteredView {
  title: string
  filter: MediaFilter
  backTo: SidebarSection
  /** Track which folder (if any) this view is showing — for sidebar active state */
  folderId?: number
  /** Path prefix when viewing a subfolder — for sidebar active state */
  pathPrefix?: string
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export default function App() {
  const [profiles, setProfiles] = useState<ProfileSummary[]>([])
  const [selectedProfile, setSelectedProfile] = useState<ProfileSummary | null>(null)
  const [profilePickerOpen, setProfilePickerOpen] = useState(true)
  const [profileLoading, setProfileLoading] = useState(true)
  const [profileError, setProfileError] = useState<string | null>(null)
  const [newProfileName, setNewProfileName] = useState('')
  const [newProfilePassword, setNewProfilePassword] = useState('')
  const [newProfilePasswordConfirm, setNewProfilePasswordConfirm] = useState('')
  const [copySettingsFromProfileId, setCopySettingsFromProfileId] = useState('')
  const [creatingProfile, setCreatingProfile] = useState(false)
  const [profileActionBusy, setProfileActionBusy] = useState(false)

  const [section, setSection]       = useState<SidebarSection>('library')
  const [filtered, setFiltered]     = useState<FilteredView | null>(null)
  const [headerSearchQuery, setHeaderSearchQuery] = useState('')
  const [activeSearchQuery, setActiveSearchQuery] = useState('')
  const [searchMode, setSearchMode] = useState<'natural' | 'classic'>('classic')
  const [desktopNotifyEnabled, setDesktopNotifyEnabled] = useState(false)
  const [desktopNotifyStatus, setDesktopNotifyStatus] = useState<'off' | 'on' | 'blocked' | 'unavailable'>('off')
  const [showNotifyBlockedHelp, setShowNotifyBlockedHelp] = useState(false)

  // Pipeline panel collapse state (persisted in sessionStorage for comfort)
  const [pipelineCollapsed, setPipelineCollapsed] = useState(() => {
    return sessionStorage.getItem('pipeline_collapsed') === 'true'
  })
  function togglePipeline() {
    setPipelineCollapsed(v => {
      sessionStorage.setItem('pipeline_collapsed', String(!v))
      return !v
    })
  }

  // Panel widths — persisted so they survive page reload
  const [sidebarWidth, setSidebarWidth] = useState(() =>
    parseInt(localStorage.getItem('vip_sidebar_width') ?? '192', 10)
  )
  const [pipelineWidth, setPipelineWidth] = useState(() =>
    parseInt(localStorage.getItem('vip_pipeline_width') ?? '288', 10)
  )
  const panelDragRef = useRef<{
    which: 'sidebar' | 'pipeline'
    startX: number
    startW: number
  } | null>(null)

  function onPanelDragStart(which: 'sidebar' | 'pipeline', e: React.MouseEvent) {
    e.preventDefault()
    panelDragRef.current = {
      which,
      startX: e.clientX,
      startW: which === 'sidebar' ? sidebarWidth : pipelineWidth,
    }
  }

  useEffect(() => {
    const SIDEBAR_MIN = 140, SIDEBAR_MAX = 480
    const PIPELINE_MIN = 220, PIPELINE_MAX = 600

    function onMouseMove(e: MouseEvent) {
      const drag = panelDragRef.current
      if (!drag) return
      document.body.style.userSelect = 'none'
      document.body.style.cursor = 'col-resize'
      const delta = e.clientX - drag.startX
      if (drag.which === 'sidebar') {
        const w = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, drag.startW + delta))
        setSidebarWidth(w)
        localStorage.setItem('vip_sidebar_width', String(w))
      } else {
        const w = Math.max(PIPELINE_MIN, Math.min(PIPELINE_MAX, drag.startW + delta))
        setPipelineWidth(w)
        localStorage.setItem('vip_pipeline_width', String(w))
      }
    }

    function onMouseUp() {
      if (panelDragRef.current) {
        panelDragRef.current = null
        document.body.style.userSelect = ''
        document.body.style.cursor = ''
      }
    }

    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
    return () => {
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseup', onMouseUp)
    }
  }, [])

  // Admin floating popup
  const [adminOpen, setAdminOpen] = useState(false)

  // Scanned folders displayed in sidebar
  const [folders, setFolders] = useState<FolderItem[]>([])
  const [folderLoadError, setFolderLoadError] = useState(false)
  const [allPhotosExpanded, setAllPhotosExpanded] = useState(false)
  const [folderWarning, setFolderWarning] = useState<{
    folder: FolderItem
    result: RemoveResult
  } | null>(null)
  const [folderRescanBusy, setFolderRescanBusy] = useState(false)
  const [folderRescanMsg, setFolderRescanMsg] = useState<string | null>(null)
  const [folderRescanError, setFolderRescanError] = useState<string | null>(null)
  const [folderWritebackBusy, setFolderWritebackBusy] = useState(false)
  const [folderWritebackMsg, setFolderWritebackMsg] = useState<string | null>(null)
  const [folderWritebackError, setFolderWritebackError] = useState<string | null>(null)
  // Subfolders keyed by scanned folder id — loaded lazily on expand
  const [subfolderMap, setSubfolderMap] = useState<Record<number, SubfolderItem[]>>({})
  const [folderPaneNamed, setFolderPaneNamed] = useState<Person[]>([])
  const [folderPaneUnnamed, setFolderPaneUnnamed] = useState<Cluster[]>([])
  const [folderPaneLoading, setFolderPaneLoading] = useState(false)
  const [folderPaneError, setFolderPaneError] = useState<string | null>(null)
  const [folderPaneBusyKey, setFolderPaneBusyKey] = useState<string | null>(null)
  const [folderPaneConnections, setFolderPaneConnections] = useState<{ personId: number; personName: string } | null>(null)
  const [folderPaneNameTarget, setFolderPaneNameTarget] = useState<Cluster | null>(null)
  const [folderPaneNameInput, setFolderPaneNameInput] = useState('')
  const [folderPaneMergeCandidate, setFolderPaneMergeCandidate] = useState<{ personId: number; name: string } | null>(null)
  const [folderPaneIgnoreTarget, setFolderPaneIgnoreTarget] = useState<Cluster | null>(null)
  const [folderPaneIgnoreAllOpen, setFolderPaneIgnoreAllOpen] = useState(false)
  const [folderPaneSelectMode, setFolderPaneSelectMode] = useState(false)
  const [folderPaneSelectedClusterIds, setFolderPaneSelectedClusterIds] = useState<Set<number>>(new Set())
  const [folderPaneBulkNameOpen, setFolderPaneBulkNameOpen] = useState(false)
  const [folderPaneBulkNameInput, setFolderPaneBulkNameInput] = useState('')
  const [folderPaneBulkIgnoreOpen, setFolderPaneBulkIgnoreOpen] = useState(false)
  const [folderPaneCollapsed, setFolderPaneCollapsed] = useState(() => {
    return sessionStorage.getItem('folder_faces_pane_collapsed') === 'true'
  })

  const loadFolders = useCallback(async () => {
    try {
      setFolderLoadError(false)
      const list = await api.folders.list()
      setFolders(list)
      // Refresh subfolder data for any folder we already have open
      setSubfolderMap(prev => {
        const next = { ...prev }
        // Remove entries for folders no longer in list
        const ids = new Set(list.map(f => f.id))
        for (const k of Object.keys(next)) {
          if (!ids.has(Number(k))) delete next[Number(k)]
        }
        return next
      })
    } catch {
      setFolderLoadError(true)
    }
  }, [])

  const loadSubfolders = useCallback(async (folderId: number) => {
    try {
      const subs = await api.folders.subfolders(folderId)
      setSubfolderMap(prev => ({ ...prev, [folderId]: subs }))
    } catch { /* ignore */ }
  }, [])

  const loadFolderPaneFaces = useCallback(async (opts?: { keepError?: boolean }) => {
    if (!filtered || (!filtered.folderId && !filtered.pathPrefix)) {
      setFolderPaneNamed([])
      setFolderPaneUnnamed([])
      if (!opts?.keepError) setFolderPaneError(null)
      setFolderPaneLoading(false)
      return
    }

    setFolderPaneLoading(true)
    if (!opts?.keepError) setFolderPaneError(null)
    try {
      const scope = filtered.pathPrefix
        ? { path_prefix: filtered.pathPrefix }
        : (filtered.folderId ? { folder_id: filtered.folderId } : {})
      const [persons, unnamedPage] = await Promise.all([
        api.persons.list(scope),
        api.clusters.unnamed({ ...scope, limit: 200, offset: 0, sortBy: 'member_count', sortDir: 'desc' }),
      ])
      setFolderPaneNamed(persons.filter(p => !!p.name && !p.is_merged))
      setFolderPaneUnnamed(unnamedPage.items)
    } catch (e: unknown) {
      setFolderPaneError(e instanceof Error ? e.message : 'Could not load faces pane')
    } finally {
      setFolderPaneLoading(false)
    }
  }, [filtered?.folderId, filtered?.pathPrefix])

  // Right-side face pane (shown in folder-scoped filtered view)
  useEffect(() => {
    void loadFolderPaneFaces()
  }, [loadFolderPaneFaces])

  useEffect(() => {
    // Keep selection valid when unnamed list changes after reload/actions.
    setFolderPaneSelectedClusterIds(prev => {
      if (prev.size === 0) return prev
      const visible = new Set(folderPaneUnnamed.map(c => c.id))
      const next = new Set<number>()
      for (const id of prev) {
        if (visible.has(id)) next.add(id)
      }
      return next
    })
  }, [folderPaneUnnamed])

  function faceThumbUrl(path: string | null | undefined): string | null {
    if (!path) return null
    const marker = '/thumbnails/'
    const idx = path.indexOf(marker)
    if (idx >= 0) return marker + path.slice(idx + marker.length)
    return path
  }

  async function handleFolderPaneNameCluster(cluster: Cluster) {
    setFolderPaneNameTarget(cluster)
    setFolderPaneNameInput('')
    setFolderPaneMergeCandidate(null)
  }

  async function handleFolderPaneConfirmName(forceSamePhotoOverride = false) {
    if (!folderPaneNameTarget) return
    const name = folderPaneNameInput.trim()
    if (!name) return
    setFolderPaneBusyKey(`name-${folderPaneNameTarget.id}`)
    setFolderPaneError(null)
    try {
      const persons = await api.persons.list()
      const existing = persons.find(p => (p.name ?? '').toLowerCase() === name.toLowerCase() && !p.is_merged)
      if (existing) {
        setFolderPaneMergeCandidate({ personId: existing.id, name: existing.name ?? name })
        if (!forceSamePhotoOverride) {
          return
        }
        await api.persons.addCluster(existing.id, folderPaneNameTarget.id, { forceSamePhotoOverride: true })
      } else {
        await api.persons.fromCluster(folderPaneNameTarget.id, name)
      }
      setFolderPaneNameTarget(null)
      setFolderPaneNameInput('')
      setFolderPaneMergeCandidate(null)
      await loadFolderPaneFaces()
    } catch (e: unknown) {
      setFolderPaneError(e instanceof Error ? e.message : 'Could not name this cluster')
      await loadFolderPaneFaces({ keepError: true })
    } finally {
      setFolderPaneBusyKey(null)
    }
  }

  async function handleFolderPaneIgnoreCluster(cluster: Cluster) {
    setFolderPaneIgnoreTarget(cluster)
  }

  async function handleFolderPaneConfirmIgnoreCluster() {
    if (!folderPaneIgnoreTarget) return
    setFolderPaneBusyKey(`ignore-${folderPaneIgnoreTarget.id}`)
    setFolderPaneError(null)
    try {
      await api.clusters.ignore(folderPaneIgnoreTarget.id)
      setFolderPaneIgnoreTarget(null)
      await loadFolderPaneFaces()
    } catch (e: unknown) {
      setFolderPaneError(e instanceof Error ? e.message : 'Could not ignore this cluster')
      await loadFolderPaneFaces({ keepError: true })
    } finally {
      setFolderPaneBusyKey(null)
    }
  }

  async function handleFolderPaneIgnoreAllUnnamed() {
    if (folderPaneUnnamed.length === 0) return
    setFolderPaneIgnoreAllOpen(true)
  }

  async function handleFolderPaneConfirmIgnoreAllUnnamed() {
    setFolderPaneBusyKey('ignore-all')
    setFolderPaneError(null)
    try {
      const results = await Promise.allSettled(folderPaneUnnamed.map(cluster => api.clusters.ignore(cluster.id)))
      const failed = results.filter(r => r.status === 'rejected').length
      if (failed > 0) {
        setFolderPaneError(`Ignored ${results.length - failed}/${results.length} clusters. ${failed} failed.`)
        await loadFolderPaneFaces({ keepError: true })
      } else {
        await loadFolderPaneFaces()
      }
      setFolderPaneIgnoreAllOpen(false)
    } catch (e: unknown) {
      setFolderPaneError(e instanceof Error ? e.message : 'Could not ignore unnamed clusters')
      await loadFolderPaneFaces({ keepError: true })
    } finally {
      setFolderPaneBusyKey(null)
    }
  }

  function toggleFolderPaneSelect(clusterId: number) {
    setFolderPaneSelectedClusterIds(prev => {
      const next = new Set(prev)
      if (next.has(clusterId)) next.delete(clusterId)
      else next.add(clusterId)
      return next
    })
  }

  function exitFolderPaneSelectMode() {
    setFolderPaneSelectMode(false)
    setFolderPaneSelectedClusterIds(new Set())
    setFolderPaneBulkNameOpen(false)
    setFolderPaneBulkNameInput('')
    setFolderPaneBulkIgnoreOpen(false)
  }

  async function handleFolderPaneBulkName() {
    const name = folderPaneBulkNameInput.trim()
    const selectedIds = [...folderPaneSelectedClusterIds]
    if (!name || selectedIds.length === 0) return
    setFolderPaneBusyKey('bulk-name')
    setFolderPaneError(null)
    try {
      const persons = await api.persons.list()
      const existing = persons.find(p => (p.name ?? '').toLowerCase() === name.toLowerCase() && !p.is_merged)
      if (existing) {
        const results = await Promise.allSettled(selectedIds.map(id => api.persons.addCluster(existing.id, id)))
        const failed = results.filter(r => r.status === 'rejected').length
        if (failed > 0) {
          setFolderPaneError(`Named ${results.length - failed}/${results.length} clusters. ${failed} failed.`)
        }
      } else {
        const [first, ...rest] = selectedIds
        const created = await api.persons.fromCluster(first, name)
        if (rest.length > 0) {
          const results = await Promise.allSettled(rest.map(id => api.persons.addCluster(created.person_id, id)))
          const failed = results.filter(r => r.status === 'rejected').length
          if (failed > 0) {
            setFolderPaneError(`Named ${selectedIds.length - failed}/${selectedIds.length} clusters. ${failed} failed.`)
          }
        }
      }
      setFolderPaneBulkNameOpen(false)
      setFolderPaneBulkNameInput('')
      exitFolderPaneSelectMode()
      await loadFolderPaneFaces({ keepError: true })
    } catch (e: unknown) {
      setFolderPaneError(e instanceof Error ? e.message : 'Could not apply bulk naming')
      await loadFolderPaneFaces({ keepError: true })
    } finally {
      setFolderPaneBusyKey(null)
    }
  }

  async function handleFolderPaneBulkIgnoreSelected() {
    const selectedIds = [...folderPaneSelectedClusterIds]
    if (selectedIds.length === 0) return
    setFolderPaneBusyKey('bulk-ignore')
    setFolderPaneError(null)
    try {
      const results = await Promise.allSettled(selectedIds.map(id => api.clusters.ignore(id)))
      const failed = results.filter(r => r.status === 'rejected').length
      if (failed > 0) {
        setFolderPaneError(`Ignored ${results.length - failed}/${results.length} clusters. ${failed} failed.`)
      }
      setFolderPaneBulkIgnoreOpen(false)
      exitFolderPaneSelectMode()
      await loadFolderPaneFaces({ keepError: true })
    } catch (e: unknown) {
      setFolderPaneError(e instanceof Error ? e.message : 'Could not ignore selected clusters')
      await loadFolderPaneFaces({ keepError: true })
    } finally {
      setFolderPaneBusyKey(null)
    }
  }

  // ── Global pipeline notifications ─────────────────────────────────────────
  const wsRef = useRef<WebSocket | null>(null)
  const [qualityCount,     setQualityCount]     = useState<number | null>(null)
  const [mergeSuggestions, setMergeSuggestions] = useState<MergeSuggestionItem[]>([])
  const [mergeWorking,     setMergeWorking]     = useState(false)

  const resetProfileViewState = useCallback(() => {
    setSection('library')
    setFiltered(null)
    setHeaderSearchQuery('')
    setActiveSearchQuery('')
    setSearchMode('classic')
    setFolders([])
    setFolderLoadError(false)
    setAllPhotosExpanded(false)
    setFolderWarning(null)
    setSubfolderMap({})
    setQualityCount(null)
    setMergeSuggestions([])
    setAdminOpen(false)
  }, [])

  const refreshProfiles = useCallback(async () => {
    setProfileLoading(true)
    setProfileError(null)
    try {
      const list = await api.profiles.list()
      setProfiles(list)
    } catch (e: unknown) {
      setProfileError(e instanceof Error ? e.message : 'Could not load profiles')
    } finally {
      setProfileLoading(false)
    }
  }, [])

  const activateProfile = useCallback(async (profileId: string, password?: string): Promise<boolean> => {
    setProfileError(null)
    try {
      const active = await api.profiles.select(profileId, password)
      setCurrentProfileId(active.id)
      setSelectedProfile(active)
      setProfiles(prev => prev.map(profile => ({
        ...profile,
        is_active: profile.id === active.id,
      })))
      resetProfileViewState()
      setProfilePickerOpen(false)
      return true
    } catch (e: unknown) {
      setProfileError(e instanceof Error ? e.message : 'Could not switch profile')
      return false
    }
  }, [resetProfileViewState])

  const createAndActivateProfile = useCallback(async () => {
    const name = newProfileName.trim()
    if (!name) return
    if (newProfilePassword.trim() && newProfilePassword !== newProfilePasswordConfirm) {
      setProfileError('Password and confirm password must match')
      return
    }
    setCreatingProfile(true)
    setProfileError(null)
    try {
      const created = await api.profiles.create(
        name,
        copySettingsFromProfileId || undefined,
        newProfilePassword.trim() || undefined,
      )
      setProfiles(prev => [...prev, created])
      setNewProfileName('')
      setNewProfilePassword('')
      setNewProfilePasswordConfirm('')
      setCopySettingsFromProfileId('')
      await activateProfile(created.id, newProfilePassword.trim() || undefined)
    } catch (e: unknown) {
      setProfileError(e instanceof Error ? e.message : 'Could not create profile')
    } finally {
      setCreatingProfile(false)
    }
  }, [activateProfile, copySettingsFromProfileId, newProfileName, newProfilePassword, newProfilePasswordConfirm])

  const renameProfile = useCallback(async (profileId: string, name: string) => {
    setProfileActionBusy(true)
    setProfileError(null)
    try {
      const updated = await api.profiles.rename(profileId, name)
      setProfiles(prev => prev.map(profile => profile.id === profileId ? { ...profile, name: updated.name } : profile))
      setSelectedProfile(prev => prev && prev.id === profileId ? { ...prev, name: updated.name } : prev)
    } catch (e: unknown) {
      setProfileError(e instanceof Error ? e.message : 'Could not rename profile')
    } finally {
      setProfileActionBusy(false)
    }
  }, [])

  const deleteProfile = useCallback(async (profileId: string) => {
    setProfileActionBusy(true)
    setProfileError(null)
    try {
      await api.profiles.delete(profileId)
      const [list, active] = await Promise.all([api.profiles.list(), api.profiles.active()])
      setProfiles(list)
      setSelectedProfile(active)
      setCurrentProfileId(active.id)
      resetProfileViewState()
    } catch (e: unknown) {
      setProfileError(e instanceof Error ? e.message : 'Could not delete profile')
    } finally {
      setProfileActionBusy(false)
    }
  }, [resetProfileViewState])

  const setProfilePassword = useCallback(async (profileId: string, password: string, currentPassword?: string): Promise<boolean> => {
    setProfileActionBusy(true)
    setProfileError(null)
    try {
      const updated = await api.profiles.setPassword(profileId, password, currentPassword)
      setProfiles(prev => prev.map(profile => profile.id === profileId ? updated : profile))
      setSelectedProfile(prev => prev && prev.id === profileId ? updated : prev)
      return true
    } catch (e: unknown) {
      setProfileError(e instanceof Error ? e.message : 'Could not update profile password')
      return false
    } finally {
      setProfileActionBusy(false)
    }
  }, [])

  const clearProfilePassword = useCallback(async (profileId: string, currentPassword?: string): Promise<boolean> => {
    setProfileActionBusy(true)
    setProfileError(null)
    try {
      const updated = await api.profiles.setPassword(profileId, null, currentPassword)
      setProfiles(prev => prev.map(profile => profile.id === profileId ? updated : profile))
      setSelectedProfile(prev => prev && prev.id === profileId ? updated : prev)
      return true
    } catch (e: unknown) {
      setProfileError(e instanceof Error ? e.message : 'Could not remove profile password')
      return false
    } finally {
      setProfileActionBusy(false)
    }
  }, [])

  useEffect(() => {
    refreshProfiles()
  }, [refreshProfiles])

  useEffect(() => {
    if (typeof Notification === 'undefined') {
      setDesktopNotifyStatus('unavailable')
      setDesktopNotifyEnabled(false)
      return
    }
    const remembered = localStorage.getItem('vip_desktop_notify_enabled')
    if (Notification.permission === 'granted') {
      // Permission already granted — restore enabled state if user had it on.
      if (remembered === '1') {
        setDesktopNotifyStatus('on')
        setDesktopNotifyEnabled(true)
      } else {
        setDesktopNotifyStatus('off')
        setDesktopNotifyEnabled(false)
      }
      return
    }
    if (Notification.permission === 'denied') {
      setDesktopNotifyStatus('blocked')
      setDesktopNotifyEnabled(false)
      return
    }
    setDesktopNotifyStatus('off')
    setDesktopNotifyEnabled(false)
  }, [])

  // Re-check permission when user returns to the tab (e.g. after changing browser settings).
  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState !== 'visible') return
      if (typeof Notification === 'undefined') return
      if (Notification.permission === 'granted') {
        setDesktopNotifyStatus(prev => {
          if (prev === 'blocked' || prev === 'off') {
            // Auto-enable since permission is now granted.
            setDesktopNotifyEnabled(true)
            setShowNotifyBlockedHelp(false)
            localStorage.setItem('vip_desktop_notify_enabled', '1')
            return 'on'
          }
          return prev
        })
      } else if (Notification.permission === 'denied') {
        setDesktopNotifyEnabled(false)
        setDesktopNotifyStatus('blocked')
      }
    }
    document.addEventListener('visibilitychange', handleVisibility)
    return () => document.removeEventListener('visibilitychange', handleVisibility)
  }, [])

  function sendDesktopNotification(title: string, body: string, tag: string) {
    if (!desktopNotifyEnabled) return
    if (typeof Notification === 'undefined') return
    if (Notification.permission !== 'granted') return
    try {
      new Notification(title, { body, tag })
    } catch {
      // Ignore notification API failures in unsupported contexts.
    }
  }

  async function enableDesktopNotifications() {
    if (desktopNotifyEnabled) {
      setDesktopNotifyEnabled(false)
      setDesktopNotifyStatus('off')
      localStorage.setItem('vip_desktop_notify_enabled', '0')
      return
    }

    if (typeof Notification === 'undefined') {
      setDesktopNotifyStatus('unavailable')
      return
    }

    if (Notification.permission === 'denied') {
      // Already denied — try calling requestPermission anyway; some browsers
      // may re-prompt when the site has been un-blocked in settings.
      const result = await Notification.requestPermission().catch(() => 'denied' as NotificationPermission)
      if (result === 'granted') {
        setDesktopNotifyEnabled(true)
        setDesktopNotifyStatus('on')
        setShowNotifyBlockedHelp(false)
        localStorage.setItem('vip_desktop_notify_enabled', '1')
        sendDesktopNotification('VIP Notifications Enabled', 'You will receive background process updates.', 'vip-notify-enabled')
        return
      }
      // Still denied — show help popover with macOS instructions.
      setDesktopNotifyStatus('blocked')
      setShowNotifyBlockedHelp(true)
      return
    }

    if (Notification.permission === 'granted') {
      setDesktopNotifyEnabled(true)
      setDesktopNotifyStatus('on')
      localStorage.setItem('vip_desktop_notify_enabled', '1')
      sendDesktopNotification('VIP Notifications Enabled', 'You will receive background process updates.', 'vip-notify-enabled')
      return
    }

    const result = await Notification.requestPermission()
    const enabled = result === 'granted'
    setDesktopNotifyEnabled(enabled)
    setDesktopNotifyStatus(enabled ? 'on' : (result === 'denied' ? 'blocked' : 'off'))
    if (result === 'denied') setShowNotifyBlockedHelp(true)
    localStorage.setItem('vip_desktop_notify_enabled', enabled ? '1' : '0')
    if (enabled) {
      sendDesktopNotification('VIP Notifications Enabled', 'You will receive background process updates.', 'vip-notify-enabled')
    }
  }

  useEffect(() => {
    if (!selectedProfile) return
    const activeProfileId = selectedProfile.id
    loadFolders()
    function connect() {
      const ws = new WebSocket(buildProfileWebSocketUrl(activeProfileId))
      wsRef.current = ws
      ws.onmessage = (msg) => {
        try {
          const ev: WsEvent = JSON.parse(msg.data)
          if (ev.event === 'merge_suggestions' && ev.suggestions?.length) {
            setMergeSuggestions(prev => {
              // Append only new ones (by cluster_id)
              const existingIds = new Set(prev.map(s => s.cluster_id))
              const fresh = ev.suggestions!.filter(s => !existingIds.has(s.cluster_id))
              return [...prev, ...fresh]
            })
          }
          if (ev.event === 'quality_issues_found' && ev.count) {
            setQualityCount(ev.count)
            sendDesktopNotification('VIP Quality Check', `${ev.count} quality issues found`, 'vip-quality')
          }
          if (ev.event === 'pipeline_start') {
            sendDesktopNotification('VIP Background Process Started', ev.folder ? `Started: ${ev.folder}` : 'Pipeline started', 'vip-pipeline-start')
          }
          if (ev.event === 'pipeline_complete') {
            loadFolders()
            sendDesktopNotification('VIP Background Process Completed', ev.folder ? `Completed: ${ev.folder}` : 'Pipeline completed', 'vip-pipeline-complete')
          }
          if (ev.event === 'pipeline_pausing') {
            sendDesktopNotification('VIP Pipeline Pausing', ev.message ?? 'Pause requested', 'vip-pipeline-pausing')
          }
          if (ev.event === 'pipeline_resumed') {
            sendDesktopNotification('VIP Pipeline Resumed', ev.message ?? 'Pipeline resumed', 'vip-pipeline-resumed')
          }
          if (ev.event === 'pipeline_stopping') {
            sendDesktopNotification('VIP Pipeline Stopping', ev.message ?? 'Stop requested', 'vip-pipeline-stopping')
          }
          if (ev.event === 'suggestion_worker_started') {
            sendDesktopNotification('VIP Suggestions Worker Started', ev.message ?? 'Background suggestions started', 'vip-worker-started')
          }
          if (ev.event === 'suggestion_worker_paused') {
            sendDesktopNotification('VIP Suggestions Worker Paused', ev.message ?? 'Background suggestions paused', 'vip-worker-paused')
          }
          if (ev.event === 'suggestion_worker_resumed') {
            sendDesktopNotification('VIP Suggestions Worker Resumed', ev.message ?? 'Background suggestions resumed', 'vip-worker-resumed')
          }
          if (ev.event === 'suggestion_worker_cycle') {
            const generated = typeof ev.generated === 'number' ? ev.generated : 0
            const persons = typeof ev.persons === 'number' ? ev.persons : 0
            sendDesktopNotification('VIP Suggestions Cycle', `Processed ${persons} person(s), generated ${generated} suggestion(s)`, 'vip-worker-cycle')
          }
          if (ev.event === 'suggestion_worker_stopped') {
            sendDesktopNotification('VIP Suggestions Worker Stopped', ev.message ?? 'Background suggestions stopped', 'vip-worker-stopped')
          }
        } catch {}
      }
      ws.onclose = () => {
        // Auto-reconnect after 3 s
        setTimeout(connect, 3000)
      }
    }
    connect()
    return () => wsRef.current?.close()
  }, [loadFolders, selectedProfile])

  // Handle merge suggestion: accept (merge) or skip
  const handleMergeAction = useCallback(async (action: 'merge' | 'skip') => {
    const top = mergeSuggestions[0]
    if (!top) return
    setMergeWorking(true)
    try {
      if (action === 'merge') {
        await api.persons.addCluster(top.person_id, top.cluster_id)
      } else {
        await api.persons.rejectSuggestion(top.person_id, top.cluster_id)
      }
    } catch { /* ignore */ }
    setMergeWorking(false)
    setMergeSuggestions(prev => prev.slice(1))
  }, [mergeSuggestions])

  /** Navigate to a filtered photo grid (e.g. photos of Alice, photos of dogs) */
  function openFiltered(filter: MediaFilter, title: string, backTo: SidebarSection, folderId?: number, pathPrefix?: string) {
    setFolderRescanMsg(null)
    setFolderRescanError(null)
    setFolderWritebackMsg(null)
    setFolderWritebackError(null)
    setFiltered({ filter, title, backTo, folderId, pathPrefix })
  }

  /** Go back from filtered view to the discover/people section */
  function closeFiltered() {
    setFolderRescanMsg(null)
    setFolderRescanError(null)
    setFolderWritebackMsg(null)
    setFolderWritebackError(null)
    setFiltered(null)
  }

  /** Switch sidebar section — clears any filtered sub-view */
  function navigate(s: SidebarSection) {
    setSection(s)
    setFolderWritebackMsg(null)
    setFolderWritebackError(null)
    setFiltered(null)
  }

  function runHeaderSearch() {
    const q = headerSearchQuery.trim()
    if (!q) return
    setSearchMode('classic')
    setActiveSearchQuery(q)
    setSection('search')
    setFiltered(null)
  }

  /** Start folder remove — check for pending writeback first */
  async function handleFolderRemove(folder: FolderItem, force = false) {
    try {
      const result = await api.folders.removeFromApp(folder.id, force)
      if (result.status === 'warning') {
        setFolderWarning({ folder, result })
        return
      }
      // On success: close warning, refresh folders, go back to library if viewing that folder
      setFolderWarning(null)
      loadFolders()
      if (filtered?.folderId === folder.id) {
        navigate('library')
      }
    } catch { /* ignore */ }
  }

  async function handleFolderRescan() {
    if (!filtered?.folderId) return
    setFolderRescanBusy(true)
    setFolderRescanMsg(null)
    setFolderRescanError(null)
    try {
      await api.pipeline.rescanFolder(filtered.folderId, filtered.pathPrefix)
      setFolderRescanMsg('Rescan started — all available models will re-run for this folder scope.')
      loadFolders()
    } catch (e: unknown) {
      setFolderRescanError(e instanceof Error ? e.message : 'Folder rescan failed')
    } finally {
      setFolderRescanBusy(false)
    }
  }

  async function handleFolderWriteback() {
    if (!filtered?.folderId) return
    setFolderWritebackBusy(true)
    setFolderWritebackMsg(null)
    setFolderWritebackError(null)
    try {
      const result = await api.folders.writeback(filtered.folderId, filtered.pathPrefix)
      setFolderWritebackMsg(`Writeback complete for scope: written=${result.written}, failed=${result.failed}.`)
      await loadFolders()
      await loadSubfolders(filtered.folderId)
    } catch (e: unknown) {
      setFolderWritebackError(e instanceof Error ? e.message : 'Folder writeback failed')
    } finally {
      setFolderWritebackBusy(false)
    }
  }

  if (profileLoading && !selectedProfile) {
    return <div className="h-screen overflow-hidden bg-gray-950 text-gray-100 flex items-center justify-center">Loading profiles…</div>
  }

  if (!selectedProfile) {
    return (
      <ProfilePickerModal
        profiles={profiles}
        newProfileName={newProfileName}
        newProfilePassword={newProfilePassword}
        newProfilePasswordConfirm={newProfilePasswordConfirm}
        copySettingsFromProfileId={copySettingsFromProfileId}
        creatingProfile={creatingProfile}
        profileActionBusy={profileActionBusy}
        error={profileError}
        onProfileNameChange={setNewProfileName}
        onProfilePasswordChange={setNewProfilePassword}
        onProfilePasswordConfirmChange={setNewProfilePasswordConfirm}
        onCopySettingsFromProfileIdChange={setCopySettingsFromProfileId}
        onSelect={activateProfile}
        onCreate={createAndActivateProfile}
        onRename={renameProfile}
        onDelete={deleteProfile}
        onSetPassword={setProfilePassword}
        onClearPassword={clearProfilePassword}
      />
    )
  }

  // ── Main content ──────────────────────────────────────────────────────────

  let mainContent: React.ReactNode

  if (filtered) {
    const rootFolder = filtered.folderId ? folders.find(f => f.id === filtered.folderId) : null
    const scopedPendingWriteback = filtered.folderId
      ? (filtered.pathPrefix
        ? (subfolderMap[filtered.folderId]?.find(s => s.path === filtered.pathPrefix)?.pending_writeback_count ?? 0)
        : (rootFolder?.pending_writeback_count ?? 0))
      : 0

    const folderRescanSlot = filtered.folderId ? (
      <div className="flex items-center gap-2">
        <button
          onClick={handleFolderWriteback}
          disabled={folderWritebackBusy || scopedPendingWriteback <= 0}
          className="text-xs px-3 py-1.5 rounded-lg font-medium bg-emerald-700 text-white hover:bg-emerald-600 disabled:opacity-40 transition-colors"
          title="Write pending metadata to files in this folder scope only"
        >
          {folderWritebackBusy
            ? 'Writing…'
            : `✍ Write Back (${scopedPendingWriteback.toLocaleString()} pending)`}
        </button>
        <button
          onClick={handleFolderRescan}
          disabled={folderRescanBusy}
          className="text-xs px-3 py-1.5 rounded-lg font-medium bg-indigo-700 text-white hover:bg-indigo-600 disabled:opacity-40 transition-colors"
          title="Re-run all enabled models for this folder scope"
        >
          {folderRescanBusy ? 'Queuing…' : '⟳ Rescan Folder'}
        </button>
      </div>
    ) : null

    const scopeFilter: MediaFilter = filtered.pathPrefix
      ? { path_prefix: filtered.pathPrefix }
      : filtered.folderId
        ? { folder_id: filtered.folderId }
        : {}

    const facesPane = (filtered.folderId || filtered.pathPrefix) ? (
      <aside className={`${folderPaneCollapsed ? 'w-10' : 'w-[22rem]'} shrink-0 min-h-0 rounded-xl border border-gray-800 bg-gray-900/40 ${folderPaneCollapsed ? 'p-1.5' : 'p-3'} overflow-y-auto transition-all`}>
        {folderPaneCollapsed ? (
          <button
            onClick={() => {
              setFolderPaneCollapsed(false)
              sessionStorage.setItem('folder_faces_pane_collapsed', 'false')
            }}
            className="w-full h-8 rounded-md border border-gray-700 bg-gray-900 text-gray-300 hover:text-white hover:border-indigo-500 transition-colors text-xs"
            title="Expand faces pane"
          >
            ◂
          </button>
        ) : (
          <>
            <div className="mb-3 flex items-start justify-between gap-2">
              <div>
                <h3 className="text-sm font-semibold text-white">Faces</h3>
                <p className="text-[11px] text-gray-400">People-style face cards in this folder only</p>
              </div>
              <button
                onClick={() => {
                  setFolderPaneCollapsed(true)
                  sessionStorage.setItem('folder_faces_pane_collapsed', 'true')
                }}
                className="h-7 px-2 rounded-md border border-gray-700 bg-gray-900 text-gray-300 hover:text-white hover:border-indigo-500 transition-colors text-xs"
                title="Collapse faces pane"
              >
                ▸
              </button>
            </div>

            {folderPaneError && (
              <div className="text-[11px] text-red-300 bg-red-900/20 border border-red-700/30 rounded-lg px-2 py-1.5 mb-3">
                {folderPaneError}
              </div>
            )}

            <div className="space-y-3">
              <section>
                <div className="mb-1.5 flex items-center justify-between border-l-4 border-emerald-500 pl-2">
                  <span className="text-xs font-semibold uppercase tracking-wide text-emerald-300">Named Faces Bar</span>
                  <span className="text-[11px] text-gray-500">{folderPaneNamed.length}</span>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  {folderPaneLoading && <p className="text-[11px] text-gray-500 px-2 py-1">Loading…</p>}
                  {!folderPaneLoading && folderPaneNamed.length === 0 && (
                    <p className="text-[11px] text-gray-500 px-2 py-1">No named faces</p>
                  )}
                  {!folderPaneLoading && folderPaneNamed.slice(0, 80).map(person => {
                    const thumb = faceThumbUrl(person.representative_thumbnail)
                    return (
                      <div key={person.id} className="flex flex-col items-center gap-1.5">
                        <button
                          onClick={() => openFiltered(
                            { ...scopeFilter, person_id: person.id },
                            `👤 ${person.name}`,
                            'library',
                            filtered.folderId,
                            filtered.pathPrefix,
                          )}
                          className="relative w-20 h-20 rounded-xl bg-gray-800 border border-gray-700 overflow-hidden hover:border-indigo-400 transition-colors"
                          title={`View photos of ${person.name}`}
                        >
                          {thumb
                            ? <img src={thumb} alt={person.name || 'person'} className="w-full h-full object-cover" />
                            : <span className="w-full h-full flex items-center justify-center text-gray-500 text-2xl">👤</span>}
                          <span className="absolute bottom-0 right-0 bg-indigo-700 text-white text-[10px] px-1 rounded-tl leading-tight">
                            {person.photo_count}
                          </span>
                        </button>
                        <span className="text-xs text-center text-gray-200 truncate w-full px-1">{person.name}</span>
                        <button
                          onClick={() => setFolderPaneConnections({ personId: person.id, personName: person.name ?? 'Unknown' })}
                          className="text-[11px] text-gray-500 hover:text-purple-300 transition-colors"
                        >
                          Connections
                        </button>
                      </div>
                    )
                  })}
                </div>
              </section>

              <section>
                <div className="mb-1.5 flex items-center justify-between border-l-4 border-amber-500 pl-2 gap-2">
                  <span className="text-xs font-semibold uppercase tracking-wide text-amber-300">Unnamed Faces Bar</span>
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-gray-500">{folderPaneUnnamed.length}</span>
                    {folderPaneSelectMode && (
                      <span className="text-[11px] text-gray-400">{folderPaneSelectedClusterIds.size} selected</span>
                    )}
                    <button
                      onClick={() => {
                        if (folderPaneSelectMode) exitFolderPaneSelectMode()
                        else setFolderPaneSelectMode(true)
                      }}
                      disabled={folderPaneLoading || !!folderPaneBusyKey || folderPaneUnnamed.length === 0}
                      className="text-[11px] px-2 py-0.5 rounded border border-indigo-800 bg-indigo-900/20 text-indigo-300 hover:bg-indigo-900/40 disabled:opacity-40 transition-colors"
                      title="Select unnamed clusters for bulk actions"
                    >
                      {folderPaneSelectMode ? 'Cancel' : 'Select'}
                    </button>
                    {folderPaneSelectMode && (
                      <>
                        <button
                          onClick={() => setFolderPaneBulkNameOpen(true)}
                          disabled={!!folderPaneBusyKey || folderPaneSelectedClusterIds.size === 0}
                          className="text-[11px] px-2 py-0.5 rounded border border-indigo-700 bg-indigo-900/20 text-indigo-300 hover:bg-indigo-900/40 disabled:opacity-40 transition-colors"
                        >
                          Name selected
                        </button>
                        <button
                          onClick={() => setFolderPaneBulkIgnoreOpen(true)}
                          disabled={!!folderPaneBusyKey || folderPaneSelectedClusterIds.size === 0}
                          className="text-[11px] px-2 py-0.5 rounded border border-red-800 bg-red-900/20 text-red-300 hover:bg-red-900/40 disabled:opacity-40 transition-colors"
                        >
                          Ignore selected
                        </button>
                      </>
                    )}
                    <button
                      onClick={handleFolderPaneIgnoreAllUnnamed}
                      disabled={folderPaneSelectMode || folderPaneLoading || !!folderPaneBusyKey || folderPaneUnnamed.length === 0}
                      className="text-[11px] px-2 py-0.5 rounded border border-red-800 bg-red-900/20 text-red-300 hover:bg-red-900/40 disabled:opacity-40 transition-colors"
                      title="Always ignore all unnamed clusters in this folder scope"
                    >
                      {folderPaneBusyKey === 'ignore-all' ? 'Ignoring…' : 'Ignore all'}
                    </button>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  {folderPaneLoading && <p className="text-[11px] text-gray-500 px-2 py-1">Loading…</p>}
                  {!folderPaneLoading && folderPaneUnnamed.length === 0 && (
                    <p className="text-[11px] text-gray-500 px-2 py-1">No unnamed clusters</p>
                  )}
                  {!folderPaneLoading && folderPaneUnnamed.slice(0, 80).map(cluster => {
                    const thumb = faceThumbUrl(cluster.representative_thumbnail)
                    const nameBusy = folderPaneBusyKey === `name-${cluster.id}`
                    const ignoreBusy = folderPaneBusyKey === `ignore-${cluster.id}`
                    const selected = folderPaneSelectedClusterIds.has(cluster.id)
                    return (
                      <div key={cluster.id} className="flex flex-col items-center gap-1.5">
                        <button
                          onClick={() => {
                            if (folderPaneSelectMode) {
                              toggleFolderPaneSelect(cluster.id)
                              return
                            }
                            openFiltered(
                              { ...scopeFilter, cluster_id: cluster.id },
                              `👤 Unnamed cluster (${cluster.member_count})`,
                              'library',
                              filtered.folderId,
                              filtered.pathPrefix,
                            )
                          }}
                          className={`relative w-20 h-20 rounded-xl bg-gray-800 border overflow-hidden transition-colors ${selected ? 'border-indigo-500 ring-2 ring-indigo-600' : 'border-gray-700 hover:border-indigo-400'}`}
                          title={folderPaneSelectMode ? (selected ? 'Deselect' : 'Select') : 'View cluster photos'}
                        >
                          {thumb
                            ? <img src={thumb} alt="Unnamed cluster" className="w-full h-full object-cover" />
                            : <span className="w-full h-full flex items-center justify-center text-gray-500 text-2xl">?</span>}
                          <span className="absolute bottom-0 right-0 bg-indigo-700 text-white text-[10px] px-1 rounded-tl leading-tight">
                            {cluster.member_count}
                          </span>
                          {folderPaneSelectMode && (
                            <span className={`absolute top-1 right-1 w-5 h-5 rounded-full border-2 flex items-center justify-center text-[10px] font-bold ${selected ? 'bg-indigo-500 border-indigo-400 text-white' : 'bg-black/40 border-gray-400 text-transparent'}`}>
                              ✓
                            </span>
                          )}
                        </button>
                        <span className="text-xs text-center text-gray-400 truncate w-full px-1">Cluster #{cluster.id}</span>
                        {!folderPaneSelectMode && (
                          <div className="flex items-center gap-2 text-[11px]">
                            <button
                              onClick={() => handleFolderPaneNameCluster(cluster)}
                              disabled={!!folderPaneBusyKey}
                              className="text-indigo-300 hover:text-indigo-200 disabled:opacity-40 transition-colors"
                            >
                              {nameBusy ? 'Naming…' : 'Name'}
                            </button>
                            <button
                              onClick={() => handleFolderPaneIgnoreCluster(cluster)}
                              disabled={!!folderPaneBusyKey}
                              className="text-red-300 hover:text-red-200 disabled:opacity-40 transition-colors"
                            >
                              {ignoreBusy ? 'Ignoring…' : 'Ignore'}
                            </button>
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </section>
            </div>
          </>
        )}
      </aside>
    ) : null

    mainContent = (
      <div className="flex flex-col gap-4 h-full">
        <button
          onClick={closeFiltered}
          className="self-start flex items-center gap-1.5 text-sm text-gray-400 hover:text-white transition-colors"
        >
          ← Back
        </button>
        {filtered.folderId && folderRescanMsg && (
          <div className="text-xs text-emerald-300 bg-emerald-900/20 border border-emerald-700/30 rounded-lg px-3 py-2">
            {folderRescanMsg}
          </div>
        )}
        {filtered.folderId && folderRescanError && (
          <div className="text-xs text-red-300 bg-red-900/20 border border-red-700/30 rounded-lg px-3 py-2">
            {folderRescanError}
          </div>
        )}
        {filtered.folderId && folderWritebackMsg && (
          <div className="text-xs text-emerald-300 bg-emerald-900/20 border border-emerald-700/30 rounded-lg px-3 py-2">
            {folderWritebackMsg}
          </div>
        )}
        {filtered.folderId && folderWritebackError && (
          <div className="text-xs text-red-300 bg-red-900/20 border border-red-700/30 rounded-lg px-3 py-2">
            {folderWritebackError}
          </div>
        )}
        <div className="flex gap-4 min-h-0 flex-1">
          <div className="flex-1 min-w-0 min-h-0 overflow-y-auto pr-1">
            <PhotoGrid
              filter={filtered.filter}
              title={filtered.title}
              selectable
              enableReprocess
              headerSlot={folderRescanSlot}
            />
          </div>
          {facesPane}
        </div>
      </div>
    )
  } else {
    switch (section) {
      case 'dashboard':
        mainContent = <DashboardPage />
        break
      case 'library':
        mainContent = <LibraryPage onScanStarted={() => { loadFolders() }} />
        break
      case 'people':
        mainContent = (
          <PeoplePage
            onSelectPerson={(id, name) =>
              openFiltered({ person_id: id }, `👤 ${name}`, 'people')
            }
            onSelectCluster={(id) =>
              openFiltered({ cluster_id: id }, '👤 Unnamed cluster', 'people')
            }
          />
        )
        break
      case 'animals':
        mainContent = (
          <DiscoverPage
            category="animal"
            onSelectTag={(cat, label, title) =>
              openFiltered({ tag_category: cat, tag_label: label }, title, 'animals')
            }
          />
        )
        break
      case 'places':
        mainContent = (
          <DiscoverPage
            category="place"
            onSelectTag={(cat, label, title) =>
              openFiltered({ tag_category: cat, tag_label: label }, title, 'places')
            }
          />
        )
        break
      case 'things':
        mainContent = (
          <DiscoverPage
            category="object"
            onSelectTag={(cat, label, title) =>
              openFiltered({ tag_category: cat, tag_label: label }, title, 'things')
            }
          />
        )
        break
      case 'explicit':
        mainContent = (
          <ExplicitPage
            onSelectLabel={(label) =>
              openFiltered(
                { tag_category: 'explicit', tag_label: label },
                `🔞 ${label.replace(/_/g, ' ')}`,
                'explicit',
              )
            }
          />
        )
        break
      case 'search':
        mainContent = <SearchPage initialQuery={activeSearchQuery} mode={searchMode} />
        break
      case 'assistant':
        mainContent = (
          <AssistantPage
            onOpenSearch={(query) => {
              setSearchMode('natural')
              setActiveSearchQuery(query)
              setSection('search')
              setFiltered(null)
            }}
            mode="v1"
          />
        )
        break
      case 'assistant_v2':
        mainContent = (
          <AssistantPage
            onOpenSearch={(query) => {
              setSearchMode('natural')
              setActiveSearchQuery(query)
              setSection('search')
              setFiltered(null)
            }}
            mode="v2"
          />
        )
        break
      case 'tags':
        mainContent = <TagsPage />
        break
      case 'writeback':
        mainContent = <WritebackPage />
        break
      case 'quality':
        mainContent = <QualityPage />
        break
    }
  }

  return (
    <div className="h-screen overflow-hidden bg-gray-950 text-gray-100 flex flex-col">
      {/* ── Top bar ── */}
      <header className="h-11 border-b border-gray-800 flex items-center px-4 gap-3 shrink-0">
        <span className="font-semibold text-white text-sm tracking-wide">📸 VIP</span>
        <span className="text-gray-600 text-xs">Visual Intelligence Platform</span>
        <button
          onClick={() => setProfilePickerOpen(true)}
          className="ml-2 h-7 rounded-lg border border-gray-700 bg-gray-900 px-2.5 text-[11px] text-gray-200 hover:border-indigo-500 hover:text-white transition-colors"
          title="Switch profile"
        >
          Profile: {selectedProfile.name}
        </button>

        <div className="relative">
          <button
            onClick={enableDesktopNotifications}
            title={
              desktopNotifyStatus === 'blocked'
                ? 'Notifications blocked — click for instructions'
                : desktopNotifyStatus === 'on'
                  ? 'Notifications on — click to turn off'
                  : 'Enable desktop notifications'
            }
            className={`h-7 rounded-lg border px-2 text-[11px] transition-colors ${
              desktopNotifyStatus === 'on'
                ? 'border-emerald-600 bg-emerald-900/30 text-emerald-300'
                : desktopNotifyStatus === 'blocked'
                  ? 'border-amber-700 bg-amber-900/30 text-amber-400 animate-pulse'
                  : desktopNotifyStatus === 'unavailable'
                    ? 'border-gray-700 bg-gray-900 text-gray-500 cursor-not-allowed'
                    : 'border-gray-700 bg-gray-900 text-gray-300 hover:border-indigo-500 hover:text-white'
            }`}
          >
            {desktopNotifyStatus === 'on' && '🔔 On'}
            {desktopNotifyStatus === 'off' && '🔔 Off'}
            {desktopNotifyStatus === 'blocked' && '🔕 Blocked'}
            {desktopNotifyStatus === 'unavailable' && '🔕 N/A'}
          </button>

          {/* Blocked-help popover */}
          {showNotifyBlockedHelp && (
            <div className="absolute left-0 top-9 z-50 w-80 rounded-xl border border-amber-700 bg-gray-900 shadow-2xl p-4 text-xs text-gray-300">
              <div className="flex justify-between items-start mb-2">
                <p className="font-semibold text-amber-300">Notifications blocked by browser</p>
                <button onClick={() => setShowNotifyBlockedHelp(false)} className="text-gray-500 hover:text-white ml-2">✕</button>
              </div>
              <p className="mb-3 text-gray-400">Your browser has blocked notifications for this site. To allow them:</p>
              <div className="space-y-2">
                <div>
                  <p className="font-medium text-white mb-0.5">Safari</p>
                  <p className="text-gray-400">Safari menu → Settings → Websites → Notifications → find <span className="text-gray-200">localhost</span> → Allow</p>
                </div>
                <div>
                  <p className="font-medium text-white mb-0.5">Chrome / Arc</p>
                  <p className="text-gray-400">Click the 🔒 lock icon in the address bar → Notifications → Allow</p>
                </div>
                <div>
                  <p className="font-medium text-white mb-0.5">Firefox</p>
                  <p className="text-gray-400">Click the shield icon in the address bar → Permissions → Allow Notifications</p>
                </div>
              </div>
              <button
                onClick={enableDesktopNotifications}
                className="mt-3 w-full rounded-lg bg-amber-700 hover:bg-amber-600 py-1.5 text-white font-medium transition-colors"
              >
                Re-check permission
              </button>
            </div>
          )}
        </div>

        <div className="flex-1 max-w-2xl ml-2">
          <div className="relative">
            <input
              value={headerSearchQuery}
              onChange={e => setHeaderSearchQuery(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') runHeaderSearch() }}
              placeholder="Search people, filenames, folders, tags, Florence text (* and ? supported)..."
              className="w-full h-8 rounded-lg border border-gray-700 bg-gray-900 pl-9 pr-20 text-xs text-gray-100 placeholder:text-gray-500 outline-none focus:border-indigo-500"
            />
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500 text-xs">🔎</span>
            <button
              onClick={runHeaderSearch}
              className="absolute right-1.5 top-1/2 -translate-y-1/2 h-6 px-2.5 rounded-md bg-indigo-600 hover:bg-indigo-500 text-[11px] font-medium text-white"
            >
              Search
            </button>
          </div>
        </div>

        {/* Gear icon — opens Admin popup */}
        <button
          onClick={() => setAdminOpen(true)}
          title="Admin & Settings"
          className="w-8 h-8 rounded-lg flex items-center justify-center text-gray-400 hover:text-white hover:bg-gray-800 transition-colors text-base"
        >
          ⚙
        </button>
      </header>

      <div className="flex flex-1 min-h-0 overflow-hidden">
        {/* ── Nav sidebar ── */}
        <aside
          style={{ width: sidebarWidth }}
          className="shrink-0 min-h-0 border-r border-gray-800 py-3 flex flex-col bg-gray-950"
        >
          <div className="min-h-0 overflow-y-auto flex flex-col gap-1">
          <NavGroup label="Browse">
            <NavItem id="assistant" icon="💬" label="Assistant"    active={section === 'assistant' && !filtered} onClick={() => navigate('assistant')} />
            <NavItem id="assistant_v2" icon="🧪" label="Assistant V2" active={section === 'assistant_v2' && !filtered} onClick={() => navigate('assistant_v2')} />
            <NavItem id="people"    icon="👤" label="People"       active={(section === 'people'   || filtered?.backTo === 'people')  } onClick={() => navigate('people')} />
            <NavItem id="places"    icon="📍" label="Places"       active={(section === 'places'   || filtered?.backTo === 'places')  } onClick={() => navigate('places')} />
            <NavItem id="things"    icon="📦" label="Things"       active={(section === 'things'   || filtered?.backTo === 'things')  } onClick={() => navigate('things')} />
            <NavItem id="animals"   icon="🐾" label="Animals"      active={(section === 'animals'  || filtered?.backTo === 'animals') } onClick={() => navigate('animals')} />
            <NavItem id="explicit"  icon="🔞" label="Explicit"     active={(section === 'explicit' || filtered?.backTo === 'explicit')} onClick={() => navigate('explicit')} />
            <NavItem id="tags"      icon="🏷️" label="All Tags"     active={section === 'tags'      && !filtered} onClick={() => navigate('tags')} />
          </NavGroup>

          <NavGroup label="Manage">
            <NavItem id="writeback" icon="💾" label="Write to Files" active={section === 'writeback' && !filtered} onClick={() => navigate('writeback')} />
            <NavItem id="quality"   icon="🎯" label="Quality"       active={section === 'quality'   && !filtered} onClick={() => navigate('quality')} />
          </NavGroup>

          <NavGroup label="Library">
            <div className="p-1">
              {/* All Photos as collapsible header */}
              <div
                className={`group flex items-center pl-1 pr-1 py-1.5 rounded-lg text-sm transition-colors
                  ${section === 'library' && !filtered ? 'bg-indigo-600/80 text-white' : 'text-gray-400 hover:text-white hover:bg-gray-800'}`}
              >
                {/* expand/collapse chevron */}
                <button
                  onClick={e => { e.stopPropagation(); setAllPhotosExpanded(v => !v) }}
                  className="w-5 h-5 shrink-0 flex items-center justify-center text-gray-500 hover:text-white"
                  aria-label={allPhotosExpanded ? 'Collapse' : 'Expand'}
                >
                  {folders.length > 0 ? (allPhotosExpanded ? '▾' : '▸') : <span className="w-4" />}
                </button>

                {/* All Photos label */}
                <button onClick={() => navigate('library')} className="flex-1 flex items-center gap-1.5 min-w-0 text-left px-1">
                  <span className="text-base leading-none shrink-0">📚</span>
                  <span className="truncate">All Photos</span>
                </button>
              </div>

              {/* Expanded content */}
              {allPhotosExpanded && (
                <div className="ml-2 mt-1">
                  {folderLoadError && (
                    <p className="text-[10px] text-red-400 px-3 py-1">Could not load folders — is the server running?</p>
                  )}
                  {!folderLoadError && folders.length === 0 && (
                    <p className="text-[10px] text-gray-600 px-3 py-1 italic">No folders scanned yet</p>
                  )}
                  {folders.map(f => {
                    const rootName = f.folder_path.split('/').pop() || f.folder_path
                    return (
                      <FolderTreeRoot
                        key={f.id}
                        folder={f}
                        subfolders={subfolderMap[f.id] ?? null}
                        activePathPrefix={filtered?.pathPrefix ?? null}
                        activeFolderId={filtered?.folderId ?? null}
                        onRootClick={() => openFiltered({ folder_id: f.id }, `📁 ${rootName}`, 'library', f.id, undefined)}
                        onSubfolderClick={(path, name) =>
                          openFiltered({ path_prefix: path }, `📁 ${name}`, 'library', f.id, path)
                        }
                        onExpand={() => loadSubfolders(f.id)}
                        onRemove={() => handleFolderRemove(f)}
                      />
                    )
                  })}
                </div>
              )}
            </div>
          </NavGroup>
          </div>

          <div className="mt-auto border-t border-gray-800 pt-2 px-2">
            <button
              onClick={() => navigate('dashboard')}
              title="Dashboard"
              className={`w-8 h-8 rounded-lg flex items-center justify-center text-base transition-colors ${
                section === 'dashboard' && !filtered
                  ? 'bg-indigo-600 text-white'
                  : 'text-gray-400 hover:text-white hover:bg-gray-800'
              }`}
            >
              ◳
            </button>
          </div>
        </aside>

        {/* Sidebar resize handle */}
        <div
          className="w-1 shrink-0 -ml-px cursor-col-resize hover:bg-indigo-500/50 active:bg-indigo-500/70 transition-colors z-10"
          onMouseDown={e => onPanelDragStart('sidebar', e)}
          title="Drag to resize sidebar"
        />

        {/* ── Pipeline panel (always mounted, collapsible) ── */}
        <PipelinePanel
          profileId={selectedProfile.id}
          collapsed={pipelineCollapsed}
          onToggle={togglePipeline}
          onPipelineComplete={loadFolders}
          width={pipelineWidth}
        />

        {/* Pipeline resize handle (hidden when panel is collapsed) */}
        {!pipelineCollapsed && (
          <div
            className="w-1 shrink-0 -ml-px cursor-col-resize hover:bg-indigo-500/50 active:bg-indigo-500/70 transition-colors z-10"
            onMouseDown={e => onPanelDragStart('pipeline', e)}
            title="Drag to resize pipeline panel"
          />
        )}

        {/* ── Main content ── */}
        <main key={selectedProfile.id} className="flex-1 min-h-0 overflow-y-auto p-6">
          {mainContent}
        </main>
      </div>

      {/* ── Admin floating popup ── */}
      {adminOpen && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-end bg-black/50 backdrop-blur-sm pt-12 pr-4"
          onClick={e => { if (e.target === e.currentTarget) setAdminOpen(false) }}
        >
          <div className="bg-gray-950 border border-gray-700 rounded-2xl shadow-2xl w-full max-w-2xl max-h-[85vh] overflow-y-auto">
            <div className="flex items-center justify-between px-6 pt-5 pb-3 border-b border-gray-800 sticky top-0 bg-gray-950 z-10">
              <h2 className="text-base font-semibold text-white">Admin &amp; Settings</h2>
              <button
                onClick={() => setAdminOpen(false)}
                className="w-7 h-7 rounded-lg flex items-center justify-center text-gray-400 hover:text-white hover:bg-gray-800 transition-colors text-lg leading-none"
              >
                ✕
              </button>
            </div>
            <div className="px-6 py-4">
              <AdminPage />
            </div>
          </div>
        </div>
      )}

      {/* ── Folder remove warning modal ── */}
      {folderWarning && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-gray-900 border border-amber-700 rounded-2xl p-6 max-w-sm w-full mx-4 shadow-2xl">
            <h3 className="text-white font-semibold text-base mb-2">⚠️ Pending metadata</h3>
            <p className="text-gray-300 text-sm mb-3">
              {folderWarning.result.unwritten_count} photo
              {folderWarning.result.unwritten_count !== 1 ? 's' : ''} in this folder have metadata
              that hasn't been written to file yet. Removing will discard those changes.
            </p>
            {folderWarning.result.unwritten_paths && folderWarning.result.unwritten_paths.length > 0 && (
              <ul className="text-amber-300 text-xs mb-4 space-y-0.5 max-h-24 overflow-y-auto">
                {folderWarning.result.unwritten_paths.map((p, i) => (
                  <li key={i} className="truncate">{p.split('/').pop()}</li>
                ))}
              </ul>
            )}
            <div className="flex gap-3">
              <button
                onClick={() => setFolderWarning(null)}
                className="flex-1 bg-gray-800 hover:bg-gray-700 text-gray-200 text-sm font-medium rounded-lg py-2"
              >
                Cancel
              </button>
              <button
                onClick={() => handleFolderRemove(folderWarning.folder, true)}
                className="flex-1 bg-red-700 hover:bg-red-600 text-white text-sm font-medium rounded-lg py-2"
              >
                Remove anyway
              </button>
            </div>
          </div>
        </div>
      )}

      {folderPaneNameTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-sm w-full shadow-2xl mx-4">
            <p className="text-white font-semibold text-lg mb-1">Name this face</p>
            <p className="text-gray-400 text-sm mb-4">
              Assign this unnamed cluster to an existing or new person.
            </p>
            <input
              autoFocus
              value={folderPaneNameInput}
              onChange={e => setFolderPaneNameInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') void handleFolderPaneConfirmName(false)
                if (e.key === 'Escape') {
                  setFolderPaneNameTarget(null)
                  setFolderPaneMergeCandidate(null)
                }
              }}
              placeholder="Enter name…"
              className="w-full bg-gray-800 border border-indigo-500 rounded-lg px-3 py-2 text-sm text-white outline-none mb-4"
            />

            {folderPaneMergeCandidate && (
              <div className="mb-4 rounded-lg border border-amber-700 bg-amber-900/20 p-3">
                <p className="text-amber-200 text-xs mb-2">
                  "{folderPaneNameInput.trim()}" already exists as {folderPaneMergeCandidate.name}. Same person?
                </p>
                <div className="flex gap-2">
                  <button
                    onClick={async () => {
                      try {
                        setFolderPaneBusyKey(`name-${folderPaneNameTarget.id}`)
                        await api.persons.addCluster(folderPaneMergeCandidate.personId, folderPaneNameTarget.id)
                        setFolderPaneNameTarget(null)
                        setFolderPaneNameInput('')
                        setFolderPaneMergeCandidate(null)
                        await loadFolderPaneFaces()
                      } catch (e: unknown) {
                        setFolderPaneError(e instanceof Error ? e.message : 'Could not merge this cluster')
                      } finally {
                        setFolderPaneBusyKey(null)
                      }
                    }}
                    disabled={!!folderPaneBusyKey}
                    className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-lg py-1.5 text-xs font-medium"
                  >
                    Same — merge
                  </button>
                  <button
                    onClick={() => void handleFolderPaneConfirmName(true)}
                    disabled={!!folderPaneBusyKey}
                    className="flex-1 bg-amber-700 hover:bg-amber-600 disabled:opacity-40 text-white rounded-lg py-1.5 text-xs font-medium"
                  >
                    Force merge
                  </button>
                </div>
              </div>
            )}

            <div className="flex gap-3">
              <button
                onClick={() => void handleFolderPaneConfirmName(false)}
                disabled={!!folderPaneBusyKey || !folderPaneNameInput.trim()}
                className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
              >
                {folderPaneBusyKey?.startsWith('name-') ? 'Saving…' : 'Save'}
              </button>
              <button
                onClick={() => {
                  setFolderPaneNameTarget(null)
                  setFolderPaneMergeCandidate(null)
                }}
                disabled={!!folderPaneBusyKey}
                className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {folderPaneIgnoreTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-sm w-full shadow-2xl mx-4">
            <p className="text-white font-semibold text-lg mb-1">Always ignore this face?</p>
            <p className="text-gray-400 text-sm mb-6">
              This cluster will be moved to ignored faces and removed from unnamed suggestions.
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => void handleFolderPaneConfirmIgnoreCluster()}
                disabled={!!folderPaneBusyKey}
                className="flex-1 bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
              >
                {folderPaneBusyKey?.startsWith('ignore-') ? 'Ignoring…' : 'Always ignore'}
              </button>
              <button
                onClick={() => setFolderPaneIgnoreTarget(null)}
                disabled={!!folderPaneBusyKey}
                className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {folderPaneIgnoreAllOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-md w-full shadow-2xl mx-4">
            <p className="text-white font-semibold text-lg mb-1">Ignore all unnamed faces?</p>
            <p className="text-gray-400 text-sm mb-6">
              This will always ignore all {folderPaneUnnamed.length} unnamed clusters currently shown in this folder scope.
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => void handleFolderPaneConfirmIgnoreAllUnnamed()}
                disabled={!!folderPaneBusyKey || folderPaneUnnamed.length === 0}
                className="flex-1 bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
              >
                {folderPaneBusyKey === 'ignore-all' ? 'Ignoring…' : 'Ignore all'}
              </button>
              <button
                onClick={() => setFolderPaneIgnoreAllOpen(false)}
                disabled={!!folderPaneBusyKey}
                className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {folderPaneBulkNameOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-sm w-full shadow-2xl mx-4">
            <p className="text-white font-semibold text-lg mb-1">Name selected faces</p>
            <p className="text-gray-400 text-sm mb-4">
              Assign one name to {folderPaneSelectedClusterIds.size} selected unnamed clusters.
            </p>
            <input
              autoFocus
              value={folderPaneBulkNameInput}
              onChange={e => setFolderPaneBulkNameInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') void handleFolderPaneBulkName()
                if (e.key === 'Escape') setFolderPaneBulkNameOpen(false)
              }}
              placeholder="Enter name…"
              className="w-full bg-gray-800 border border-indigo-500 rounded-lg px-3 py-2 text-sm text-white outline-none mb-4"
            />
            <div className="flex gap-3">
              <button
                onClick={() => void handleFolderPaneBulkName()}
                disabled={!!folderPaneBusyKey || !folderPaneBulkNameInput.trim() || folderPaneSelectedClusterIds.size === 0}
                className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
              >
                {folderPaneBusyKey === 'bulk-name' ? 'Saving…' : 'Save'}
              </button>
              <button
                onClick={() => setFolderPaneBulkNameOpen(false)}
                disabled={!!folderPaneBusyKey}
                className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {folderPaneBulkIgnoreOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl p-7 max-w-md w-full shadow-2xl mx-4">
            <p className="text-white font-semibold text-lg mb-1">Ignore selected faces?</p>
            <p className="text-gray-400 text-sm mb-6">
              This will always ignore {folderPaneSelectedClusterIds.size} selected unnamed clusters in this folder scope.
            </p>
            <div className="flex gap-3">
              <button
                onClick={() => void handleFolderPaneBulkIgnoreSelected()}
                disabled={!!folderPaneBusyKey || folderPaneSelectedClusterIds.size === 0}
                className="flex-1 bg-red-700 hover:bg-red-600 disabled:opacity-40 text-white rounded-xl py-2.5 text-sm font-semibold transition-colors"
              >
                {folderPaneBusyKey === 'bulk-ignore' ? 'Ignoring…' : 'Ignore selected'}
              </button>
              <button
                onClick={() => setFolderPaneBulkIgnoreOpen(false)}
                disabled={!!folderPaneBusyKey}
                className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-300 rounded-xl py-2.5 text-sm font-medium transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {folderPaneConnections && (
        <ConnectionsGraph
          personId={folderPaneConnections.personId}
          personName={folderPaneConnections.personName}
          onClose={() => setFolderPaneConnections(null)}
          onNavigatePerson={(pid, name) => {
            setFolderPaneConnections(null)
            if (filtered?.folderId || filtered?.pathPrefix) {
              const scoped: MediaFilter = filtered.pathPrefix
                ? { path_prefix: filtered.pathPrefix }
                : (filtered.folderId ? { folder_id: filtered.folderId } : {})
              openFiltered(
                { ...scoped, person_id: pid },
                `👤 ${name}`,
                'library',
                filtered?.folderId,
                filtered?.pathPrefix,
              )
              return
            }
            openFiltered({ person_id: pid }, `👤 ${name}`, 'people')
          }}
        />
      )}

      {/* ── Global notification stack (bottom-right) ── */}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-3 max-w-sm w-full pointer-events-none">

        {/* Quality issues banner */}
        {qualityCount !== null && qualityCount > 0 && (
          <div className="pointer-events-auto bg-orange-950 border border-orange-700 rounded-xl px-4 py-3 shadow-2xl flex items-center gap-3">
            <span className="text-orange-400 text-lg">🔍</span>
            <div className="flex-1 min-w-0">
              <p className="text-white text-sm font-medium">Quality issues found</p>
              <p className="text-orange-300 text-xs">{qualityCount} photo{qualityCount > 1 ? 's' : ''} may be blurry or have closed eyes</p>
            </div>
            <button
              onClick={() => { navigate('quality'); setQualityCount(null) }}
              className="text-xs bg-orange-700 hover:bg-orange-600 text-white rounded-lg px-3 py-1.5 font-medium whitespace-nowrap"
            >
              Review
            </button>
            <button
              onClick={() => setQualityCount(null)}
              className="text-gray-400 hover:text-white text-lg leading-none px-1"
            >
              ×
            </button>
          </div>
        )}

        {/* Merge suggestion card */}
        {mergeSuggestions.length > 0 && (() => {
          const s = mergeSuggestions[0]
          return (
            <div className="pointer-events-auto bg-gray-900 border border-indigo-700 rounded-xl p-4 shadow-2xl">
              <p className="text-white text-sm font-semibold mb-1">Same person?</p>
              <p className="text-gray-400 text-xs mb-3">
                This cluster ({s.member_count} photo{s.member_count > 1 ? 's' : ''}) looks like{' '}
                <span className="text-indigo-300 font-medium">{s.person_name}</span>
                <span className="ml-1 text-gray-500">({Math.round(s.similarity * 100)}% match)</span>
              </p>
              <div className="flex items-center gap-3 mb-4">
                {/* Person face */}
                <div className="w-16 h-16 rounded-xl bg-gray-800 border border-gray-700 overflow-hidden flex items-center justify-center">
                  {s.person_face_id ? (
                    <img src={`/api/faces/${s.person_face_id}/thumbnail`} alt={s.person_name} className="w-full h-full object-cover" onError={e => { (e.target as HTMLImageElement).style.display='none' }} />
                  ) : (
                    <span className="text-gray-500 text-xs text-center leading-tight px-1">{s.person_name}</span>
                  )}
                </div>
                <span className="text-gray-500 text-xl">≈</span>
                {/* Cluster face */}
                <div className="w-16 h-16 rounded-xl bg-gray-800 border border-gray-700 overflow-hidden flex items-center justify-center">
                  {s.cluster_face_id ? (
                    <img src={`/api/faces/${s.cluster_face_id}/thumbnail`} alt="" className="w-full h-full object-cover" onError={e => { (e.target as HTMLImageElement).style.display='none' }} />
                  ) : (
                    <span className="w-full h-full flex items-center justify-center text-gray-600 text-xs">?</span>
                  )}
                </div>
              </div>
              {mergeSuggestions.length > 1 && (
                <p className="text-gray-600 text-[10px] mb-2">+{mergeSuggestions.length - 1} more suggestion{mergeSuggestions.length > 2 ? 's' : ''}</p>
              )}
              <div className="flex gap-2">
                <button
                  onClick={() => handleMergeAction('merge')}
                  disabled={mergeWorking}
                  className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-sm font-medium rounded-lg py-2"
                >
                  Merge
                </button>
                <button
                  onClick={() => handleMergeAction('skip')}
                  disabled={mergeWorking}
                  className="flex-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-200 text-sm font-medium rounded-lg py-2"
                >
                  Different person
                </button>
              </div>
            </div>
          )
        })()}
      </div>

      {profilePickerOpen && (
        <ProfilePickerModal
          profiles={profiles}
          activeProfileId={selectedProfile.id}
          newProfileName={newProfileName}
          newProfilePassword={newProfilePassword}
          newProfilePasswordConfirm={newProfilePasswordConfirm}
          copySettingsFromProfileId={copySettingsFromProfileId}
          creatingProfile={creatingProfile}
          profileActionBusy={profileActionBusy}
          error={profileError}
          onProfileNameChange={setNewProfileName}
          onProfilePasswordChange={setNewProfilePassword}
          onProfilePasswordConfirmChange={setNewProfilePasswordConfirm}
          onCopySettingsFromProfileIdChange={setCopySettingsFromProfileId}
          onSelect={activateProfile}
          onCreate={createAndActivateProfile}
          onRename={renameProfile}
          onDelete={deleteProfile}
          onSetPassword={setProfilePassword}
          onClearPassword={clearProfilePassword}
          onClose={() => setProfilePickerOpen(false)}
        />
      )}
    </div>
  )
}

function ProfilePickerModal({
  profiles,
  activeProfileId,
  newProfileName,
  newProfilePassword,
  newProfilePasswordConfirm,
  copySettingsFromProfileId,
  creatingProfile,
  profileActionBusy,
  error,
  onProfileNameChange,
  onProfilePasswordChange,
  onProfilePasswordConfirmChange,
  onCopySettingsFromProfileIdChange,
  onSelect,
  onCreate,
  onRename,
  onDelete,
  onSetPassword,
  onClearPassword,
  onClose,
}: {
  profiles: ProfileSummary[]
  activeProfileId?: string
  newProfileName: string
  newProfilePassword: string
  newProfilePasswordConfirm: string
  copySettingsFromProfileId: string
  creatingProfile: boolean
  profileActionBusy: boolean
  error: string | null
  onProfileNameChange: (value: string) => void
  onProfilePasswordChange: (value: string) => void
  onProfilePasswordConfirmChange: (value: string) => void
  onCopySettingsFromProfileIdChange: (value: string) => void
  onSelect: (profileId: string, password?: string) => Promise<boolean>
  onCreate: () => void
  onRename: (profileId: string, name: string) => void
  onDelete: (profileId: string) => void
  onSetPassword: (profileId: string, password: string, currentPassword?: string) => Promise<boolean>
  onClearPassword: (profileId: string, currentPassword?: string) => Promise<boolean>
  onClose?: () => void
}) {
  const [passwordDialog, setPasswordDialog] = useState<
    | {
      mode: 'unlock' | 'set' | 'change' | 'remove'
      profile: ProfileSummary
    }
    | null
  >(null)
  const [currentPasswordInput, setCurrentPasswordInput] = useState('')
  const [newPasswordInput, setNewPasswordInput] = useState('')
  const [confirmPasswordInput, setConfirmPasswordInput] = useState('')
  const [passwordDialogError, setPasswordDialogError] = useState<string | null>(null)
  const [showCurrentPassword, setShowCurrentPassword] = useState(false)
  const [showNewPassword, setShowNewPassword] = useState(false)
  const [showConfirmPassword, setShowConfirmPassword] = useState(false)
  const [showCreatePassword, setShowCreatePassword] = useState(false)
  const [showCreatePasswordConfirm, setShowCreatePasswordConfirm] = useState(false)

  function openPasswordDialog(
    mode: 'unlock' | 'set' | 'change' | 'remove',
    profile: ProfileSummary,
  ) {
    setPasswordDialog({ mode, profile })
    setCurrentPasswordInput('')
    setNewPasswordInput('')
    setConfirmPasswordInput('')
    setPasswordDialogError(null)
    setShowCurrentPassword(false)
    setShowNewPassword(false)
    setShowConfirmPassword(false)
  }

  function closePasswordDialog() {
    setPasswordDialog(null)
    setPasswordDialogError(null)
  }

  function validatePasswordInputs(mode: 'unlock' | 'set' | 'change' | 'remove'): string | null {
    const current = currentPasswordInput.trim()
    const next = newPasswordInput.trim()
    const confirm = confirmPasswordInput.trim()

    if ((mode === 'unlock' || mode === 'remove' || mode === 'change') && !current) {
      return 'Current password is required'
    }
    if (mode === 'set' || mode === 'change') {
      if (!next) return 'New password is required'
      if (next.length < 4) return 'Password must be at least 4 characters'
      if (next !== confirm) return 'Password and confirm password must match'
    }
    return null
  }

  async function submitPasswordDialog() {
    if (!passwordDialog) return
    const validationError = validatePasswordInputs(passwordDialog.mode)
    if (validationError) {
      setPasswordDialogError(validationError)
      return
    }

    const current = currentPasswordInput.trim()
    const next = newPasswordInput.trim()

    if (passwordDialog.mode === 'unlock') {
      const ok = await onSelect(passwordDialog.profile.id, current)
      if (ok) closePasswordDialog()
      return
    }
    if (passwordDialog.mode === 'set') {
      const ok = await onSetPassword(passwordDialog.profile.id, next)
      if (ok) closePasswordDialog()
      return
    }
    if (passwordDialog.mode === 'change') {
      const ok = await onSetPassword(passwordDialog.profile.id, next, current)
      if (ok) closePasswordDialog()
      return
    }
    const ok = await onClearPassword(passwordDialog.profile.id, current)
    if (ok) closePasswordDialog()
  }

  const createPasswordError = newProfilePassword.trim() && newProfilePassword !== newProfilePasswordConfirm
    ? 'Password and confirm password must match'
    : null

  function handleSelect(profile: ProfileSummary) {
    if (!profile.is_password_protected) {
      onSelect(profile.id)
      return
    }
    openPasswordDialog('unlock', profile)
  }

  function handleRename(profile: ProfileSummary, event: React.MouseEvent) {
    event.stopPropagation()
    const nextName = window.prompt('Rename profile', profile.name)?.trim()
    if (!nextName || nextName === profile.name) return
    onRename(profile.id, nextName)
  }

  function handleSetPassword(profile: ProfileSummary, event: React.MouseEvent) {
    event.stopPropagation()
    openPasswordDialog(profile.is_password_protected ? 'change' : 'set', profile)
  }

  function handleClearPassword(profile: ProfileSummary, event: React.MouseEvent) {
    event.stopPropagation()
    if (!profile.is_password_protected) return
    openPasswordDialog('remove', profile)
  }

  function handleDelete(profile: ProfileSummary, event: React.MouseEvent) {
    event.stopPropagation()
    if (profile.is_default) return
    const confirmed = window.confirm(
      `Delete profile "${profile.name}"? This permanently removes all data in that profile.`
    )
    if (!confirmed) return
    onDelete(profile.id)
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
      <div className="w-full max-w-2xl rounded-2xl border border-gray-800 bg-gray-950 shadow-2xl overflow-hidden">
        <div className="flex items-center justify-between border-b border-gray-800 px-6 py-4">
          <div>
            <h2 className="text-lg font-semibold text-white">Choose Profile</h2>
            <p className="text-sm text-gray-400">Each profile has its own database, thumbnails, people, and write queue.</p>
          </div>
          {onClose && (
            <button
              onClick={onClose}
              className="w-8 h-8 rounded-lg flex items-center justify-center text-gray-400 hover:text-white hover:bg-gray-800"
            >
              ✕
            </button>
          )}
        </div>

        <div className="p-6 grid gap-6 md:grid-cols-[1.2fr_0.8fr]">
          <div>
            <p className="text-xs font-semibold uppercase tracking-widest text-gray-500 mb-3">Existing profiles</p>
            <div className="space-y-2 max-h-80 overflow-y-auto">
              {profiles.map(profile => (
                <button
                  key={profile.id}
                  onClick={() => handleSelect(profile)}
                  disabled={profileActionBusy}
                  className={`w-full rounded-xl border px-4 py-3 text-left transition-colors ${
                    activeProfileId === profile.id
                      ? 'border-indigo-500 bg-indigo-500/10'
                      : 'border-gray-800 bg-gray-900 hover:border-gray-700'
                  }`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <span className="font-medium text-white">{profile.name}</span>
                    <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-gray-500 shrink-0">
                      {profile.is_default && <span>Default</span>}
                      {profile.is_password_protected && <span>Protected</span>}
                      {activeProfileId === profile.id && <span className="text-indigo-300">Active</span>}
                    </div>
                  </div>
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={(e) => handleRename(profile, e)}
                      disabled={profileActionBusy}
                      className="rounded-md border border-gray-700 px-2 py-1 text-[11px] text-gray-300 hover:bg-gray-800 disabled:opacity-40"
                    >
                      Rename
                    </button>
                    <button
                      onClick={(e) => handleSetPassword(profile, e)}
                      disabled={profileActionBusy}
                      className="rounded-md border border-gray-700 px-2 py-1 text-[11px] text-gray-300 hover:bg-gray-800 disabled:opacity-40"
                    >
                      {profile.is_password_protected ? 'Change Password' : 'Add Password'}
                    </button>
                    {profile.is_password_protected && (
                      <button
                        onClick={(e) => handleClearPassword(profile, e)}
                        disabled={profileActionBusy}
                        className="rounded-md border border-amber-800 px-2 py-1 text-[11px] text-amber-300 hover:bg-amber-900/30 disabled:opacity-40"
                      >
                        Remove Password
                      </button>
                    )}
                    {!profile.is_default && (
                      <button
                        onClick={(e) => handleDelete(profile, e)}
                        disabled={profileActionBusy}
                        className="rounded-md border border-red-800 px-2 py-1 text-[11px] text-red-300 hover:bg-red-900/30 disabled:opacity-40"
                      >
                        Delete
                      </button>
                    )}
                  </div>
                  <p className="mt-1 text-xs text-gray-500">Created {new Date(profile.created_at).toLocaleString()}</p>
                </button>
              ))}
            </div>
          </div>

          <div>
            <p className="text-xs font-semibold uppercase tracking-widest text-gray-500 mb-3">Create new profile</p>
            <div className="rounded-xl border border-gray-800 bg-gray-900 p-4 space-y-3">
              <input
                value={newProfileName}
                onChange={e => onProfileNameChange(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') onCreate() }}
                placeholder="e.g. Family Archive"
                className="w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500"
              />
              <PasswordField
                value={newProfilePassword}
                onChange={onProfilePasswordChange}
                onKeyDownEnter={onCreate}
                placeholder="Optional profile password"
                show={showCreatePassword}
                onToggleShow={() => setShowCreatePassword(v => !v)}
              />
              {newProfilePassword.trim() && (
                <PasswordField
                  value={newProfilePasswordConfirm}
                  onChange={onProfilePasswordConfirmChange}
                  onKeyDownEnter={onCreate}
                  placeholder="Confirm profile password"
                  show={showCreatePasswordConfirm}
                  onToggleShow={() => setShowCreatePasswordConfirm(v => !v)}
                />
              )}
              <select
                value={copySettingsFromProfileId}
                onChange={e => onCopySettingsFromProfileIdChange(e.target.value)}
                className="w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-white outline-none focus:border-indigo-500"
              >
                <option value="">Start with default settings</option>
                {profiles.map(profile => (
                  <option key={profile.id} value={profile.id}>{profile.name}</option>
                ))}
              </select>
              <button
                onClick={onCreate}
                disabled={creatingProfile || profileActionBusy || !newProfileName.trim() || !!createPasswordError}
                className="w-full rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-40"
              >
                {creatingProfile ? 'Creating…' : 'Create and open'}
              </button>
              <p className="text-xs text-gray-500">A new profile starts empty unless you copy admin settings from an existing profile.</p>
              {createPasswordError && <p className="text-xs text-red-400">{createPasswordError}</p>}
              {error && <p className="text-xs text-red-400">{error}</p>}
            </div>
          </div>
        </div>

        {passwordDialog && (
          <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
            <div className="w-full max-w-md rounded-2xl border border-gray-800 bg-gray-950 shadow-2xl overflow-hidden">
              <div className="flex items-center justify-between border-b border-gray-800 px-5 py-4">
                <div>
                  <h3 className="text-base font-semibold text-white">
                    {passwordDialog.mode === 'unlock' && 'Unlock Profile'}
                    {passwordDialog.mode === 'set' && 'Add Profile Password'}
                    {passwordDialog.mode === 'change' && 'Change Profile Password'}
                    {passwordDialog.mode === 'remove' && 'Remove Profile Password'}
                  </h3>
                  <p className="text-xs text-gray-400 mt-0.5">{passwordDialog.profile.name}</p>
                </div>
                <button
                  onClick={closePasswordDialog}
                  className="w-8 h-8 rounded-lg flex items-center justify-center text-gray-400 hover:text-white hover:bg-gray-800"
                >
                  ✕
                </button>
              </div>

              <div className="px-5 py-4 space-y-3">
                {(passwordDialog.mode === 'unlock' || passwordDialog.mode === 'change' || passwordDialog.mode === 'remove') && (
                  <PasswordField
                    value={currentPasswordInput}
                    onChange={setCurrentPasswordInput}
                    onKeyDownEnter={submitPasswordDialog}
                    placeholder={passwordDialog.mode === 'unlock' ? 'Enter password' : 'Current password'}
                    show={showCurrentPassword}
                    onToggleShow={() => setShowCurrentPassword(v => !v)}
                    autoFocus
                  />
                )}

                {(passwordDialog.mode === 'set' || passwordDialog.mode === 'change') && (
                  <>
                    <PasswordField
                      value={newPasswordInput}
                      onChange={setNewPasswordInput}
                      onKeyDownEnter={submitPasswordDialog}
                      placeholder="New password"
                      show={showNewPassword}
                      onToggleShow={() => setShowNewPassword(v => !v)}
                      autoFocus={passwordDialog.mode === 'set'}
                    />
                    <PasswordField
                      value={confirmPasswordInput}
                      onChange={setConfirmPasswordInput}
                      onKeyDownEnter={submitPasswordDialog}
                      placeholder="Confirm new password"
                      show={showConfirmPassword}
                      onToggleShow={() => setShowConfirmPassword(v => !v)}
                    />
                  </>
                )}

                {passwordDialogError && <p className="text-xs text-red-400">{passwordDialogError}</p>}

                <div className="pt-1 flex gap-2">
                  <button
                    onClick={closePasswordDialog}
                    className="flex-1 rounded-lg border border-gray-700 bg-gray-900 px-4 py-2 text-sm text-gray-200 hover:bg-gray-800"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={submitPasswordDialog}
                    className="flex-1 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500"
                  >
                    {passwordDialog.mode === 'unlock' && 'Unlock'}
                    {passwordDialog.mode === 'set' && 'Set Password'}
                    {passwordDialog.mode === 'change' && 'Update Password'}
                    {passwordDialog.mode === 'remove' && 'Remove Password'}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function PasswordField({
  value,
  onChange,
  onKeyDownEnter,
  placeholder,
  show,
  onToggleShow,
  autoFocus,
}: {
  value: string
  onChange: (value: string) => void
  onKeyDownEnter: () => void
  placeholder: string
  show: boolean
  onToggleShow: () => void
  autoFocus?: boolean
}) {
  return (
    <div className="relative">
      <input
        type={show ? 'text' : 'password'}
        value={value}
        onChange={e => onChange(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') onKeyDownEnter() }}
        placeholder={placeholder}
        autoFocus={autoFocus}
        className="w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 pr-10 text-sm text-white outline-none focus:border-indigo-500"
      />
      <button
        type="button"
        onClick={onToggleShow}
        className="absolute inset-y-0 right-0 w-10 flex items-center justify-center text-gray-400 hover:text-white"
        aria-label={show ? 'Hide password' : 'Show password'}
      >
        {show ? (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M3 3L21 21" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
            <path d="M10.58 10.58A2 2 0 0 0 13.42 13.42" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
            <path d="M9.88 5.09A10.94 10.94 0 0 1 12 4.9C16.2 4.9 19.7 7.48 21 11.1C20.51 12.47 19.71 13.68 18.68 14.65" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
            <path d="M6.38 6.39C4.45 7.5 2.95 9.11 2 11.1C3.3 14.72 6.8 17.3 11 17.3C12.02 17.3 12.99 17.15 13.9 16.87" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
          </svg>
        ) : (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M2 12C3.3 8.38 6.8 5.8 11 5.8C15.2 5.8 18.7 8.38 20 12C18.7 15.62 15.2 18.2 11 18.2C6.8 18.2 3.3 15.62 2 12Z" stroke="currentColor" strokeWidth="1.8"/>
            <circle cx="11" cy="12" r="2.6" stroke="currentColor" strokeWidth="1.8"/>
          </svg>
        )}
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sidebar sub-components
// ---------------------------------------------------------------------------

// ── Folder tree ──────────────────────────────────────────────────────────────

/** One node in the flat subfolder list from the API */
interface TreeNode {
  path: string
  name: string
  photo_count: number
  pending_writeback_count: number
  children: TreeNode[]
}

/** Build a nested tree from the flat list returned by the backend. */
function buildTree(root: string, items: SubfolderItem[]): TreeNode[] {
  // items are sorted by path so parents always precede children
  const nodeMap = new Map<string, TreeNode>()
  const roots: TreeNode[] = []

  for (const item of items) {
    const node: TreeNode = {
      path: item.path,
      name: item.name,
      photo_count: item.photo_count,
      pending_writeback_count: item.pending_writeback_count ?? 0,
      children: [],
    }
    nodeMap.set(item.path, node)
    const parentPath = item.path.substring(0, item.path.lastIndexOf('/'))
    const parent = parentPath === root ? null : nodeMap.get(parentPath)
    if (parent) {
      parent.children.push(node)
    } else {
      roots.push(node)
    }
  }
  return roots
}

/** Recursive node in the tree */
function FolderTreeNode({
  node,
  depth,
  activePathPrefix,
  onSubfolderClick,
}: {
  node: TreeNode
  depth: number
  activePathPrefix: string | null
  onSubfolderClick: (path: string, name: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const hasChildren = node.children.length > 0
  const isActive = activePathPrefix === node.path
  const indentPx = 12 + depth * 12

  return (
    <div>
      <div
        style={{ paddingLeft: indentPx }}
        className={`group flex items-center pr-1 py-1 rounded-lg text-xs transition-colors cursor-pointer
          ${isActive ? 'bg-indigo-600/80 text-white' : 'text-gray-400 hover:text-white hover:bg-gray-800'}`}
      >
        {/* chevron / spacer */}
        <button
          onClick={e => { e.stopPropagation(); setExpanded(v => !v) }}
          className="w-4 h-4 shrink-0 flex items-center justify-center text-gray-500 hover:text-white mr-1"
          aria-label={expanded ? 'Collapse' : 'Expand'}
        >
          {hasChildren ? (expanded ? '▾' : '▸') : ''}
        </button>

        {/* name + count */}
        <button
          onClick={() => onSubfolderClick(node.path, node.name)}
          className="flex-1 flex items-center gap-1.5 min-w-0 text-left"
        >
          <span className="shrink-0">📁</span>
          <span className="truncate">{node.name}</span>
          {node.pending_writeback_count > 0 && (
            <span className={`shrink-0 rounded px-1.5 py-0.5 text-[9px] font-medium ${isActive ? 'bg-amber-200/20 text-amber-100' : 'bg-amber-900/40 text-amber-300'}`}>
              {node.pending_writeback_count} pending
            </span>
          )}
          {node.photo_count > 0 && (
            <span className={`ml-auto pl-1 shrink-0 text-[10px] ${isActive ? 'text-indigo-200' : 'text-gray-600'}`}>
              {node.photo_count}
            </span>
          )}
        </button>
      </div>

      {expanded && hasChildren && (
        <div>
          {node.children.map(child => (
            <FolderTreeNode
              key={child.path}
              node={child}
              depth={depth + 1}
              activePathPrefix={activePathPrefix}
              onSubfolderClick={onSubfolderClick}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/** Top-level scanned folder row — always visible, expands to reveal tree */
function FolderTreeRoot({
  folder,
  subfolders,
  activePathPrefix,
  activeFolderId,
  onRootClick,
  onSubfolderClick,
  onExpand,
  onRemove,
}: {
  folder: FolderItem
  subfolders: SubfolderItem[] | null   // null = not loaded yet
  activePathPrefix: string | null
  activeFolderId: number | null
  onRootClick: () => void
  onSubfolderClick: (path: string, name: string) => void
  onExpand: () => void
  onRemove: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const name = folder.folder_path.split('/').pop() || folder.folder_path
  const isRootActive = activeFolderId === folder.id && !activePathPrefix

  function toggle() {
    const next = !expanded
    setExpanded(next)
    if (next && subfolders === null) {
      onExpand()
    }
  }

  const tree = subfolders ? buildTree(folder.folder_path, subfolders) : []
  const hasChildren = subfolders === null || subfolders.length > 0

  return (
    <div>
      <div
        className={`group flex items-center pl-1 pr-1 py-1.5 rounded-lg text-sm transition-colors
          ${isRootActive ? 'bg-indigo-600/80 text-white' : 'text-gray-400 hover:text-white hover:bg-gray-800'}`}
      >
        {/* expand/collapse chevron */}
        <button
          onClick={e => { e.stopPropagation(); toggle() }}
          className="w-5 h-5 shrink-0 flex items-center justify-center text-gray-500 hover:text-white"
          aria-label={expanded ? 'Collapse' : 'Expand'}
        >
          {hasChildren ? (expanded ? '▾' : '▸') : <span className="w-4" />}
        </button>

        {/* folder label */}
        <button onClick={onRootClick} className="flex-1 flex items-center gap-1.5 min-w-0 text-left px-1">
          <span className="text-base leading-none shrink-0">📁</span>
          <span className="truncate">{name}</span>
          {folder.pending_writeback_count > 0 && (
            <span className={`shrink-0 rounded px-1.5 py-0.5 text-[9px] font-medium ${isRootActive ? 'bg-amber-200/20 text-amber-100' : 'bg-amber-900/40 text-amber-300'}`}>
              {folder.pending_writeback_count} pending
            </span>
          )}
          {folder.active_count > 0 && (
            <span className={`ml-auto pl-1 shrink-0 text-[10px] ${isRootActive ? 'text-indigo-200' : 'text-gray-600'}`}>
              {folder.active_count}
            </span>
          )}
        </button>

        {/* remove button */}
        <button
          onClick={e => { e.stopPropagation(); onRemove() }}
          title="Remove folder from app"
          className="shrink-0 w-6 h-6 flex items-center justify-center rounded text-gray-500 hover:text-white hover:bg-red-600 transition-colors"
        >
          ✕
        </button>
      </div>

      {/* Subtree */}
      {expanded && (
        <div className="ml-2">
          {subfolders === null ? (
            <p className="text-[10px] text-gray-600 px-4 py-1">Loading…</p>
          ) : tree.length === 0 ? (
            <p className="text-[10px] text-gray-600 px-4 py-1 italic">No subfolders</p>
          ) : (
            tree.map(node => (
              <FolderTreeNode
                key={node.path}
                node={node}
                depth={0}
                activePathPrefix={activePathPrefix}
                onSubfolderClick={onSubfolderClick}
              />
            ))
          )}
        </div>
      )}
    </div>
  )
}



function NavGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="px-2 pb-1">
      <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-widest px-2 py-1.5">{label}</p>
      {children}
    </div>
  )
}

function NavItem({ id, icon, label, active, onClick }: {
  id: string; icon: string; label: string; active: boolean; onClick: () => void
}) {
  return (
    <button
      key={id}
      onClick={onClick}
      className={`w-full flex items-center gap-2.5 px-3 py-1.5 rounded-lg text-sm transition-colors text-left
        ${active
          ? 'bg-indigo-600/80 text-white'
          : 'text-gray-400 hover:text-white hover:bg-gray-800'}`}
    >
      <span className="text-base leading-none">{icon}</span>
      <span className="truncate">{label}</span>
    </button>
  )
}

