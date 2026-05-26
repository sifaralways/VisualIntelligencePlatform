import { useState } from 'react'
import { api } from '../api/client'
import type {
  ChatFaceResult,
  ChatResponse,
  ChatV2Response,
  NaturalSearchResult,
  Person,
  ToolCallTrace,
} from '../api/client'
import PhotoDetail from '../components/PhotoDetail'

interface AssistantPageProps {
  onOpenSearch: (query: string) => void
  mode?: 'v1' | 'v2'
}

interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
}

interface AssistantUiResponse {
  conversation_id: string
  reply_text: string
  results: NaturalSearchResult[]
  face_results?: ChatFaceResult[]
  face_total_count?: number
  count: number
  intent?: 'SQL_ONLY' | 'CLIP_ONLY' | 'HYBRID'
  explanation?: string
  action_payload: {
    query?: string
    offset?: number
    next_offset?: number | null
    has_more?: boolean
  }
  open_search: boolean
  tool_trace?: ToolCallTrace[]
}

function normalizeV1Response(res: ChatResponse): AssistantUiResponse {
  return {
    conversation_id: res.conversation_id,
    reply_text: res.reply_text,
    results: res.results,
    face_results: res.face_results,
    face_total_count: res.face_total_count,
    count: res.count,
    intent: res.intent,
    explanation: res.explanation,
    action_payload: res.action_payload,
    open_search: res.action === 'open_search',
  }
}

function normalizeV2Response(res: ChatV2Response): AssistantUiResponse {
  return {
    conversation_id: res.conversation_id,
    reply_text: res.reply_text,
    results: res.results,
    face_results: res.face_results,
    count: res.count,
    intent: res.intent,
    explanation: res.explanation,
    action_payload: res.action_payload,
    open_search: res.action_type === 'open_search',
    tool_trace: res.tool_trace,
  }
}

export default function AssistantPage({ onOpenSearch, mode = 'v1' }: AssistantPageProps) {
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [conversationId, setConversationId] = useState<string>('')
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'assistant',
      text: 'Ask me about your library: counts, top people, or photo searches.',
    },
  ])
  const [lastResponse, setLastResponse] = useState<AssistantUiResponse | null>(null)
  const [selected, setSelected] = useState<NaturalSearchResult | null>(null)
  const [namingFace, setNamingFace] = useState<ChatFaceResult | null>(null)
  const [nameInput, setNameInput] = useState('')
  const [knownPersons, setKnownPersons] = useState<Person[]>([])
  const [knownPersonsLoaded, setKnownPersonsLoaded] = useState(false)
  const [faceActionBusy, setFaceActionBusy] = useState(false)
  const [faceActionMessage, setFaceActionMessage] = useState<string>('')
  const [lastUserMessage, setLastUserMessage] = useState<string>('')
  const [loadingMore, setLoadingMore] = useState(false)

  async function ensureKnownPersonsLoaded(): Promise<Person[]> {
    if (knownPersonsLoaded) return knownPersons
    const list = await api.persons.list()
    setKnownPersons(list)
    setKnownPersonsLoaded(true)
    return list
  }

  function removeResolvedFaceResults(face: ChatFaceResult) {
    setLastResponse(prev => {
      if (!prev?.face_results?.length) return prev
      const nextFaceResults = prev.face_results.filter(f => {
        if (face.cluster_id != null) return f.cluster_id !== face.cluster_id
        return f.face_id !== face.face_id
      })
      return {
        ...prev,
        face_results: nextFaceResults,
        count: nextFaceResults.length,
      }
    })
  }

  async function handleIgnoreAlways(face: ChatFaceResult) {
    if (face.cluster_id == null) {
      setFaceActionMessage('This face is not in a cluster, so Ignore Always is unavailable.')
      return
    }
    setFaceActionBusy(true)
    try {
      await api.clusters.ignore(face.cluster_id)
      removeResolvedFaceResults(face)
      setFaceActionMessage('Ignored this face cluster. It will be hidden from unnamed faces.')
    } catch (e: any) {
      setFaceActionMessage(String(e?.message || e || 'Failed to ignore face cluster'))
    } finally {
      setFaceActionBusy(false)
    }
  }

  async function handleConfirmName() {
    if (!namingFace) return
    const name = nameInput.trim()
    if (!name) return
    setFaceActionBusy(true)
    try {
      const persons = await ensureKnownPersonsLoaded()
      const existing = persons.find(p => p.name?.toLowerCase() === name.toLowerCase())

      if (namingFace.cluster_id != null) {
        if (existing) {
          await api.persons.addCluster(existing.id, namingFace.cluster_id)
        } else {
          await api.persons.fromCluster(namingFace.cluster_id, name)
        }
      } else {
        await api.persons.assignFace(namingFace.face_id, name)
      }
      removeResolvedFaceResults(namingFace)
      setNamingFace(null)
      setNameInput('')
      setKnownPersonsLoaded(false)
      setFaceActionMessage(`Assigned name '${name}' successfully.`)
    } catch (e: any) {
      setFaceActionMessage(String(e?.message || e || 'Failed to assign name'))
    } finally {
      setFaceActionBusy(false)
    }
  }

  async function sendMessage() {
    const message = input.trim()
    if (!message || loading) return

    setMessages(prev => [...prev, { role: 'user', text: message }])
    setInput('')
    setLoading(true)
    setLastUserMessage(message)
    try {
      const res = mode === 'v2'
        ? normalizeV2Response(await api.chat.messageV2({ message, limit: 100, conversation_id: conversationId || undefined }))
        : normalizeV1Response(await api.chat.message({ message, limit: 100, conversation_id: conversationId || undefined }))
      setLastResponse(res)
      setMessages(prev => [...prev, { role: 'assistant', text: res.reply_text }])
      setConversationId(res.conversation_id || conversationId)
    } catch (e: any) {
      setMessages(prev => [
        ...prev,
        { role: 'assistant', text: String(e?.message || e || 'Chat failed') },
      ])
      setLastResponse(null)
    } finally {
      setLoading(false)
    }
  }

  async function loadMore() {
    if (!lastResponse || loadingMore) return
    const nextOffset = lastResponse.action_payload?.next_offset
    if (nextOffset == null) return

    setLoadingMore(true)
    try {
      const res = mode === 'v2'
        ? normalizeV2Response(await api.chat.messageV2({
            message: lastUserMessage,
            limit: 100,
            offset: nextOffset,
            conversation_id: conversationId || undefined,
          }))
        : normalizeV1Response(await api.chat.message({
            message: lastUserMessage,
            limit: 100,
            offset: nextOffset,
            conversation_id: conversationId || undefined,
          }))
      // Append results/face_results rather than replacing
      setLastResponse(prev => {
        if (!prev) return res
        return {
          ...res,
          results: [...prev.results, ...res.results],
          face_results: res.face_results
            ? [...(prev.face_results ?? []), ...res.face_results]
            : prev.face_results,
          count: res.count,
        }
      })
    } catch {
      // Silently ignore load-more failures
    } finally {
      setLoadingMore(false)
    }
  }

  return (
    <div className="flex flex-col gap-4 h-full">
      <h1 className="text-xl font-semibold">{mode === 'v2' ? 'Assistant V2' : 'Assistant'}</h1>

      <div className="rounded-xl border border-gray-800 bg-gray-900/60 p-3 h-72 overflow-y-auto space-y-3">
        {messages.map((m, idx) => (
          <div
            key={idx}
            className={`max-w-[85%] px-3 py-2 rounded-lg text-sm whitespace-pre-wrap ${
              m.role === 'user'
                ? 'ml-auto bg-indigo-600 text-white'
                : 'bg-gray-800 text-gray-200'
            }`}
          >
            {m.text}
          </div>
        ))}
        {loading && (
          <div className="max-w-[85%] px-3 py-2 rounded-lg text-sm bg-gray-800 text-gray-400">
            Thinking...
          </div>
        )}
      </div>

      <div className="flex gap-2">
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && sendMessage()}
          placeholder="Ask: top 5 people, how many faces named, show Person A with Person B by ocean"
          className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-indigo-500"
        />
        <button
          onClick={sendMessage}
          disabled={loading}
          className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded-lg px-4 py-2 text-sm font-medium"
        >
          Send
        </button>
      </div>

      {lastResponse?.open_search && lastResponse.action_payload?.query && (
        <button
          onClick={() => onOpenSearch(lastResponse.action_payload?.query || '')}
          className="self-start text-xs bg-gray-800 hover:bg-gray-700 text-gray-200 rounded-lg px-3 py-1.5"
        >
          Open these results in Search
        </button>
      )}

      {lastResponse && (
        <p className="text-xs text-gray-500">
          {lastResponse.face_results?.length
            ? (
                lastResponse.face_total_count && lastResponse.face_total_count > lastResponse.face_results.length
                  ? `Showing ${lastResponse.face_results.length} of ${lastResponse.face_total_count} unnamed face thumbnails`
                  : `${lastResponse.face_results.length} unnamed face thumbnail${lastResponse.face_results.length !== 1 ? 's' : ''}`
              )
            : `${lastResponse.count} result${lastResponse.count !== 1 ? 's' : ''}`}
          {lastResponse.intent ? ` • Route: ${lastResponse.intent}` : ''}
          {mode === 'v2' ? ' • API: V2' : ' • API: V1'}
        </p>
      )}

      {mode === 'v2' && lastResponse?.tool_trace?.length ? (
        <div className="rounded-lg border border-gray-800 bg-gray-900/50 p-2.5">
          <p className="text-[11px] text-gray-400 mb-1.5">Tool trace</p>
          <div className="space-y-1.5">
            {lastResponse.tool_trace.map((trace, idx) => (
              <div key={`${trace.tool_name}-${idx}`} className="text-[11px] text-gray-300">
                <span className="text-indigo-300">{idx + 1}.</span>{' '}
                <span className="font-medium">{trace.tool_name}</span>{' '}
                <span className={trace.status === 'ok' ? 'text-emerald-300' : 'text-red-300'}>
                  {trace.status}
                </span>{' '}
                <span className="text-gray-500">({trace.latency_ms} ms)</span>
                {trace.notes ? <span className="text-gray-400"> • {trace.notes}</span> : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {faceActionMessage && (
        <p className="text-xs text-indigo-300">{faceActionMessage}</p>
      )}

      {lastResponse?.face_results?.length ? (
        <div>
          <p className="text-xs text-gray-400 mb-2">Unnamed faces</p>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(110px,1fr))] gap-1.5">
            {lastResponse.face_results.map(f => (
              <AssistantFaceTile
                key={f.face_id}
                face={f}
                disabled={faceActionBusy}
                onPreview={() => setSelected({
                  media_id: f.media_id,
                  file_path: f.file_path,
                  date_taken: f.date_taken,
                  persons: [],
                  tags: [],
                })}
                onName={() => {
                  void ensureKnownPersonsLoaded()
                  setNamingFace(f)
                  setNameInput('')
                }}
                onIgnore={() => { void handleIgnoreAlways(f) }}
              />
            ))}
          </div>
          {lastResponse.action_payload?.has_more && (
            <button
              onClick={() => void loadMore()}
              disabled={loadingMore}
              className="mt-3 w-full text-xs py-2 rounded-lg border border-gray-700 bg-gray-800/60 text-gray-300 hover:text-white hover:bg-gray-700 hover:border-gray-600 disabled:opacity-40 transition-colors"
            >
              {loadingMore ? 'Loading…' : `Load more faces (showing ${lastResponse.face_results.length}${lastResponse.face_total_count ? ` of ${lastResponse.face_total_count}` : ''})`}
            </button>
          )}
        </div>
      ) : null}

      {namingFace && (
        <div className="rounded-lg border border-gray-700 bg-gray-900/80 p-3 space-y-2">
          <p className="text-xs text-gray-300">Name this unnamed face</p>
          <input
            value={nameInput}
            onChange={e => setNameInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && void handleConfirmName()}
            placeholder="Enter person name"
            list="assistant-known-persons"
            className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-white outline-none focus:border-indigo-500"
          />
          <datalist id="assistant-known-persons">
            {knownPersons
              .filter(p => !!p.name)
              .map(p => (
                <option key={p.id} value={p.name!} />
              ))}
          </datalist>
          <div className="flex gap-2">
            <button
              onClick={() => void handleConfirmName()}
              disabled={faceActionBusy || !nameInput.trim()}
              className="text-xs bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white rounded px-2.5 py-1"
            >
              Save Name
            </button>
            <button
              onClick={() => { setNamingFace(null); setNameInput('') }}
              disabled={faceActionBusy}
              className="text-xs bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-200 rounded px-2.5 py-1"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {lastResponse?.results?.length ? (
        <div>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-1">
            {lastResponse.results.map(r => (
              <AssistantTile key={r.media_id} result={r} onClick={() => setSelected(r)} />
            ))}
          </div>
          {lastResponse.action_payload?.has_more && (
            <button
              onClick={() => void loadMore()}
              disabled={loadingMore}
              className="mt-3 w-full text-xs py-2 rounded-lg border border-gray-700 bg-gray-800/60 text-gray-300 hover:text-white hover:bg-gray-700 hover:border-gray-600 disabled:opacity-40 transition-colors"
            >
              {loadingMore ? 'Loading…' : `Load more photos (showing ${lastResponse.results.length} of ${lastResponse.count})`}
            </button>
          )}
        </div>
      ) : null}

      {selected && (
        <PhotoDetail
          mediaId={selected.media_id}
          filePath={selected.file_path}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  )
}

function AssistantFaceTile({
  face,
  disabled,
  onPreview,
  onName,
  onIgnore,
}: {
  face: ChatFaceResult
  disabled?: boolean
  onPreview: () => void
  onName: () => void
  onIgnore: () => void
}) {
  const [errored, setErrored] = useState(false)
  const src = api.faces.thumbnailUrl(face.face_id)
  const filename = face.file_path.split('/').pop() ?? ''
  const date = face.date_taken ? face.date_taken.slice(0, 10) : null

  return (
    <div className="space-y-1">
      <button
        onClick={onPreview}
        title={`${filename}${date ? `  •  ${date}` : ''}`}
        className="relative aspect-square w-full bg-gray-900 rounded overflow-hidden group transition-all focus:outline-none hover:ring-2 hover:ring-indigo-400 focus:ring-2 focus:ring-indigo-400"
      >
        {errored ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center text-gray-600 gap-1">
            <span className="text-xl">🙂</span>
            <span className="text-[10px] truncate px-1 max-w-full">Face #{face.face_id}</span>
          </div>
        ) : (
          <img
            src={src}
            alt={`Face ${face.face_id}`}
            loading="lazy"
            onError={() => setErrored(true)}
            className="w-full h-full object-cover transition-transform duration-200 group-hover:scale-105"
          />
        )}
        <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 via-black/40 to-transparent p-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <p className="text-white text-[10px] truncate">{filename}</p>
        </div>
      </button>
      <div className="grid grid-cols-2 gap-1">
        <button
          onClick={onName}
          disabled={disabled}
          className="text-[10px] bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-200 rounded px-1.5 py-1"
        >
          Name
        </button>
        <button
          onClick={onIgnore}
          disabled={disabled || face.cluster_id == null}
          className="text-[10px] bg-red-900/70 hover:bg-red-800 disabled:opacity-30 text-red-100 rounded px-1.5 py-1"
          title={face.cluster_id == null ? 'Ignore Always requires a clustered face' : 'Always ignore this cluster'}
        >
          Ignore Always
        </button>
      </div>
    </div>
  )
}

function AssistantTile({ result, onClick }: { result: NaturalSearchResult; onClick: () => void }) {
  const [errored, setErrored] = useState(false)
  const src = api.media.thumbnailUrl(result.media_id)
  const filename = result.file_path.split('/').pop() ?? ''
  const date = result.date_taken ? result.date_taken.slice(0, 10) : null

  return (
    <button
      onClick={onClick}
      title={`${filename}${date ? `  •  ${date}` : ''}`}
      className="relative aspect-square bg-gray-900 rounded overflow-hidden group transition-all focus:outline-none hover:ring-2 hover:ring-indigo-400 focus:ring-2 focus:ring-indigo-400"
    >
      {errored ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center text-gray-600 gap-1">
          <span className="text-2xl">📷</span>
          <span className="text-xs truncate px-1 max-w-full">{filename}</span>
        </div>
      ) : (
        <img
          src={src}
          alt={filename}
          loading="lazy"
          onError={() => setErrored(true)}
          className="w-full h-full object-cover transition-transform duration-200 group-hover:scale-105"
        />
      )}
      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 via-black/40 to-transparent p-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
        <p className="text-white text-[10px] truncate">{filename}</p>
        {date && <p className="text-gray-300 text-[9px]">{date}</p>}
      </div>
    </button>
  )
}
