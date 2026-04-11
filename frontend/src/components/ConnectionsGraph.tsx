/**
 * ConnectionsGraph — full-screen force-directed social graph.
 *
 * - Full-screen modal with no inset margins.
 * - Depth level 1-4 controls how many hops of connections are visible;
 *   higher level = more connections (zoom out), lower = fewer (zoom in).
 * - Mouse drag to pan; scroll wheel to visually zoom.
 * - Hover any node → edit pencil appears → click to name/rename.
 * - Stats panel bottom-left: named/unnamed/depth counts.
 * - Clicking a named person recentres the graph on them.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { ConnectionGraph, ConnectionGraphNode, ConnectionGraphEdge, Person, MediaFile } from '../api/client'

// ── Props ──────────────────────────────────────────────────────────────────

interface Props {
  personId: number
  personName: string
  onClose: () => void
  onNavigatePerson?: (personId: number, name: string) => void
}

// ── Simulation node type ───────────────────────────────────────────────────

interface SimNode extends ConnectionGraphNode {
  x: number
  y: number
  vx: number
  vy: number
  pinned: boolean
}

// ── Layout constants ───────────────────────────────────────────────────────

const W = 1600
const H = 900
const CX = W / 2
const CY = H / 2
const RING_R  = [0, 260, 500] as const
const NODE_R  = [48, 30, 20]  as const

const DEPTH_LABELS: Record<number, string> = {
  1: 'Close', 2: 'Direct', 3: 'Extended', 4: 'All',
}

type LayoutMode = 'force' | 'tree-h' | 'tree-v' | 'circular'

const LAYOUT_LABELS: Record<LayoutMode, string> = {
  'force':    '⊛ Force',
  'tree-h':   '⊢ Tree H',
  'tree-v':   '⊤ Tree V',
  'circular': '◎ Radial',
}

// ── Helpers ────────────────────────────────────────────────────────────────

function thumbUrl(path: string | null): string | null {
  if (!path) return null
  const part = path.split('/thumbnails/').pop()
  return part ? '/thumbnails/' + part : null
}

function nodeRadius(depth: number): number {
  return NODE_R[Math.min(depth, 2)]
}

function computeVisibleNodeIds(graph: ConnectionGraph, depthLevel: number): Set<string> {
  const visible = new Set<string>()
  visible.add(graph.center_id)

  // Depth-1 nodes sorted by edge weight to centre
  const centerEdges = graph.edges
    .filter(e => e.source === graph.center_id || e.target === graph.center_id)
    .sort((a, b) => b.weight - a.weight)

  const d1Ids: string[] = []
  for (const edge of centerEdges) {
    const nid = edge.source === graph.center_id ? edge.target : edge.source
    const node = graph.nodes.find(n => n.id === nid)
    if (node && node.depth === 1) d1Ids.push(nid)
  }
  // Catch any depth-1 nodes not linked directly to centre edge list
  for (const node of graph.nodes) {
    if (node.depth === 1 && !d1Ids.includes(node.id)) d1Ids.push(node.id)
  }

  const maxD1 = depthLevel === 1 ? 4 : Infinity
  d1Ids.slice(0, maxD1).forEach(id => visible.add(id))

  if (depthLevel >= 3) {
    const d2Nodes = graph.nodes.filter(n => n.depth === 2)
    const maxD2 = depthLevel === 3 ? 10 : Infinity
    const scored = d2Nodes
      .map(n => ({
        id: n.id,
        score: graph.edges
          .filter(e =>
            (e.source === n.id && visible.has(e.target)) ||
            (e.target === n.id && visible.has(e.source))
          )
          .reduce((s, e) => s + e.weight, 0),
      }))
      .sort((a, b) => b.score - a.score)
    scored.slice(0, maxD2).forEach(({ id }) => visible.add(id))
  }

  return visible
}

// ── Static layout calculators ────────────────────────────────────────────

function computeTreeHPositions(nodes: ConnectionGraphNode[], centerId: string): Map<string, {x: number; y: number}> {
  const pos = new Map<string, {x: number; y: number}>()
  const d1 = nodes.filter(n => n.depth === 1)
  const d2 = nodes.filter(n => n.depth === 2)
  pos.set(centerId, { x: 160, y: CY })
  // Depth-1: vertical stack at x=500
  d1.forEach((n, i) => {
    const y = CY + (i - (d1.length - 1) / 2) * (Math.min(120, (H - 120) / Math.max(d1.length, 1)))
    pos.set(n.id, { x: 500, y })
  })
  // Depth-2: evenly spaced column at x=840
  const stepD2 = Math.min(80, (H - 120) / Math.max(d2.length, 1))
  d2.forEach((n, i) => {
    pos.set(n.id, { x: 840, y: 80 + i * stepD2 })
  })
  return pos
}

function computeTreeVPositions(nodes: ConnectionGraphNode[], centerId: string): Map<string, {x: number; y: number}> {
  const pos = new Map<string, {x: number; y: number}>()
  const d1 = nodes.filter(n => n.depth === 1)
  const d2 = nodes.filter(n => n.depth === 2)
  // Centre at top
  pos.set(centerId, { x: CX, y: 120 })
  // Depth-1: horizontal row at y=340
  const stepD1 = Math.min(180, (W - 120) / Math.max(d1.length, 1))
  d1.forEach((n, i) => {
    const x = CX + (i - (d1.length - 1) / 2) * stepD1
    pos.set(n.id, { x, y: 340 })
  })
  // Depth-2: row at y=580
  const stepD2 = Math.min(120, (W - 80) / Math.max(d2.length, 1))
  d2.forEach((n, i) => {
    const x = 60 + i * stepD2
    pos.set(n.id, { x, y: 580 })
  })
  return pos
}

function computeCircularPositions(nodes: ConnectionGraphNode[], centerId: string): Map<string, {x: number; y: number}> {
  const pos = new Map<string, {x: number; y: number}>()
  pos.set(centerId, { x: CX, y: CY })
  const byDepth: ConnectionGraphNode[][] = [[], []]
  nodes.filter(n => n.id !== centerId).forEach(n => byDepth[Math.min(n.depth - 1, 1)].push(n))
  ;[byDepth[0], byDepth[1]].forEach((ring, di) => {
    const r = di === 0 ? 230 : 450
    ring.forEach((n, i) => {
      const angle = (2 * Math.PI * i) / (ring.length || 1) - Math.PI / 2
      pos.set(n.id, { x: CX + r * Math.cos(angle), y: CY + r * Math.sin(angle) })
    })
  })
  return pos
}

// ── Physics ────────────────────────────────────────────────────────────────

function radialInit(nodes: ConnectionGraphNode[], centerId: string): SimNode[] {
  const byDepth: ConnectionGraphNode[][] = [[], [], []]
  for (const n of nodes) byDepth[Math.min(n.depth, 2)].push(n)
  return nodes.map(n => {
    const d     = Math.min(n.depth, 2)
    const ring  = byDepth[d]
    const idx   = ring.indexOf(n)
    const angle = (2 * Math.PI * idx) / (ring.length || 1) - Math.PI / 2
    const r     = RING_R[d]
    return {
      ...n,
      x: CX + r * Math.cos(angle) + (Math.random() - 0.5) * (d > 0 ? 20 : 0),
      y: CY + r * Math.sin(angle) + (Math.random() - 0.5) * (d > 0 ? 20 : 0),
      vx: 0, vy: 0,
      pinned: n.id === centerId,
    }
  })
}

function tick(nodes: SimNode[], edgeMap: Map<string, number>, alpha: number) {
  const nm = new Map(nodes.map(n => [n.id, n]))

  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j]
      const dx = b.x - a.x, dy = b.y - a.y
      const d2 = dx * dx + dy * dy + 1
      const d  = Math.sqrt(d2)
      const f  = (9000 / d2) * alpha
      const fx = f * dx / d, fy = f * dy / d
      if (!a.pinned) { a.vx -= fx; a.vy -= fy }
      if (!b.pinned) { b.vx += fx; b.vy += fy }
    }
  }

  for (const [key, weight] of edgeMap) {
    const [sid, tid] = key.split('||')
    const a = nm.get(sid), b = nm.get(tid)
    if (!a || !b) continue
    const dx = b.x - a.x, dy = b.y - a.y
    const d  = Math.sqrt(dx * dx + dy * dy) || 1
    const tl = 240 - Math.log1p(weight) * 14
    const f  = (d - tl) * 0.04 * alpha
    const fx = f * dx / d, fy = f * dy / d
    if (!a.pinned) { a.vx += fx; a.vy += fy }
    if (!b.pinned) { b.vx -= fx; b.vy -= fy }
  }

  for (const n of nodes) {
    if (n.pinned) continue
    n.vx += (CX - n.x) * 0.006 * alpha
    n.vy += (CY - n.y) * 0.006 * alpha
  }

  for (const n of nodes) {
    if (n.pinned) { n.x = CX; n.y = CY; n.vx = 0; n.vy = 0; continue }
    n.vx *= 0.82; n.vy *= 0.82
    n.x = Math.max(55, Math.min(W - 55, n.x + n.vx))
    n.y = Math.max(55, Math.min(H - 55, n.y + n.vy))
  }
}

// ── Component ──────────────────────────────────────────────────────────────

interface HistoryEntry { id: number; name: string }

export default function ConnectionsGraph({ personId, personName, onClose, onNavigatePerson }: Props) {

  const [graph,       setGraph]       = useState<ConnectionGraph | null>(null)
  const [loading,     setLoading]     = useState(true)
  const [error,       setError]       = useState(false)
  const [depthLevel,  setDepthLevel]  = useState(3)
  const [layoutMode,  setLayoutMode]  = useState<LayoutMode>('force')
  const [currentPid,  setCurrentPid]  = useState(personId)
  const [currentName, setCurrentName] = useState(personName)
  const [history,     setHistory]     = useState<HistoryEntry[]>([])
  const [hoveredId,   setHoveredId]   = useState<string | null>(null)
  const [nodeFilter,  setNodeFilter]  = useState<'all' | 'named' | 'unnamed'>('all')

  // Edit panel
  const [editingNode,        setEditingNode]        = useState<SimNode | null>(null)
  const [editPos,            setEditPos]            = useState<{ x: number; y: number } | null>(null)
  const [editName,           setEditName]           = useState('')
  const [editSaving,         setEditSaving]         = useState(false)
  const [editError,          setEditError]          = useState('')
  const [editShowSuggestions,setEditShowSuggestions]= useState(false)
  const [mergeCandidate,     setMergeCandidate]     = useState<{ personId: number; name: string } | null>(null)

  // All named persons — loaded on mount for autocomplete + merge detection
  const [allPersons, setAllPersons] = useState<Person[]>([])
  useEffect(() => {
    api.persons.list().then(setAllPersons).catch(() => {})
  }, [])

  // Simulation refs
  const simNodes    = useRef<SimNode[]>([])
  const edgeMap     = useRef<Map<string, number>>(new Map())
  const alphaRef    = useRef(1.0)
  const rafRef      = useRef(0)
  const loopRunning = useRef(false)
  const [, setFrameCount] = useState(0)

  // Mirror of layoutMode/depthLevel as refs so loadGraph (stable useCallback) can read live values
  const layoutModeRef  = useRef<LayoutMode>(layoutMode)
  const depthLevelRef  = useRef<number>(depthLevel)
  const nodeFilterRef  = useRef<'all' | 'named' | 'unnamed'>(nodeFilter)
  useEffect(() => { layoutModeRef.current = layoutMode }, [layoutMode])
  useEffect(() => { depthLevelRef.current = depthLevel }, [depthLevel])
  useEffect(() => { nodeFilterRef.current = nodeFilter }, [nodeFilter])

  // DOM refs
  const svgRef       = useRef<SVGSVGElement>(null)
  const graphGRef    = useRef<SVGGElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const svgTxRef     = useRef({ zoom: 1.0, panX: 0.0, panY: 0.0 })
  const dragRef      = useRef({ active: false, startX: 0, startY: 0, panX: 0.0, panY: 0.0, moved: false })
  // Node drag (separate from canvas pan)
  const nodeDragRef  = useRef<{ active: boolean; nodeId: string; startSvgX: number; startSvgY: number } | null>(null)

  // Direct DOM transform — avoids React re-render on every drag/wheel tick
  function applyTransform(zoom: number, panX: number, panY: number) {
    svgTxRef.current = { zoom, panX, panY }
    graphGRef.current?.setAttribute(
      'transform',
      `translate(${panX.toFixed(2)},${panY.toFixed(2)}) scale(${zoom.toFixed(4)})`
    )
  }

  // Convert a screen-space point to SVG viewBox space (accounting for pan+zoom)
  function screenToSvgSpace(clientX: number, clientY: number): { x: number; y: number } | null {
    const el = svgRef.current
    if (!el) return null
    const ctm = el.getScreenCTM()
    if (!ctm) return null
    const { zoom, panX, panY } = svgTxRef.current
    const pt = el.createSVGPoint()
    pt.x = clientX; pt.y = clientY
    const svgPt = pt.matrixTransform(ctm.inverse())
    // svgPt is in viewBox space; undo pan+zoom to get graph space
    return { x: (svgPt.x - panX) / zoom, y: (svgPt.y - panY) / zoom }
  }

  function zoomBy(factor: number) {
    const { zoom, panX, panY } = svgTxRef.current
    const newZoom = Math.max(0.15, Math.min(8.0, zoom * factor))
    // Zoom around canvas centre
    const cx = (svgRef.current?.clientWidth ?? 800) / 2
    const cy = (svgRef.current?.clientHeight ?? 500) / 2
    const ctm = svgRef.current?.getScreenCTM()
    if (!ctm) { applyTransform(newZoom, panX, panY); return }
    const el = svgRef.current!
    const pt = el.createSVGPoint(); pt.x = cx + el.getBoundingClientRect().left; pt.y = cy + el.getBoundingClientRect().top
    const svgPt = pt.matrixTransform(ctm.inverse())
    applyTransform(newZoom, svgPt.x - (svgPt.x - panX) / zoom * newZoom, svgPt.y - (svgPt.y - panY) / zoom * newZoom)
  }

  function kickSimulation(fromAlpha = 1.0) {
    alphaRef.current = Math.max(alphaRef.current, fromAlpha)
    if (loopRunning.current) return
    loopRunning.current = true
    const step = () => {
      if (alphaRef.current > 0.008) {
        for (let i = 0; i < 4; i++) tick(simNodes.current, edgeMap.current, alphaRef.current)
        alphaRef.current *= 0.97
        setFrameCount(c => c + 1)
        rafRef.current = requestAnimationFrame(step)
      } else {
        loopRunning.current = false
        rafRef.current = 0
      }
    }
    rafRef.current = requestAnimationFrame(step)
  }

  const loadGraph = useCallback(async (pid: number) => {
    cancelAnimationFrame(rafRef.current)
    loopRunning.current = false
    setLoading(true); setError(false); setGraph(null)
    setHoveredId(null); setEditingNode(null)
    applyTransform(1.0, 0, 0)
    try {
      const g = await api.persons.connectionsGraph(pid, 2)
      setGraph(g)
      simNodes.current = radialInit(g.nodes, g.center_id)
      edgeMap.current  = new Map(g.edges.map((e: ConnectionGraphEdge) => [`${e.source}||${e.target}`, e.weight]))
      const mode  = layoutModeRef.current
      const depth = depthLevelRef.current
      if (mode === 'force') {
        alphaRef.current = 1.0
        kickSimulation(1.0)
      } else {
        // Apply the current static layout immediately using live depth + filter
        alphaRef.current = 0
        const filter = nodeFilterRef.current
        const visibleIds   = computeVisibleNodeIds(g, depth)
        const visibleNodes = g.nodes.filter(n => {
          if (!visibleIds.has(n.id)) return false
          if (n.id === g.center_id) return true
          if (filter === 'named')   return n.type === 'person'
          if (filter === 'unnamed') return n.type === 'cluster'
          return true
        })
        let posMap: Map<string, {x: number; y: number}>
        if (mode === 'tree-h')      posMap = computeTreeHPositions(visibleNodes, g.center_id)
        else if (mode === 'tree-v') posMap = computeTreeVPositions(visibleNodes, g.center_id)
        else                        posMap = computeCircularPositions(visibleNodes, g.center_id)
        simNodes.current.forEach(n => {
          const p = posMap.get(n.id)
          if (p) { n.x = p.x; n.y = p.y; n.vx = 0; n.vy = 0 }
        })
        setFrameCount(c => c + 1)
      }
    } catch { setError(true) }
    finally  { setLoading(false) }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    loadGraph(personId)
    return () => { cancelAnimationFrame(rafRef.current); loopRunning.current = false }
  }, [personId, loadGraph])

  // Non-passive wheel for zoom-around-cursor.
  // Attached to the container div (always mounted), not the SVG (conditionally rendered).
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const handler = (e: WheelEvent) => {
      e.preventDefault()
      const svg = svgRef.current
      if (!svg) return
      const factor  = e.deltaY < 0 ? 1.12 : 0.89
      const { zoom, panX, panY } = svgTxRef.current
      const newZoom = Math.max(0.15, Math.min(8.0, zoom * factor))
      const pt = svg.createSVGPoint()
      pt.x = e.clientX; pt.y = e.clientY
      const ctm = svg.getScreenCTM()
      if (!ctm) return
      const svgPt   = pt.matrixTransform(ctm.inverse())
      const newPanX = svgPt.x - (svgPt.x - panX) / zoom * newZoom
      const newPanY = svgPt.y - (svgPt.y - panY) / zoom * newZoom
      applyTransform(newZoom, newPanX, newPanY)
    }
    el.addEventListener('wheel', handler, { passive: false })
    return () => el.removeEventListener('wheel', handler)
  }, [])

  // Apply a static layout (tree/circular) by directly setting node positions.
  // All three control values (mode, depth, filter) must be passed explicitly to
  // avoid stale closures — state setters are async, refs are always current.
  function applyStaticLayout(
    mode: LayoutMode,
    g: ConnectionGraph,
    depth: number,
    filter: 'all' | 'named' | 'unnamed' = 'all'
  ) {
    if (mode === 'force') return
    const visibleIds   = computeVisibleNodeIds(g, depth)
    const visibleNodes = g.nodes.filter(n => {
      if (!visibleIds.has(n.id)) return false
      if (n.id === g.center_id) return true // centre always included in layout
      if (filter === 'named')   return n.type === 'person'
      if (filter === 'unnamed') return n.type === 'cluster'
      return true
    })
    let posMap: Map<string, {x: number; y: number}>
    if (mode === 'tree-h')      posMap = computeTreeHPositions(visibleNodes, g.center_id)
    else if (mode === 'tree-v') posMap = computeTreeVPositions(visibleNodes, g.center_id)
    else                        posMap = computeCircularPositions(visibleNodes, g.center_id)
    simNodes.current.forEach(n => {
      const p = posMap.get(n.id)
      if (p) { n.x = p.x; n.y = p.y; n.vx = 0; n.vy = 0 }
    })
    setFrameCount(c => c + 1)
  }

  function changeLayout(mode: LayoutMode) {
    setLayoutMode(mode)
    if (mode === 'force') {
      kickSimulation(0.8)
    } else if (graph) {
      cancelAnimationFrame(rafRef.current)
      loopRunning.current = false
      alphaRef.current = 0
      applyStaticLayout(mode, graph, depthLevel, nodeFilter)
    }
  }

  // Mouse pan
  function onMouseDown(e: React.MouseEvent) {
    if (e.button !== 0) return
    const { panX, panY } = svgTxRef.current
    dragRef.current = { active: true, startX: e.clientX, startY: e.clientY, panX, panY, moved: false }
  }
  function onMouseMove(e: React.MouseEvent) {
    // Node drag takes priority
    const nd = nodeDragRef.current
    if (nd?.active) {
      const gsp = screenToSvgSpace(e.clientX, e.clientY)
      if (!gsp) return
      const node = simNodes.current.find(n => n.id === nd.nodeId)
      if (node) {
        node.x = gsp.x
        node.y = gsp.y
        node.vx = 0; node.vy = 0
        dragRef.current.moved = true
        setFrameCount(c => c + 1)
      }
      return
    }
    const dr = dragRef.current
    if (!dr.active) return
    const dx = e.clientX - dr.startX, dy = e.clientY - dr.startY
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dr.moved = true
    if (!dr.moved) return
    const ctm = svgRef.current?.getScreenCTM()
    if (!ctm) return
    applyTransform(svgTxRef.current.zoom, dr.panX + dx / ctm.a, dr.panY + dy / ctm.d)
  }
  function onMouseUp() {
    if (nodeDragRef.current?.active) {
      // Unpin node unless it's centre; re-heat simulation slightly for force layout
      const node = simNodes.current.find(n => n.id === nodeDragRef.current!.nodeId)
      if (node && !node.pinned && layoutMode === 'force') kickSimulation(0.25)
      nodeDragRef.current = null
    }
    dragRef.current.active = false
  }

  // Node navigation
  function handleNodeClick(node: SimNode) {
    if (dragRef.current.moved || nodeDragRef.current !== null) return
    if (node.pinned || node.type !== 'person') return
    setHistory(h => [...h, { id: currentPid, name: currentName }])
    setCurrentPid(node.raw_id)
    setCurrentName(node.name ?? 'Unknown')
    loadGraph(node.raw_id)
  }
  function handleBack() {
    if (!history.length) return
    const prev = history[history.length - 1]
    setHistory(h => h.slice(0, -1))
    setCurrentPid(prev.id); setCurrentName(prev.name)
    loadGraph(prev.id)
  }

  // Start dragging a specific node
  function onNodeMouseDown(e: React.MouseEvent, node: SimNode) {
    e.stopPropagation() // prevent canvas pan from starting
    const gsp = screenToSvgSpace(e.clientX, e.clientY)
    if (!gsp) return
    nodeDragRef.current = { active: true, nodeId: node.id, startSvgX: gsp.x, startSvgY: gsp.y }
    dragRef.current.moved = false
  }

  // Depth change re-heats simulation so nodes re-arrange
  function changeDepth(level: number) {
    setDepthLevel(level)
    if (layoutMode === 'force') {
      kickSimulation(0.55)
    } else if (graph) {
      applyStaticLayout(layoutMode, graph, level, nodeFilter)
    }
  }

  // Filter change: re-draw with current depth and layout
  function changeFilter(f: 'all' | 'named' | 'unnamed') {
    setNodeFilter(f)
    nodeFilterRef.current = f // update immediately (setNodeFilter is async)
    if (layoutMode === 'force') {
      kickSimulation(0.45)
    } else if (graph) {
      applyStaticLayout(layoutMode, graph, depthLevel, f)
    }
  }

  // Edit panel
  function openEdit(node: SimNode, e: React.MouseEvent) {
    e.stopPropagation()
    setEditingNode(node); setEditName(node.name ?? ''); setEditError('')
    setMergeCandidate(null); setEditShowSuggestions(false)
    if (svgRef.current && containerRef.current) {
      const { zoom, panX, panY } = svgTxRef.current
      const pt = svgRef.current.createSVGPoint()
      pt.x = panX + node.x * zoom
      pt.y = panY + node.y * zoom
      const ctm = svgRef.current.getScreenCTM()
      if (ctm) {
        const sc = pt.matrixTransform(ctm)
        const r  = containerRef.current.getBoundingClientRect()
        setEditPos({ x: sc.x - r.left, y: sc.y - r.top })
      }
    }
  }

  async function saveEdit() {
    if (!editingNode || !editName.trim()) return
    const trimmed = editName.trim()

    // Check for an existing person with this name (case-insensitive),
    // excluding the node being edited itself (for renames)
    const existing = allPersons.find(
      p => p.name?.toLowerCase() === trimmed.toLowerCase() &&
           !(editingNode.type === 'person' && p.id === editingNode.raw_id)
    )
    if (existing) {
      // Pause and ask the user: same person (merge) or different?
      setMergeCandidate({ personId: existing.id, name: existing.name! })
      return
    }

    setEditSaving(true); setEditError('')
    try {
      if (editingNode.type === 'cluster') {
        await api.persons.fromCluster(editingNode.raw_id, trimmed)
      } else {
        await api.persons.namePerson(editingNode.raw_id, trimmed)
      }
      const refreshed = await api.persons.list()
      setAllPersons(refreshed)
      setEditingNode(null)
      loadGraph(currentPid)
    } catch {
      setEditError('Could not save — please try again.')
    } finally {
      setEditSaving(false)
    }
  }

  // Merge: treat as the same person
  async function confirmMerge() {
    if (!editingNode || !mergeCandidate) return
    setEditSaving(true); setEditError('')
    try {
      if (editingNode.type === 'cluster') {
        // Add this unnamed cluster into the existing person
        await api.persons.addCluster(mergeCandidate.personId, editingNode.raw_id)
      } else {
        // Merge two named persons — keep the existing person as survivor
        await api.persons.mergePersons(editingNode.raw_id, mergeCandidate.personId, mergeCandidate.name)
      }
      const refreshed = await api.persons.list()
      setAllPersons(refreshed)
      setMergeCandidate(null); setEditingNode(null)
      loadGraph(currentPid)
    } catch {
      setEditError('Could not merge — please try again.')
    } finally {
      setEditSaving(false)
    }
  }

  // Different person: save with a disambiguated name
  async function saveDifferentPerson() {
    if (!editingNode || !editName.trim()) return
    const disambiguated = editName.trim() + ' (2)'
    setEditSaving(true); setEditError('')
    try {
      if (editingNode.type === 'cluster') {
        await api.persons.fromCluster(editingNode.raw_id, disambiguated)
      } else {
        await api.persons.namePerson(editingNode.raw_id, disambiguated)
      }
      const refreshed = await api.persons.list()
      setAllPersons(refreshed)
      setMergeCandidate(null); setEditingNode(null)
      loadGraph(currentPid)
    } catch {
      setEditError('Could not save — please try again.')
    } finally {
      setEditSaving(false)
    }
  }

  // Cluster photo gallery
  const [clusterGallery,  setClusterGallery]  = useState<{ id: number; label: string } | null>(null)
  const [galleryPhotos,   setGalleryPhotos]   = useState<MediaFile[]>([])
  const [galleryLoading,  setGalleryLoading]  = useState(false)

  async function openClusterGallery(clusterId: number, label: string) {
    setEditingNode(null)
    setClusterGallery({ id: clusterId, label })
    setGalleryLoading(true)
    try {
      const photos = await api.media.list({ cluster_id: clusterId, limit: 200 })
      setGalleryPhotos(photos)
    } finally {
      setGalleryLoading(false)
    }
  }

  // Ignore: send unnamed cluster to Ignored Faces
  async function ignoreClusterNode() {
    if (!editingNode || editingNode.type !== 'cluster') return
    setEditSaving(true); setEditError('')
    try {
      await api.clusters.ignore(editingNode.raw_id)
      setEditingNode(null)
      loadGraph(currentPid)
    } catch {
      setEditError('Could not ignore — please try again.')
    } finally {
      setEditSaving(false)
    }
  }

  // ── Derived visible set ────────────────────────────────────────────────

  const visibleNodeIds  = graph ? computeVisibleNodeIds(graph, depthLevel) : new Set<string>()
  const nm              = new Map(simNodes.current.map(n => [n.id, n]))
  const visibleSimNodes = simNodes.current.filter(n => {
    if (!visibleNodeIds.has(n.id)) return false
    if (n.pinned) return true // centre node always visible regardless of filter
    if (nodeFilter === 'named')   return n.type === 'person'
    if (nodeFilter === 'unnamed') return n.type === 'cluster'
    return true
  })
  const visibleNodeIdSet = new Set(visibleSimNodes.map(n => n.id))
  const visibleEdges    = (graph?.edges ?? []).filter(
    e => visibleNodeIdSet.has(e.source) && visibleNodeIdSet.has(e.target)
  )
  const maxWeight = graph ? Math.max(...graph.edges.map(e => e.weight), 1) : 1

  // Stats
  const statNamed   = visibleSimNodes.filter(n => n.type === 'person' && !n.pinned).length
  const statUnnamed = visibleSimNodes.filter(n => n.type === 'cluster').length
  const statD1      = visibleSimNodes.filter(n => n.depth === 1).length
  const statD2      = visibleSimNodes.filter(n => n.depth === 2).length

  const hoveredNode = hoveredId ? nm.get(hoveredId) : null
  const hoveredEdge = (hoveredNode && graph)
    ? graph.edges.find(e =>
        (e.source === hoveredId && e.target === graph.center_id) ||
        (e.target === hoveredId && e.source === graph.center_id)
      )
    : null

  // Edit panel: flip left if near right edge
  const cW = containerRef.current?.clientWidth ?? 1400
  const cH = containerRef.current?.clientHeight ?? 900
  const editL = editPos ? (editPos.x + 268 > cW ? editPos.x - 276 : editPos.x + 16) : 0
  const editT = editPos ? Math.min(Math.max(8, editPos.y - 60), cH - 210) : 0

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-gray-950">

      {/* Header */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-gray-800 shrink-0">
        <div className="flex items-center gap-3">
          {history.length > 0 && (
            <button onClick={handleBack}
              className="text-gray-500 hover:text-gray-200 transition-colors text-sm">
              ← Back
            </button>
          )}
          <span className="text-gray-500 text-xs font-medium uppercase tracking-widest">Connections</span>
          <span className="text-gray-500 text-xs">·</span>
          <span className="text-white font-semibold text-sm">{currentName}</span>
          {graph && (
            <span className="text-gray-600 text-xs">
              ({graph.nodes.length - 1} total · {statNamed} named · {statUnnamed} unnamed)
            </span>
          )}
        </div>

        <div className="flex items-center gap-4">
          {/* Node filter */}
          <div className="flex items-center gap-1 bg-gray-900 border border-gray-700 rounded-lg overflow-hidden">
            {(['all', 'named', 'unnamed'] as const).map(f => (
              <button key={f} onClick={() => changeFilter(f)}
                className={`px-2.5 py-1 text-xs font-medium transition-colors capitalize ${
                  nodeFilter === f
                    ? 'bg-indigo-700 text-white'
                    : 'text-gray-400 hover:text-white hover:bg-gray-800'
                }`}>{f}</button>
            ))}
          </div>

          {/* Layout mode selector */}
          <div className="flex items-center gap-1">
            <span className="text-gray-600 text-xs mr-1">View:</span>
            {(['force', 'tree-h', 'tree-v', 'circular'] as LayoutMode[]).map(m => (
              <button key={m} onClick={() => changeLayout(m)}
                title={LAYOUT_LABELS[m]}
                className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-colors border ${
                  layoutMode === m
                    ? 'bg-indigo-700 border-indigo-500 text-white'
                    : 'bg-gray-900 border-gray-700 text-gray-400 hover:text-white hover:border-gray-500'
                }`}>
                {LAYOUT_LABELS[m]}
              </button>
            ))}
          </div>

          {/* Depth level selector */}
          <div className="flex items-center gap-1.5">
            <span className="text-gray-600 text-xs">Depth:</span>
            {[1, 2, 3, 4].map(l => (
              <button key={l} onClick={() => changeDepth(l)}
                title={`${DEPTH_LABELS[l]} view`}
                className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-colors border ${
                  depthLevel === l
                    ? 'bg-indigo-700 border-indigo-500 text-white'
                    : 'bg-gray-900 border-gray-700 text-gray-400 hover:text-white hover:border-gray-500'
                }`}>
                {l}
              </button>
            ))}
            <span className="text-gray-500 text-xs ml-0.5">{DEPTH_LABELS[depthLevel]}</span>
          </div>

          {/* Zoom buttons */}
          <div className="flex items-center gap-1 bg-gray-900 border border-gray-700 rounded-lg overflow-hidden">
            <button onClick={() => zoomBy(1.25)}
              className="px-2.5 py-1 text-gray-400 hover:text-white hover:bg-gray-800 transition-colors text-sm leading-none"
              title="Zoom in">+</button>
            <button onClick={() => applyTransform(1.0, 0, 0)}
              className="px-2 py-1 text-gray-500 hover:text-white hover:bg-gray-800 transition-colors text-[10px] leading-none border-x border-gray-700"
              title="Reset zoom">⌂</button>
            <button onClick={() => zoomBy(0.8)}
              className="px-2.5 py-1 text-gray-400 hover:text-white hover:bg-gray-800 transition-colors text-sm leading-none"
              title="Zoom out">−</button>
          </div>

          {onNavigatePerson && (
            <button onClick={() => { onClose(); onNavigatePerson(currentPid, currentName) }}
              className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors">
              View photos →
            </button>
          )}
          <button onClick={onClose}
            className="text-gray-600 hover:text-white transition-colors text-xl leading-none">
            ✕
          </button>
        </div>
      </div>

      {/* Graph canvas */}
      <div className="relative flex-1 min-h-0 overflow-hidden" ref={containerRef}>

        {loading && (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="w-9 h-9 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
          </div>
        )}

        {error && (
          <div className="absolute inset-0 flex items-center justify-center text-gray-500 text-sm">
            Could not load connection data.
          </div>
        )}

        {!loading && graph && graph.nodes.length === 1 && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-gray-500 text-sm">
            <span>No connections found yet.</span>
            <span className="text-xs text-gray-700">Co-occurrence data builds as photos are processed.</span>
          </div>
        )}

        {!loading && graph && graph.nodes.length > 1 && (
          <svg
            ref={svgRef}
            viewBox={`0 0 ${W} ${H}`}
            width="100%" height="100%"
            style={{ display: 'block', cursor: 'grab', userSelect: 'none' }}
            onMouseDown={onMouseDown}
            onMouseMove={onMouseMove}
            onMouseUp={onMouseUp}
            onMouseLeave={onMouseUp}
          >
            <defs>
              <clipPath id="cgc0"><circle cx="0" cy="0" r={NODE_R[0]} /></clipPath>
              <clipPath id="cgc1"><circle cx="0" cy="0" r={NODE_R[1]} /></clipPath>
              <clipPath id="cgc2"><circle cx="0" cy="0" r={NODE_R[2]} /></clipPath>
              <radialGradient id="cgglow" cx="50%" cy="50%" r="50%">
                <stop offset="0%"   stopColor="#818cf8" stopOpacity="0.28" />
                <stop offset="100%" stopColor="#818cf8" stopOpacity="0" />
              </radialGradient>
            </defs>

            <g ref={graphGRef}>
              {/* Edges */}
              {visibleEdges.map((edge: ConnectionGraphEdge) => {
                const a = nm.get(edge.source), b = nm.get(edge.target)
                if (!a || !b) return null
                const frac = edge.weight / maxWeight
                return (
                  <line key={`${edge.source}--${edge.target}`}
                    x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                    stroke="#6366f1"
                    strokeWidth={0.8 + frac * 3.5}
                    strokeOpacity={0.12 + frac * 0.58}
                    pointerEvents="none"
                  />
                )
              })}

              {/* Nodes */}
              {visibleSimNodes.map(node => {
                const d       = Math.min(node.depth, 2)
                const r       = nodeRadius(d)
                const url     = thumbUrl(node.thumbnail)
                const isCtr   = node.pinned
                const isHov   = hoveredId === node.id
                const isNamed = node.type === 'person'
                const label   = node.name ? node.name.split(' ')[0] : '?'

                return (
                  <g key={node.id}
                    transform={`translate(${node.x.toFixed(1)},${node.y.toFixed(1)})`}
                    style={{ cursor: isCtr ? 'default' : 'grab' }}
                    onClick={() => handleNodeClick(node)}
                    onMouseDown={e => onNodeMouseDown(e, node)}
                    onMouseEnter={() => setHoveredId(node.id)}
                    onMouseLeave={() => setHoveredId(null)}
                  >
                    {isCtr && (
                      <>
                        <circle r={r + 44} fill="url(#cgglow)" />
                        <circle r={r + 16} fill="none" stroke="#818cf8"
                          strokeWidth={1} strokeOpacity={0.3} />
                      </>
                    )}
                    {isHov && !isCtr && (
                      <circle r={r + 8} fill="none" stroke="#a5b4fc"
                        strokeWidth={1.5} strokeOpacity={0.7} strokeDasharray="4 3" />
                    )}
                    <circle r={r}
                      fill={isNamed ? '#1e1b4b' : '#111827'}
                      stroke={isCtr ? '#818cf8' : isNamed ? '#4338ca' : '#374151'}
                      strokeWidth={isCtr ? 2.5 : 1.5}
                    />
                    {url
                      ? <image href={url} x={-r} y={-r} width={r * 2} height={r * 2}
                          clipPath={`url(#cgc${d})`} preserveAspectRatio="xMidYMid slice" />
                      : <text textAnchor="middle" dominantBaseline="middle"
                          fontSize={r * 0.82} fill="#4b5563" pointerEvents="none">👤</text>
                    }
                    {node.depth === 2 && (
                      <circle r={r} fill="black" fillOpacity={0.25} pointerEvents="none" />
                    )}
                    <text y={r + (d === 0 ? 17 : 14)} textAnchor="middle"
                      fontSize={d === 0 ? 14 : d === 1 ? 12 : 10}
                      fontWeight={d === 0 ? '600' : '400'}
                      fill={d === 0 ? '#e0e7ff' : isNamed ? '#c7d2fe' : '#6b7280'}
                      pointerEvents="none"
                    >{label}</text>
                    {isCtr && (
                      <text y={r + 33} textAnchor="middle" fontSize={11}
                        fill="#6366f1" pointerEvents="none">
                        {node.photo_count} photo{node.photo_count !== 1 ? 's' : ''}
                      </text>
                    )}

                    {/* Edit pencil — appears on hover for any non-centre node */}
                    {isHov && !isCtr && (
                      <g transform={`translate(${(r * 0.72).toFixed(1)},${(-r * 0.72).toFixed(1)})`}
                        onClick={(e) => openEdit(node, e)}
                        style={{ cursor: 'pointer' }}
                      >
                        <circle r={12} fill="#1f2937" stroke="#6366f1" strokeWidth={1.2} />
                        <text textAnchor="middle" dominantBaseline="middle"
                          fontSize={13} fill="#a5b4fc" pointerEvents="none">✎</text>
                      </g>
                    )}
                  </g>
                )
              })}
            </g>
          </svg>
        )}

        {/* Edit panel (HTML overlay near node) */}
        {editingNode && editPos && (() => {
          const filteredSuggestions = editName.trim().length > 0
            ? allPersons
                .filter(p => p.name && p.name.toLowerCase().includes(editName.toLowerCase()))
                .filter(p => editingNode.type === 'person' ? p.id !== editingNode.raw_id : true)
                .slice(0, 6)
                .map(p => p.name as string)
            : []
          return (
            <div className="absolute z-20 w-72 bg-gray-900 border border-indigo-600 rounded-xl shadow-2xl p-4"
              style={{ left: editL, top: editT }}
              onClick={e => e.stopPropagation()}
            >
              <p className="text-xs text-gray-400 mb-2 font-medium">
                {editingNode.type === 'cluster' ? 'Name this face' : 'Rename person'}
              </p>
              {editingNode.thumbnail && (
                <div className="flex items-center gap-2 mb-3">
                  <img src={thumbUrl(editingNode.thumbnail) ?? ''} alt=""
                    className="w-10 h-10 rounded-lg object-cover border border-gray-700 flex-shrink-0" />
                  <span className="text-sm text-gray-400 truncate">
                    {editingNode.name ?? 'Unnamed face'}
                  </span>
                </div>
              )}

              {mergeCandidate ? (
                /* ── Merge confirmation ── */
                <>
                  <p className="text-white text-sm font-medium mb-1">Same person?</p>
                  <p className="text-gray-400 text-xs mb-4">
                    &ldquo;{mergeCandidate.name}&rdquo; already exists. Merge or save as different?
                  </p>
                  {editError && <p className="text-xs text-red-400 mb-2">{editError}</p>}
                  <div className="flex flex-col gap-2">
                    <button onClick={confirmMerge} disabled={editSaving}
                      className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-xs font-medium rounded-lg py-1.5 transition-colors">
                      {editSaving ? 'Saving…' : 'Same — merge'}
                    </button>
                    <button onClick={saveDifferentPerson} disabled={editSaving}
                      className="bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-gray-200 text-xs rounded-lg py-1.5 transition-colors">
                      Different person
                    </button>
                    <button onClick={() => setMergeCandidate(null)}
                      className="text-gray-600 hover:text-gray-400 text-xs py-1 transition-colors">
                      ← Back
                    </button>
                  </div>
                </>
              ) : (
                /* ── Name input with autocomplete ── */
                <>
                  <div className="relative mb-2">
                    <input autoFocus
                      value={editName}
                      onChange={e => { setEditName(e.target.value); setEditError(''); setEditShowSuggestions(true) }}
                      onFocus={() => setEditShowSuggestions(true)}
                      onBlur={() => setTimeout(() => setEditShowSuggestions(false), 120)}
                      onKeyDown={e => {
                        if (e.key === 'Enter') { setEditShowSuggestions(false); saveEdit() }
                        if (e.key === 'Escape') setEditingNode(null)
                      }}
                      placeholder="Enter name…"
                      className="w-full bg-gray-800 border border-gray-600 focus:border-indigo-500 rounded-lg px-3 py-1.5 text-sm text-white outline-none"
                    />
                    {editShowSuggestions && filteredSuggestions.length > 0 && (
                      <ul
                        onMouseDown={e => e.preventDefault()}
                        className="absolute top-full left-0 right-0 bg-gray-900 border border-gray-700 rounded-b-lg shadow-xl z-10 max-h-40 overflow-y-auto"
                      >
                        {filteredSuggestions.map(name => (
                          <li key={name}
                            onClick={() => { setEditName(name); setEditShowSuggestions(false) }}
                            className="px-3 py-1.5 text-xs text-gray-200 hover:bg-indigo-700 hover:text-white cursor-pointer truncate"
                          >{name}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                  {editError && <p className="text-xs text-red-400 mb-2">{editError}</p>}
                  <div className="flex flex-col gap-2">
                    <button onClick={saveEdit}
                      disabled={editSaving || !editName.trim()}
                      className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-xs font-medium rounded-lg py-1.5 transition-colors">
                      {editSaving ? 'Saving…' : 'Save'}
                    </button>
                    <button onClick={() => setEditingNode(null)}
                      className="flex-1 bg-gray-800 hover:bg-gray-700 text-gray-400 text-xs rounded-lg py-1.5 transition-colors">
                      Cancel
                    </button>
                    {editingNode.type === 'cluster' && (
                      <button
                        onClick={() => openClusterGallery(editingNode.raw_id, editingNode.name ?? 'Unnamed face')}
                        className="flex-1 bg-transparent border border-gray-700 hover:border-indigo-500 hover:text-indigo-400 text-gray-500 text-xs rounded-lg py-1.5 transition-colors">
                        View photos
                      </button>
                    )}
                    {editingNode.type === 'cluster' && (
                      <button onClick={ignoreClusterNode} disabled={editSaving}
                        className="flex-1 bg-transparent border border-gray-700 hover:border-red-700 hover:text-red-400 text-gray-600 text-xs rounded-lg py-1.5 transition-colors">
                        Ignore face
                      </button>
                    )}
                  </div>
                </>
              )}
            </div>
          )
        })()}

        {/* Cluster photo gallery overlay */}
        {clusterGallery && (
          <div className="absolute inset-0 z-30 flex flex-col bg-gray-950/95 backdrop-blur-sm">
            {/* Gallery header */}
            <div className="flex items-center justify-between px-5 py-3 border-b border-gray-800 shrink-0">
              <div className="flex items-center gap-3">
                <button
                  onClick={() => setClusterGallery(null)}
                  className="text-gray-500 hover:text-gray-200 transition-colors text-sm"
                >
                  ← Back
                </button>
                <span className="text-gray-500 text-xs">·</span>
                <span className="text-white text-sm font-medium">Photos with this face</span>
                {!galleryLoading && (
                  <span className="text-gray-500 text-xs">({galleryPhotos.length})</span>
                )}
              </div>
              <button
                onClick={() => setClusterGallery(null)}
                className="text-gray-600 hover:text-white transition-colors text-xl leading-none"
              >
                ✕
              </button>
            </div>

            {/* Gallery body */}
            <div className="flex-1 overflow-y-auto p-5">
              {galleryLoading && (
                <div className="flex items-center justify-center h-full">
                  <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
                </div>
              )}
              {!galleryLoading && galleryPhotos.length === 0 && (
                <p className="text-gray-500 text-sm text-center mt-12">No photos found.</p>
              )}
              {!galleryLoading && galleryPhotos.length > 0 && (
                <div className="grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-3">
                  {galleryPhotos.map(photo => (
                    <div
                      key={photo.id}
                      className="aspect-square rounded-xl overflow-hidden bg-gray-900 border border-gray-800 hover:border-indigo-500 transition-colors"
                    >
                      <img
                        src={api.media.thumbnailUrl(photo.id)}
                        alt={photo.file_path.split('/').pop()}
                        className="w-full h-full object-cover"
                        loading="lazy"
                      />
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Stats panel — bottom-left */}
        {graph && graph.nodes.length > 1 && (
          <div className="absolute bottom-4 left-4 z-10 bg-gray-950/85 border border-gray-800 rounded-xl px-4 py-3 backdrop-blur-sm pointer-events-none">
            <p className="text-gray-600 text-[10px] font-semibold uppercase tracking-widest mb-2">Stats</p>
            <div className="flex flex-col gap-1 text-xs text-gray-500">
              <span>
                <span className="inline-block w-2 h-2 rounded-full bg-indigo-900 border border-indigo-700 mr-1.5 align-middle" />
                Named: <span className="text-gray-300 font-medium">{statNamed}</span>
              </span>
              <span>
                <span className="inline-block w-2 h-2 rounded-full bg-gray-900 border border-gray-700 mr-1.5 align-middle" />
                Unnamed: <span className="text-gray-300 font-medium">{statUnnamed}</span>
              </span>
              <div className="border-t border-gray-800 my-1" />
              <span>Level 1 connections: <span className="text-gray-400 font-medium">{statD1}</span></span>
              {statD2 > 0 && (
                <span>Level 2 connections: <span className="text-gray-400 font-medium">{statD2}</span></span>
              )}
            </div>
            <p className="text-gray-700 text-[9px] mt-2 leading-snug">
              Scroll · zoom &nbsp;|&nbsp; Drag canvas · pan<br />
              Drag node · reposition<br />
              Click named node · explore<br />
              ✎ Hover · name/rename
            </p>
          </div>
        )}

        {/* Hover tooltip — bottom-centre */}
        {hoveredNode && !hoveredNode.pinned && (
          <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10 bg-gray-900/90 border border-gray-700 rounded-xl px-4 py-2 text-xs text-gray-300 pointer-events-none backdrop-blur-sm max-w-sm text-center">
            <span className="text-white font-medium">{hoveredNode.name ?? 'Unnamed face'}</span>
            {hoveredEdge && (
              <span className="text-gray-500 ml-2">
                · {hoveredEdge.weight} shared photo{hoveredEdge.weight !== 1 ? 's' : ''}
              </span>
            )}
            {hoveredNode.type === 'cluster' && (
              <span className="text-purple-400 ml-2">· click ✎ to name</span>
            )}
            {hoveredNode.type === 'person' && (
              <span className="text-indigo-400 ml-2">· click to explore · ✎ to rename</span>
            )}
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="shrink-0 flex items-center justify-between px-5 py-2 border-t border-gray-800">
        <div className="flex items-center gap-4">
          <span className="flex items-center gap-1.5 text-xs text-gray-700">
            <span className="inline-block w-2.5 h-2.5 rounded-full bg-indigo-900 border border-indigo-700" />
            Named person
          </span>
          <span className="flex items-center gap-1.5 text-xs text-gray-700">
            <span className="inline-block w-2.5 h-2.5 rounded-full bg-gray-900 border border-gray-700" />
            Unnamed face
          </span>
          <span className="text-gray-800 text-xs">·</span>
          <span className="text-gray-700 text-xs">Edge thickness = shared photos</span>
        </div>
        <span className="text-gray-700 text-xs">
          Depth {depthLevel}/4 · {visibleSimNodes.length - 1} connection{visibleSimNodes.length !== 2 ? 's' : ''} visible
        </span>
      </div>
    </div>
  )
}
