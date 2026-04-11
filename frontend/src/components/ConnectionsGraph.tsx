/**
 * ConnectionsGraph — force-directed social graph modal.
 *
 * Shows the selected person at centre with co-occurrence connections
 * arranged around them as a living force graph.  Clicking any named
 * person node recentres the graph on them.  Unnamed clusters appear as
 * grey nodes and are not re-centerable.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { ConnectionGraph, ConnectionGraphNode, ConnectionGraphEdge } from '../api/client'

// ── Props ─────────────────────────────────────────────────────────────────

interface Props {
  personId: number
  personName: string
  onClose: () => void
  /** Called when the user wants to navigate to a person's photos. */
  onNavigatePerson?: (personId: number, name: string) => void
}

// ── Simulation types ──────────────────────────────────────────────────────

interface SimNode extends ConnectionGraphNode {
  x: number
  y: number
  vx: number
  vy: number
  pinned: boolean
}

// ── Layout constants ──────────────────────────────────────────────────────

const W = 900
const H = 640
const CX = W / 2
const CY = H / 2
// Radial ring radius per depth and visual node radius per depth
const RING_R  = [0, 170, 310] as const
const NODE_R  = [38, 26, 17]  as const

// ── Helpers ───────────────────────────────────────────────────────────────

function thumbUrl(path: string | null): string | null {
  if (!path) return null
  const part = path.split('/thumbnails/').pop()
  return part ? '/thumbnails/' + part : null
}

function nodeRadius(depth: number): number {
  return NODE_R[Math.min(depth, 2)]
}

// ── Physics ───────────────────────────────────────────────────────────────

function radialInit(nodes: ConnectionGraphNode[], centerId: string): SimNode[] {
  const byDepth: ConnectionGraphNode[][] = [[], [], []]
  for (const n of nodes) byDepth[Math.min(n.depth, 2)].push(n)
  return nodes.map(n => {
    const d = Math.min(n.depth, 2)
    const ring  = byDepth[d]
    const idx   = ring.indexOf(n)
    const angle = (2 * Math.PI * idx) / (ring.length || 1) - Math.PI / 2
    const r     = RING_R[d]
    const jitter = d > 0 ? 12 : 0
    return {
      ...n,
      x: CX + r * Math.cos(angle) + (Math.random() - 0.5) * jitter,
      y: CY + r * Math.sin(angle) + (Math.random() - 0.5) * jitter,
      vx: 0,
      vy: 0,
      pinned: n.id === centerId,
    }
  })
}

function tick(nodes: SimNode[], edgeMap: Map<string, number>, alpha: number) {
  const nm = new Map(nodes.map(n => [n.id, n]))

  // Repulsion between all node pairs
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j]
      const dx = b.x - a.x, dy = b.y - a.y
      const d2 = dx * dx + dy * dy + 1
      const d  = Math.sqrt(d2)
      const f  = (5500 / d2) * alpha
      const fx = f * dx / d, fy = f * dy / d
      if (!a.pinned) { a.vx -= fx; a.vy -= fy }
      if (!b.pinned) { b.vx += fx; b.vy += fy }
    }
  }

  // Edge spring attraction
  for (const [key, weight] of edgeMap) {
    const [sid, tid] = key.split('||')
    const a = nm.get(sid), b = nm.get(tid)
    if (!a || !b) continue
    const dx = b.x - a.x, dy = b.y - a.y
    const d  = Math.sqrt(dx * dx + dy * dy) || 1
    const targetLen = 165 - Math.log1p(weight) * 8
    const f  = (d - targetLen) * 0.04 * alpha
    const fx = f * dx / d, fy = f * dy / d
    if (!a.pinned) { a.vx += fx; a.vy += fy }
    if (!b.pinned) { b.vx -= fx; b.vy -= fy }
  }

  // Gentle centre gravity
  for (const n of nodes) {
    if (n.pinned) continue
    n.vx += (CX - n.x) * 0.006 * alpha
    n.vy += (CY - n.y) * 0.006 * alpha
  }

  // Integrate + dampen
  for (const n of nodes) {
    if (n.pinned) { n.x = CX; n.y = CY; n.vx = 0; n.vy = 0; continue }
    n.vx *= 0.82
    n.vy *= 0.82
    n.x = Math.max(42, Math.min(W - 42, n.x + n.vx))
    n.y = Math.max(42, Math.min(H - 42, n.y + n.vy))
  }
}

// ── Component ─────────────────────────────────────────────────────────────

interface HistoryEntry { id: number; name: string }

export default function ConnectionsGraph({ personId, personName, onClose, onNavigatePerson }: Props) {
  const [graph,      setGraph]      = useState<ConnectionGraph | null>(null)
  const [loading,    setLoading]    = useState(true)
  const [error,      setError]      = useState(false)
  const [hoveredId,  setHoveredId]  = useState<string | null>(null)
  const [currentPid, setCurrentPid] = useState(personId)
  const [currentName,setCurrentName]= useState(personName)
  const [history,    setHistory]    = useState<HistoryEntry[]>([])

  // Mutable simulation state — written every RAF frame, not React state
  const simNodes = useRef<SimNode[]>([])
  const edgeMap  = useRef<Map<string, number>>(new Map())
  const alphaRef = useRef(1.0)
  const rafRef   = useRef(0)
  // Counter used only to trigger SVG re-renders
  const [, setFrameCount] = useState(0)

  const loadGraph = useCallback(async (pid: number) => {
    cancelAnimationFrame(rafRef.current)
    setLoading(true)
    setError(false)
    setGraph(null)
    setHoveredId(null)
    try {
      const g = await api.persons.connectionsGraph(pid)
      setGraph(g)
      simNodes.current = radialInit(g.nodes, g.center_id)
      edgeMap.current  = new Map(g.edges.map((e: ConnectionGraphEdge) => [`${e.source}||${e.target}`, e.weight]))
      alphaRef.current = 1.0

      const animate = () => {
        if (alphaRef.current > 0.008) {
          // Multiple ticks per frame for faster convergence
          for (let i = 0; i < 4; i++) {
            tick(simNodes.current, edgeMap.current, alphaRef.current)
          }
          alphaRef.current *= 0.97
          setFrameCount(c => c + 1)
          rafRef.current = requestAnimationFrame(animate)
        }
      }
      rafRef.current = requestAnimationFrame(animate)
    } catch {
      setError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadGraph(personId)
    return () => cancelAnimationFrame(rafRef.current)
  }, [personId, loadGraph])

  // Recenter on a named node
  function handleNodeClick(node: SimNode) {
    if (node.pinned || node.type !== 'person') return
    setHistory(h => [...h, { id: currentPid, name: currentName }])
    setCurrentPid(node.raw_id)
    setCurrentName(node.name ?? 'Unknown')
    loadGraph(node.raw_id)
  }

  function handleBack() {
    if (history.length === 0) return
    const prev = history[history.length - 1]
    setHistory(h => h.slice(0, -1))
    setCurrentPid(prev.id)
    setCurrentName(prev.name)
    loadGraph(prev.id)
  }

  // ── Render ──────────────────────────────────────────────────────────────

  const nodes   = simNodes.current
  const nm      = new Map(nodes.map(n => [n.id, n]))
  const maxW    = graph ? Math.max(...graph.edges.map(e => e.weight), 1) : 1
  const hovered = hoveredId ? nm.get(hoveredId) : null

  // Tooltip: find edge from hovered node to center
  const hoveredEdge = (hovered && graph)
    ? graph.edges.find(e =>
        (e.source === hoveredId && e.target === graph.center_id) ||
        (e.target === hoveredId && e.source === graph.center_id)
      )
    : null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm"
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        className="relative flex flex-col bg-gray-950 border border-gray-800 rounded-2xl shadow-2xl overflow-hidden"
        style={{ width: 'min(92vw, 960px)', height: 'min(88vh, 740px)' }}
      >
        {/* ── Header ──────────────────────────────────────────────────── */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-800 shrink-0">
          <div className="flex items-center gap-2">
            {history.length > 0 && (
              <button
                onClick={handleBack}
                className="text-gray-500 hover:text-gray-200 transition-colors text-sm mr-1"
                title="Go back"
              >
                ← Back
              </button>
            )}
            <span className="text-gray-500 text-xs font-medium uppercase tracking-widest">
              Connections
            </span>
            <span className="text-gray-500 text-xs">·</span>
            <span className="text-white font-semibold text-sm">{currentName}</span>
            {graph && (
              <span className="text-gray-600 text-xs ml-1">
                ({graph.nodes.length - 1} connection{graph.nodes.length !== 2 ? 's' : ''})
              </span>
            )}
          </div>
          <div className="flex items-center gap-3">
            {onNavigatePerson && (
              <button
                onClick={() => { onClose(); onNavigatePerson(currentPid, currentName) }}
                className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
              >
                View photos →
              </button>
            )}
            <button
              onClick={onClose}
              className="text-gray-600 hover:text-white transition-colors text-lg leading-none"
            >
              ✕
            </button>
          </div>
        </div>

        {/* ── Graph area ──────────────────────────────────────────────── */}
        <div className="relative flex-1 min-h-0">

          {loading && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
            </div>
          )}

          {error && (
            <div className="absolute inset-0 flex items-center justify-center text-gray-500 text-sm">
              Could not load connection data.
            </div>
          )}

          {!loading && graph && graph.nodes.length === 1 && (
            <div className="absolute inset-0 flex items-center justify-center text-gray-500 text-sm">
              No connections found yet — co-occurrence data builds up as photos are processed.
            </div>
          )}

          {!loading && graph && graph.nodes.length > 1 && (
            <svg
              viewBox={`0 0 ${W} ${H}`}
              className="w-full h-full"
              style={{ display: 'block' }}
            >
              {/* Shared clip paths — coordinates are in each <g>'s local space */}
              <defs>
                <clipPath id="vip-cg-clip-0"><circle cx="0" cy="0" r={NODE_R[0]} /></clipPath>
                <clipPath id="vip-cg-clip-1"><circle cx="0" cy="0" r={NODE_R[1]} /></clipPath>
                <clipPath id="vip-cg-clip-2"><circle cx="0" cy="0" r={NODE_R[2]} /></clipPath>
              </defs>

              {/* ── Edges ─────────────────────────────────────────────── */}
              {graph.edges.map((edge: ConnectionGraphEdge) => {
                const a = nm.get(edge.source), b = nm.get(edge.target)
                if (!a || !b) return null
                const frac = edge.weight / maxW
                return (
                  <line
                    key={`${edge.source}--${edge.target}`}
                    x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                    stroke="#6366f1"
                    strokeWidth={0.8 + frac * 2.8}
                    strokeOpacity={0.18 + frac * 0.5}
                  />
                )
              })}

              {/* ── Nodes ─────────────────────────────────────────────── */}
              {nodes.map(node => {
                const d       = Math.min(node.depth, 2)
                const r       = nodeRadius(d)
                const clipId  = `vip-cg-clip-${d}`
                const url     = thumbUrl(node.thumbnail)
                const isCenter = node.pinned
                const isHov   = hoveredId === node.id
                const isNamed = node.type === 'person'
                const label   = node.name ? node.name.split(' ')[0] : '?'
                const canClick = !isCenter && isNamed

                return (
                  <g
                    key={node.id}
                    transform={`translate(${node.x.toFixed(1)},${node.y.toFixed(1)})`}
                    onClick={() => handleNodeClick(node)}
                    onMouseEnter={() => setHoveredId(node.id)}
                    onMouseLeave={() => setHoveredId(null)}
                    style={{ cursor: canClick ? 'pointer' : 'default' }}
                  >
                    {/* Outer glow ring for the centre node */}
                    {isCenter && (
                      <circle r={r + 12} fill="none"
                        stroke="#818cf8" strokeWidth={1} strokeOpacity={0.3} />
                    )}
                    {/* Hover indication */}
                    {isHov && canClick && (
                      <circle r={r + 6} fill="none"
                        stroke="#a5b4fc" strokeWidth={1.5}
                        strokeOpacity={0.8} strokeDasharray="3 2" />
                    )}
                    {/* Face circle background */}
                    <circle r={r}
                      fill={isNamed ? '#1e1b4b' : '#111827'}
                      stroke={isCenter ? '#818cf8' : isNamed ? '#4338ca' : '#374151'}
                      strokeWidth={isCenter ? 2.5 : 1.5}
                    />
                    {/* Thumbnail */}
                    {url ? (
                      <image
                        href={url}
                        x={-r} y={-r} width={r * 2} height={r * 2}
                        clipPath={`url(#${clipId})`}
                        preserveAspectRatio="xMidYMid slice"
                      />
                    ) : (
                      <text
                        textAnchor="middle" dominantBaseline="middle"
                        fontSize={r * 0.85} fill="#4b5563"
                      >
                        👤
                      </text>
                    )}
                    {/* Name label */}
                    <text
                      y={r + 13}
                      textAnchor="middle"
                      fontSize={d === 0 ? 13 : d === 1 ? 11 : 10}
                      fontWeight={d === 0 ? '600' : '400'}
                      fill={d === 0 ? '#e0e7ff' : isNamed ? '#c7d2fe' : '#6b7280'}
                    >
                      {label}
                    </text>
                    {/* Photo count below label for center node */}
                    {isCenter && (
                      <text y={r + 27} textAnchor="middle" fontSize={10} fill="#6366f1">
                        {node.photo_count} photo{node.photo_count !== 1 ? 's' : ''}
                      </text>
                    )}
                  </g>
                )
              })}
            </svg>
          )}
        </div>

        {/* ── Footer / status bar ─────────────────────────────────────── */}
        <div className="shrink-0 flex items-center justify-between px-5 py-2.5 border-t border-gray-800">
          {hovered && !hovered.pinned ? (
            <span className="text-xs text-gray-400">
              <span className="text-white">{hovered.name ?? 'Unnamed face'}</span>
              {hoveredEdge ? (
                <> · {hoveredEdge.weight} shared photo{hoveredEdge.weight !== 1 ? 's' : ''}</>
              ) : null}
              {hovered.type === 'person' && (
                <span className="text-gray-600"> · click to explore</span>
              )}
            </span>
          ) : (
            <span className="text-xs text-gray-700">
              Hover a node for details · Click a named person to explore their connections
            </span>
          )}
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-1.5 text-xs text-gray-700">
              <span className="inline-block w-2.5 h-2.5 rounded-full bg-indigo-900 border border-indigo-700" />
              Named
            </span>
            <span className="flex items-center gap-1.5 text-xs text-gray-700">
              <span className="inline-block w-2.5 h-2.5 rounded-full bg-gray-900 border border-gray-700" />
              Unnamed
            </span>
          </div>
        </div>
      </div>
    </div>
  )
}
