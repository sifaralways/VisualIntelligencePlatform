/**
 * PipelinePanel — always-visible collapsible left panel.
 *
 * Maintains its own WebSocket connection so the live log persists
 * regardless of which main-content tab is active.
 * Mounting happens once; the WS auto-reconnects on drop.
 */

import { useEffect, useRef, useState } from 'react'
import { api, buildProfileWebSocketUrl } from '../api/client'
import type { WsEvent } from '../api/client'

interface Props {
  profileId: string
  collapsed: boolean
  onToggle: () => void
  /** Called whenever a pipeline completes so App can refresh folder list */
  onPipelineComplete?: () => void
  /** Controlled width of the expanded panel in px (default 288 = w-72) */
  width?: number
}

export default function PipelinePanel({ profileId, collapsed, onToggle, onPipelineComplete, width }: Props) {
  const [folder, setFolder]       = useState('')
  const [status, setStatus]       = useState<string>('idle')
  const [events, setEvents]       = useState<WsEvent[]>([])
  const [forceRetag, setForceRetag] = useState(false)
  const [useExistingVipData, setUseExistingVipData] = useState(true)
  const wsRef  = useRef<WebSocket | null>(null)
  const logRef = useRef<HTMLDivElement>(null)

  // ── WebSocket — persistent, auto-reconnecting ─────────────────────────────
  useEffect(() => {
    let cancelled = false

    function connect() {
      if (cancelled) return
      const ws = new WebSocket(buildProfileWebSocketUrl(profileId))
      wsRef.current = ws

      ws.onmessage = (msg) => {
        try {
          const ev: WsEvent = JSON.parse(msg.data)
          if (ev.event === 'ping') return
          setEvents(prev => [...prev.slice(-300), ev])
          if (ev.event === 'pipeline_complete') {
            setStatus('idle')
            onPipelineComplete?.()
          }
          if (ev.event === 'pipeline_start') setStatus('running')
        } catch {}
      }

      ws.onclose = () => {
        if (!cancelled) setTimeout(connect, 3000)
      }
    }

    connect()
    return () => {
      cancelled = true
      wsRef.current?.close()
    }
  }, [profileId, onPipelineComplete])

  // Auto-scroll log to bottom whenever events arrive
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [events])

  // ── Actions ───────────────────────────────────────────────────────────────
  async function startScan() {
    if (!folder.trim()) return
    setEvents([])
    setStatus('running')
    try {
      await api.pipeline.scan(folder.trim(), false, useExistingVipData)
    } catch {
      setStatus('error')
    }
  }

  async function rescanAll() {
    setEvents([])
    setStatus('running')
    try {
      await api.pipeline.rescan(forceRetag)
    } catch (e: unknown) {
      const err = e as { message?: string }
      setStatus('error')
      setEvents([{ event: 'error', message: err?.message ?? 'Rescan failed' }])
    }
  }

  async function migrateModel() {
    if (!window.confirm(
      'This will re-embed all named faces with the new AI model and re-cluster all unnamed faces.\n\n' +
      'Named person assignments are preserved.\n\n' +
      'Only run this after restarting the server with the new model configured.\n\nContinue?'
    )) return
    setEvents([])
    setStatus('running')
    try {
      await api.pipeline.migrateModel()
    } catch (e: unknown) {
      const err = e as { message?: string }
      setStatus('error')
      setEvents([{ event: 'error', message: err?.message ?? 'Model migration failed' }])
    }
  }

  const isRunning = status === 'running'

  // ── Collapsed strip ───────────────────────────────────────────────────────
  if (collapsed) {
    return (
      <div className="w-8 shrink-0 border-r border-gray-800 bg-gray-950 flex flex-col items-center py-3 gap-3">
        <button
          onClick={onToggle}
          title="Expand pipeline panel"
          className="text-gray-500 hover:text-white transition-colors text-xs rotate-90 whitespace-nowrap"
        >
          ▶
        </button>
        {isRunning && (
          <span className="w-2 h-2 rounded-full bg-yellow-400 animate-pulse" title="Pipeline running" />
        )}
      </div>
    )
  }

  // ── Expanded panel ────────────────────────────────────────────────────────
  return (
    <div
      style={{ width: width ?? 288 }}
      className="shrink-0 border-r border-gray-800 bg-gray-950 flex flex-col overflow-hidden"
    >
      {/* Header row */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-800 shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-gray-300 uppercase tracking-wider">⚙️ Pipeline</span>
          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
            isRunning        ? 'bg-yellow-600 text-white' :
            status === 'error' ? 'bg-red-700 text-white' :
                                 'bg-gray-800 text-gray-500'
          }`}>
            {status}
          </span>
        </div>
        <button
          onClick={onToggle}
          title="Collapse panel"
          className="text-gray-500 hover:text-white transition-colors text-sm leading-none px-1"
        >
          ◀
        </button>
      </div>

      {/* Folder input + action buttons */}
      <div className="px-3 pt-3 pb-2 shrink-0 space-y-2">
        <input
          value={folder}
          onChange={e => setFolder(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && startScan()}
          placeholder="/Volumes/Photos"
          className="w-full bg-gray-800 border border-gray-700 rounded-lg px-2.5 py-1.5 text-xs text-white outline-none focus:border-indigo-500 transition-colors"
        />
        <label className="text-[11px] text-gray-300 inline-flex items-center gap-1.5 select-none">
          <input
            type="checkbox"
            checked={useExistingVipData}
            onChange={e => setUseExistingVipData(e.target.checked)}
            className="accent-indigo-500"
          />
          Use existing VIP data if found
        </label>
        <div className="flex gap-1.5">
          <button
            onClick={startScan}
            disabled={isRunning || !folder.trim()}
            className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-xs font-medium rounded-lg px-2 py-1.5 transition-colors"
          >
            {isRunning ? 'Running…' : 'Scan'}
          </button>
          <button
            onClick={rescanAll}
            disabled={isRunning}
            className="flex-1 bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-white text-xs font-medium rounded-lg px-2 py-1.5 transition-colors"
            title="Re-run all pipeline phases on every photo already in the library"
          >
            Rescan All
          </button>
        </div>

        <button
          onClick={migrateModel}
          disabled={isRunning}
          className="w-full bg-orange-800 hover:bg-orange-700 disabled:opacity-40 text-white text-xs font-medium rounded-lg px-2 py-1.5 transition-colors"
          title="Re-embed all named faces with the current model and re-cluster unnamed faces"
        >
          Migrate AI Model
        </button>

        {/* Force retag option */}
        <label className="flex items-start gap-2 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={forceRetag}
            onChange={e => setForceRetag(e.target.checked)}
            className="mt-0.5 accent-indigo-500"
          />
          <span className="text-[10px] text-gray-400 leading-snug">
            Force retag all photos
          </span>
        </label>
        {forceRetag && (
          <p className="text-[10px] text-amber-400 leading-snug">
            ⚠ Re-runs object, animal and place detection on every photo — this will take significantly longer.
          </p>
        )}
      </div>

      {/* Divider */}
      <div className="border-t border-gray-800 mx-3 mb-1 shrink-0" />

      {/* Live event log */}
      <div
        ref={logRef}
        className="flex-1 overflow-y-auto px-3 pb-3 font-mono text-[10px] text-gray-400 space-y-0.5"
      >
        {events.length === 0 ? (
          <span className="text-gray-600">Pipeline events will appear here…</span>
        ) : (
          events.map((ev, i) => (
            <div key={i} className="leading-relaxed">
              <span className={`mr-1 ${
                ev.event === 'pipeline_complete' ? 'text-green-400' :
                ev.event === 'error'             ? 'text-red-400' :
                                                   'text-indigo-400'
              }`}>[{ev.event}]</span>
              {ev.phase    != null && <span className="mr-1">phase={ev.phase}</span>}
              {ev.done     != null && ev.total != null && <span className="mr-1">{ev.done}/{ev.total}</span>}
              {ev.scanned  != null && <span className="mr-1">scanned={ev.scanned}</span>}
              {ev.skipped  != null && <span className="mr-1">skipped={ev.skipped}</span>}
              {ev.clusters != null && <span className="mr-1">clusters={ev.clusters}</span>}
              {ev.merged   != null && <span className="mr-1 text-green-400">merged={ev.merged}</span>}
              {ev.count    != null && <span className="mr-1 text-orange-400">quality={ev.count}</span>}
              {ev.message  != null && <span className="text-yellow-300">{ev.message}</span>}
            </div>
          ))
        )}
      </div>

      {/* Footer tip */}
      <p className="px-3 pb-2 text-[9px] text-gray-600 leading-snug shrink-0">
        iCloud stubs are detected and skipped automatically.
      </p>
    </div>
  )
}
