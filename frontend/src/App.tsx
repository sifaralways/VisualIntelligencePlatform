import { useState, useEffect, useRef, useCallback } from 'react'
import LibraryPage from './pages/LibraryPage'
import PeoplePage from './pages/PeoplePage'
import DiscoverPage from './pages/DiscoverPage'
import TagsPage from './pages/TagsPage'
import WritebackPage from './pages/WritebackPage'
import AdminPage from './pages/AdminPage'
import QualityPage from './pages/QualityPage'
import ExplicitPage from './pages/ExplicitPage'
import PhotoGrid from './components/PhotoGrid'
import PipelinePanel from './components/PipelinePanel'
import { api } from './api/client'
import type { MediaFilter, WsEvent, MergeSuggestionItem, FolderItem, SubfolderItem, RemoveResult } from './api/client'
import './index.css'

// ---------------------------------------------------------------------------
// View state machine
// ---------------------------------------------------------------------------

type SidebarSection = 'library' | 'people' | 'animals' | 'places' | 'things' | 'tags' | 'writeback' | 'quality' | 'explicit'

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
  const [section, setSection]       = useState<SidebarSection>('library')
  const [filtered, setFiltered]     = useState<FilteredView | null>(null)

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
  const [folderWarning, setFolderWarning] = useState<{
    folder: FolderItem
    result: RemoveResult
  } | null>(null)
  // Subfolders keyed by scanned folder id — loaded lazily on expand
  const [subfolderMap, setSubfolderMap] = useState<Record<number, SubfolderItem[]>>({})

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

  // ── Global pipeline notifications ─────────────────────────────────────────
  const wsRef = useRef<WebSocket | null>(null)
  const [qualityCount,     setQualityCount]     = useState<number | null>(null)
  const [mergeSuggestions, setMergeSuggestions] = useState<MergeSuggestionItem[]>([])
  const [mergeWorking,     setMergeWorking]     = useState(false)

  useEffect(() => {
    loadFolders()
    function connect() {
      const ws = new WebSocket('ws://localhost:7474/ws/progress')
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
          }
          if (ev.event === 'pipeline_complete') {
            loadFolders()
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
  }, [])

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
    setFiltered({ filter, title, backTo, folderId, pathPrefix })
  }

  /** Go back from filtered view to the discover/people section */
  function closeFiltered() {
    setFiltered(null)
  }

  /** Switch sidebar section — clears any filtered sub-view */
  function navigate(s: SidebarSection) {
    setSection(s)
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

  // ── Main content ──────────────────────────────────────────────────────────

  let mainContent: React.ReactNode

  if (filtered) {
    mainContent = (
      <div className="flex flex-col gap-4 h-full">
        <button
          onClick={closeFiltered}
          className="self-start flex items-center gap-1.5 text-sm text-gray-400 hover:text-white transition-colors"
        >
          ← Back
        </button>
        <PhotoGrid filter={filtered.filter} title={filtered.title} selectable enableReprocess />
      </div>
    )
  } else {
    switch (section) {
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
    <div className="min-h-screen bg-gray-950 text-gray-100 flex flex-col">
      {/* ── Top bar ── */}
      <header className="h-11 border-b border-gray-800 flex items-center px-4 gap-3 shrink-0">
        <span className="font-semibold text-white text-sm tracking-wide">📸 VIP</span>
        <span className="text-gray-600 text-xs">Visual Intelligence Platform</span>
        <div className="flex-1" />
        {/* Gear icon — opens Admin popup */}
        <button
          onClick={() => setAdminOpen(true)}
          title="Admin & Settings"
          className="w-8 h-8 rounded-lg flex items-center justify-center text-gray-400 hover:text-white hover:bg-gray-800 transition-colors text-base"
        >
          ⚙
        </button>
      </header>

      <div className="flex flex-1 overflow-hidden">
        {/* ── Nav sidebar ── */}
        <aside
          style={{ width: sidebarWidth }}
          className="shrink-0 border-r border-gray-800 py-3 flex flex-col gap-1 overflow-y-auto bg-gray-950"
        >
          <NavGroup label="Library">
            <NavItem id="library" icon="📚" label="All Photos" active={section === 'library' && !filtered} onClick={() => navigate('library')} />
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
          </NavGroup>

          <NavGroup label="People & Places">
            <NavItem id="people"    icon="👤" label="People"       active={(section === 'people'   || filtered?.backTo === 'people')  } onClick={() => navigate('people')} />
            <NavItem id="animals"   icon="🐾" label="Animals"      active={(section === 'animals'  || filtered?.backTo === 'animals') } onClick={() => navigate('animals')} />
            <NavItem id="places"    icon="📍" label="Places"       active={(section === 'places'   || filtered?.backTo === 'places')  } onClick={() => navigate('places')} />
            <NavItem id="things"    icon="📦" label="Things"       active={(section === 'things'   || filtered?.backTo === 'things')  } onClick={() => navigate('things')} />
            <NavItem id="explicit"  icon="🔞" label="Explicit"     active={(section === 'explicit' || filtered?.backTo === 'explicit')} onClick={() => navigate('explicit')} />
            <NavItem id="tags"      icon="🏷️" label="All Tags"     active={section === 'tags'      && !filtered} onClick={() => navigate('tags')} />
          </NavGroup>

          <NavGroup label="Tools">
            <NavItem id="writeback" icon="💾" label="Write to Files" active={section === 'writeback' && !filtered} onClick={() => navigate('writeback')} />
            <NavItem id="quality"   icon="🎯" label="Quality"       active={section === 'quality'   && !filtered} onClick={() => navigate('quality')} />
          </NavGroup>
        </aside>

        {/* Sidebar resize handle */}
        <div
          className="w-1 shrink-0 -ml-px cursor-col-resize hover:bg-indigo-500/50 active:bg-indigo-500/70 transition-colors z-10"
          onMouseDown={e => onPanelDragStart('sidebar', e)}
          title="Drag to resize sidebar"
        />

        {/* ── Pipeline panel (always mounted, collapsible) ── */}
        <PipelinePanel
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
        <main className="flex-1 overflow-y-auto p-6">
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
  children: TreeNode[]
}

/** Build a nested tree from the flat list returned by the backend. */
function buildTree(root: string, items: SubfolderItem[]): TreeNode[] {
  // items are sorted by path so parents always precede children
  const nodeMap = new Map<string, TreeNode>()
  const roots: TreeNode[] = []

  for (const item of items) {
    const node: TreeNode = { path: item.path, name: item.name, photo_count: item.photo_count, children: [] }
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

