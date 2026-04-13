import { useState } from 'react'
import { api } from '../api/client'
import type { ChatResponse, NaturalSearchResult } from '../api/client'
import PhotoDetail from '../components/PhotoDetail'

interface AssistantPageProps {
  onOpenSearch: (query: string) => void
}

interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
}

export default function AssistantPage({ onOpenSearch }: AssistantPageProps) {
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [conversationId, setConversationId] = useState<string>('')
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'assistant',
      text: 'Ask me about your library: counts, top people, or photo searches.',
    },
  ])
  const [lastResponse, setLastResponse] = useState<ChatResponse | null>(null)
  const [selected, setSelected] = useState<NaturalSearchResult | null>(null)

  async function sendMessage() {
    const message = input.trim()
    if (!message || loading) return

    setMessages(prev => [...prev, { role: 'user', text: message }])
    setInput('')
    setLoading(true)
    try {
      const res = await api.chat.message({ message, limit: 100, conversation_id: conversationId || undefined })
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

  return (
    <div className="flex flex-col gap-4 h-full">
      <h1 className="text-xl font-semibold">Assistant</h1>

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

      {lastResponse?.action === 'open_search' && lastResponse.action_payload?.query && (
        <button
          onClick={() => onOpenSearch(lastResponse.action_payload?.query || '')}
          className="self-start text-xs bg-gray-800 hover:bg-gray-700 text-gray-200 rounded-lg px-3 py-1.5"
        >
          Open these results in Search
        </button>
      )}

      {lastResponse && (
        <p className="text-xs text-gray-500">
          {lastResponse.count} result{lastResponse.count !== 1 ? 's' : ''}
          {lastResponse.intent ? ` • Route: ${lastResponse.intent}` : ''}
        </p>
      )}

      {lastResponse?.results?.length ? (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-1">
          {lastResponse.results.map(r => (
            <AssistantTile key={r.media_id} result={r} onClick={() => setSelected(r)} />
          ))}
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
