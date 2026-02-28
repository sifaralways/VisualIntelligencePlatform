/**
 * AnalysisPanel — editable analysis document view for a single photo.
 *
 * Shows the Rekognition-format document (labels, face attributes, geography)
 * and lets the user rename, delete, confirm, or add labels via the amendments API.
 *
 * Amendments are applied server-side and returned in the merged document;
 * this component just calls the API and refreshes.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api,
  type AnalysisDocument,
  type AnalysisLabel,
  type AnalysisFace,
} from '../api/client'

interface Props {
  mediaId: number
}

// ─── Colour palette by label source ──────────────────────────────────────────
const SOURCE_COLOUR: Record<string, string> = {
  yolov11:    'bg-blue-800/60 text-blue-200 border-blue-700',
  places365:  'bg-yellow-800/60 text-yellow-200 border-yellow-700',
  bioclip:    'bg-green-800/60 text-green-200 border-green-700',
  clip:       'bg-purple-800/60 text-purple-200 border-purple-700',
  insightface:'bg-orange-800/60 text-orange-200 border-orange-700',
  user:       'bg-pink-800/60 text-pink-200 border-pink-700',
}
const DEFAULT_SOURCE_COLOUR = 'bg-gray-700/60 text-gray-300 border-gray-600'

function sourceColour(src: string): string {
  return SOURCE_COLOUR[src] ?? DEFAULT_SOURCE_COLOUR
}

const CONFIDENCE_COLOUR = (c: number) =>
  c >= 90 ? 'text-green-400' : c >= 70 ? 'text-yellow-400' : 'text-red-400'

// ─── Main component ───────────────────────────────────────────────────────────

export default function AnalysisPanel({ mediaId }: Props) {
  const [doc,     setDoc]     = useState<AnalysisDocument | null>(null)
  const [loading, setLoading] = useState(true)
  const [error,   setError]   = useState<string | null>(null)
  const [addingLabel, setAddingLabel] = useState(false)
  const [newLabel,    setNewLabel]    = useState('')
  const [writing,     setWriting]     = useState(false)
  const [writeResult, setWriteResult] = useState<string | null>(null)
  const addInputRef = useRef<HTMLInputElement>(null)

  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    api.analysis.get(mediaId)
      .then(d => { setDoc(d); setLoading(false) })
      .catch(e => { setError(e.message); setLoading(false) })
  }, [mediaId])

  useEffect(() => { load() }, [load])
  useEffect(() => { if (addingLabel) addInputRef.current?.focus() }, [addingLabel])

  // ── Amendment helpers ──────────────────────────────────────────────────────
  const amend = async (
    labelName: string,
    action: 'rename' | 'delete' | 'add' | 'confirm',
    userValue?: string,
  ) => {
    await api.analysis.amend(mediaId, { label_name: labelName, action, user_value: userValue })
    load()
  }

  const undoAmend = async (labelName: string) => {
    await api.analysis.deleteAmend(mediaId, labelName)
    load()
  }

  const handleAddLabel = async () => {
    const trimmed = newLabel.trim()
    if (!trimmed) return
    await amend(trimmed, 'add')
    setNewLabel('')
    setAddingLabel(false)
  }

  const writeToExif = async () => {
    setWriting(true)
    setWriteResult(null)
    try {
      const r = await api.writeback.writeOne(mediaId)
      if (r.status === 'written') {
        setWriteResult(`✓ Written — ${r.fields_written?.length ?? 0} fields embedded`)
      } else {
        setWriteResult(`⚡ Skipped — ${r.reason ?? 'no metadata to write'}`)
      }
    } catch (e: any) {
      setWriteResult(`✕ ${e.message}`)
    } finally {
      setWriting(false)
    }
  }

  const exportJson = () => {
    const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' })
    const url  = URL.createObjectURL(blob)
    const a    = document.createElement('a')
    a.href     = url
    a.download = `vip_analysis_${mediaId}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  if (loading) return <div className="text-gray-500 text-sm py-6 text-center">Loading analysis…</div>
  if (error)   return (
    <div className="text-center py-6 space-y-2">
      <p className="text-red-400 text-sm">{error}</p>
      <button onClick={load} className="text-xs text-gray-400 hover:text-white underline">Retry</button>
    </div>
  )
  if (!doc) return null

  return (
    <div className="space-y-5 text-sm">

      {/* ── Document meta ──────────────────────────────────────────────── */}
      <Section title="File Info" icon="📄">
        <div className="space-y-1 text-xs text-gray-400 font-mono break-all">
          {doc.vip_id    && <p><span className="text-gray-600">ID  </span> {doc.vip_id}</p>}
          {doc.camera    && <p><span className="text-gray-600">Cam </span> {doc.camera}</p>}
          {doc.date_taken && <p><span className="text-gray-600">Date</span> {doc.date_taken.replace('T', ' ')}</p>}
          {doc.image_size.width && (
            <p><span className="text-gray-600">Size</span> {doc.image_size.width}×{doc.image_size.height}</p>
          )}
          <p><span className="text-gray-600">Mdl </span> {doc.model_version}</p>
        </div>
      </Section>

      {/* ── Faces (rich attributes) ────────────────────────────────────── */}
      {doc.Faces.length > 0 && (
        <Section title={`People (${doc.Faces.length})`} icon="👤">
          <div className="space-y-3">
            {doc.Faces.map(f => <FaceCard key={f.face_id} face={f} mediaId={mediaId} />)}
          </div>
        </Section>
      )}

      {/* ── Labels ────────────────────────────────────────────────────── */}
      <Section
        title={`Labels (${doc.Labels.length})`}
        icon="🏷️"
        action={
          <button
            onClick={() => setAddingLabel(v => !v)}
            className="text-xs text-gray-400 hover:text-white border border-gray-700 rounded px-2 py-0.5"
          >
            + Add
          </button>
        }
      >
        <div className="flex flex-wrap gap-1.5">
          {doc.Labels.map(l => (
            <LabelChip
              key={l.Name}
              label={l}
              onRename={newName => amend(l.OriginalName ?? l.Name, 'rename', newName)}
              onDelete={() => amend(l.OriginalName ?? l.Name, 'delete')}
              onConfirm={() => amend(l.Name, 'confirm')}
              onUndo={() => undoAmend(l.OriginalName ?? l.Name)}
            />
          ))}
        </div>

        {/* Add label input */}
        {addingLabel && (
          <div className="flex gap-2 mt-2">
            <input
              ref={addInputRef}
              value={newLabel}
              onChange={e => setNewLabel(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') handleAddLabel()
                if (e.key === 'Escape') { setAddingLabel(false); setNewLabel('') }
              }}
              placeholder="Label name…"
              className="flex-1 bg-gray-800 border border-gray-600 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-blue-500"
            />
            <button
              onClick={handleAddLabel}
              className="text-xs bg-blue-700 hover:bg-blue-600 text-white rounded px-3 py-1"
            >
              Add
            </button>
          </div>
        )}
      </Section>

      {/* ── Geography ────────────────────────────────────────────────── */}
      {(doc.Geography.gps_lat != null || doc.Geography.labels.length > 0) && (
        <Section title="Geography" icon="🌍">
          {doc.Geography.gps_lat != null && (
            <p className="text-xs text-gray-400 font-mono mb-1">
              {doc.Geography.gps_lat.toFixed(5)}, {doc.Geography.gps_lon?.toFixed(5)}
            </p>
          )}
          <div className="flex flex-wrap gap-1">
            {doc.Geography.labels.map(l => (
              <span key={l} className="text-xs bg-yellow-900/40 text-yellow-300 border border-yellow-800 rounded-full px-2 py-0.5">
                {l}
              </span>
            ))}
          </div>
        </Section>
      )}

      {/* ── Actions ──────────────────────────────────────────────────── */}
      <div className="pt-1 border-t border-gray-800 space-y-1.5">
        <div className="flex gap-2">
          <button
            onClick={exportJson}
            className="flex-1 text-xs text-gray-300 hover:text-white border border-gray-700 hover:border-gray-500 rounded py-1.5 transition-colors"
          >
            ⬇ Export JSON
          </button>
          <button
            onClick={() => api.analysis.rebuild(mediaId).then(load)}
            className="flex-1 text-xs text-gray-300 hover:text-white border border-gray-700 hover:border-gray-500 rounded py-1.5 transition-colors"
          >
            ↻ Rebuild
          </button>
          <button
            onClick={writeToExif}
            disabled={writing}
            className="flex-1 text-xs text-gray-300 hover:text-white border border-gray-700 hover:border-gray-500 rounded py-1.5 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {writing ? '…' : '✍ Write to EXIF'}
          </button>
        </div>
        {writeResult && (
          <p className={`text-[11px] text-center ${
            writeResult.startsWith('✓') ? 'text-green-400' :
            writeResult.startsWith('✕') ? 'text-red-400'   : 'text-yellow-400'
          }`}>
            {writeResult}
          </p>
        )}
      </div>
    </div>
  )
}

// ─── FaceCard ─────────────────────────────────────────────────────────────────

function FaceCard({ face, mediaId }: { face: AnalysisFace; mediaId: number }) {
  const { AgeRange, Gender, Pose, Quality, Emotions } = face
  const dominantEmotion = Emotions
    ? [...Emotions].sort((a, b) => b.Confidence - a.Confidence)[0]
    : null

  return (
    <div className="bg-gray-800/60 border border-gray-700 rounded-lg p-3 space-y-2">
      {/* Person name */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <img
            src={`/api/faces/${face.face_id}/thumbnail`}
            alt=""
            className="w-10 h-10 rounded-full object-cover bg-gray-700"
            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
          <div>
            <p className="text-white font-medium text-sm">{face.person_name ?? '?'}</p>
            <p className="text-gray-500 text-[10px]">
              conf {(face.detection_conf * 100).toFixed(0)}%
            </p>
          </div>
        </div>
        {dominantEmotion && (
          <span className="text-xs text-gray-400 capitalize">
            {dominantEmotion.Type.toLowerCase()}
          </span>
        )}
      </div>

      {/* Attribute grid */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] text-gray-400">
        {AgeRange && (
          <Attr label="Age" value={`${AgeRange.Low}–${AgeRange.High}`} />
        )}
        {Gender && (
          <Attr label="Gender" value={`${Gender.Value} (${Gender.Confidence.toFixed(0)}%)`} />
        )}
        {Quality && (
          <>
            <Attr label="Brightness" value={`${Quality.Brightness.toFixed(0)}%`} />
            <Attr label="Sharpness"  value={`${Quality.Sharpness.toFixed(0)}%`} />
          </>
        )}
        {Pose && (
          <>
            <Attr label="Yaw"   value={`${Pose.Yaw.toFixed(1)}°`} />
            <Attr label="Pitch" value={`${Pose.Pitch.toFixed(1)}°`} />
            <Attr label="Roll"  value={`${Pose.Roll.toFixed(1)}°`} />
          </>
        )}
      </div>

      {/* Emotion bar */}
      {Emotions && Emotions.length > 0 && (
        <div className="space-y-0.5">
          {Emotions.filter(e => e.Confidence > 1).map(e => (
            <div key={e.Type} className="flex items-center gap-1.5">
              <span className="text-[10px] text-gray-500 w-16 capitalize">
                {e.Type.toLowerCase()}
              </span>
              <div className="flex-1 bg-gray-700 rounded-full h-1">
                <div
                  className="bg-blue-500 h-1 rounded-full"
                  style={{ width: `${Math.min(100, e.Confidence)}%` }}
                />
              </div>
              <span className="text-[10px] text-gray-500 w-8 text-right">
                {e.Confidence.toFixed(0)}%
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ─── LabelChip ────────────────────────────────────────────────────────────────

interface LabelChipProps {
  label: AnalysisLabel
  onRename: (newName: string) => void
  onDelete: () => void
  onConfirm: () => void
  onUndo: () => void
}

function LabelChip({ label, onRename, onDelete, onConfirm, onUndo }: LabelChipProps) {
  const [editing, setEditing]   = useState(false)
  const [value,   setValue]     = useState(label.Name)
  const [hovered, setHovered]   = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { if (editing) inputRef.current?.focus() }, [editing])
  useEffect(() => { setValue(label.Name) }, [label.Name])

  const commit = () => {
    const trimmed = value.trim()
    if (trimmed && trimmed !== label.Name) onRename(trimmed)
    setEditing(false)
  }

  const colourClass = label.UserEdited
    ? 'bg-pink-800/60 text-pink-200 border-pink-700'
    : label.UserConfirmed
    ? 'bg-teal-800/60 text-teal-200 border-teal-700'
    : sourceColour(label.Source)

  if (editing) {
    return (
      <input
        ref={inputRef}
        value={value}
        onChange={e => setValue(e.target.value)}
        onBlur={commit}
        onKeyDown={e => {
          if (e.key === 'Enter') commit()
          if (e.key === 'Escape') { setEditing(false); setValue(label.Name) }
        }}
        className="border border-blue-500 rounded-full px-2 py-0.5 text-xs bg-gray-900 text-white w-28 focus:outline-none"
      />
    )
  }

  return (
    <div
      className={`relative flex items-center gap-1 border rounded-full px-2 py-0.5 text-xs cursor-default group ${colourClass}`}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {/* Confidence dot */}
      <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
        label.Confidence >= 90 ? 'bg-green-400' :
        label.Confidence >= 70 ? 'bg-yellow-400' : 'bg-red-400'
      }`} />

      {/* Label name */}
      <span onDoubleClick={() => setEditing(true)} title={`${label.Confidence.toFixed(1)}% · ${label.Source}`}>
        {label.Name}
        {label.UserEdited && <span className="ml-0.5 opacity-60 text-[9px]">✎</span>}
        {label.UserConfirmed && <span className="ml-0.5 opacity-60 text-[9px]">✓</span>}
      </span>

      {/* Hover action bar */}
      {hovered && !editing && (
        <span className="flex items-center gap-0.5 ml-1">
          {!label.UserConfirmed && (
            <IconBtn title="Confirm" onClick={onConfirm}>✓</IconBtn>
          )}
          <IconBtn title="Rename" onClick={() => setEditing(true)}>✎</IconBtn>
          {(label.UserEdited || label.UserConfirmed) && (
            <IconBtn title="Undo edit" onClick={onUndo}>↩</IconBtn>
          )}
          <IconBtn title="Delete" onClick={onDelete} danger>✕</IconBtn>
        </span>
      )}
    </div>
  )
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function Section({
  title, icon, children, action,
}: { title: string; icon: string; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs font-semibold text-gray-400 uppercase tracking-widest">
          {icon} {title}
        </p>
        {action}
      </div>
      {children}
    </div>
  )
}

function Attr({ label, value }: { label: string; value: string }) {
  return (
    <p>
      <span className="text-gray-600">{label}: </span>
      <span className="text-gray-300">{value}</span>
    </p>
  )
}

function IconBtn({
  children, onClick, title, danger = false,
}: { children: React.ReactNode; onClick: () => void; title: string; danger?: boolean }) {
  return (
    <button
      title={title}
      onClick={e => { e.stopPropagation(); onClick() }}
      className={`w-4 h-4 flex items-center justify-center rounded text-[10px] leading-none
        ${danger ? 'hover:bg-red-700/60 hover:text-red-200' : 'hover:bg-gray-600 hover:text-white'}
        text-gray-400`}
    >
      {children}
    </button>
  )
}
