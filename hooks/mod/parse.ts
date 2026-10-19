// Pure helpers for the MissionCache mod: no `$`, so the tests call them as is.

export const SERVER = 'plugin:missioncache:pm'

// The tools the mod calls itself. Its own `$.mcp.call` raises a `tool.call`
// under these names, which the reporter must not take for the model working.
export const OWN_TOOLS: ReadonlySet<string> = new Set(
  ['report_session_status', 'get_lead_board', 'prepare_relay', 'record_relay'].map(
    t => `mcp__plugin_missioncache_pm__${t}`,
  ),
)

type McpResult = { content: readonly unknown[]; isError?: boolean }

// The server answers with a JSON text block, never structuredContent.
export function decodeMcp<T>(r: McpResult): T {
  const block = r.content.find(b => typeof b === 'object' && b !== null && 'text' in b) as { text: string } | undefined
  if (!block) throw new Error('the MissionCache server answered without a text block')
  if (r.isError) throw new Error(block.text)
  return JSON.parse(block.text) as T
}

// One line, no control or format characters, clipped with an ellipsis.
export function clip(text: string | null | undefined, max: number): string {
  const line = String(text ?? '').replace(/[\p{Cc}\p{Cf}]/gu, ' ').replace(/\s+/g, ' ').trim()
  return line.length <= max ? line : `${line.slice(0, max - 1)}…`
}

// The board shows what was asked and said, not how the transcript wraps it: a
// slash command arrives as <command-name>/x</command-name> tags, an answer as markdown.
export function plain(text: string | null | undefined): string {
  const s = String(text ?? '')
  const cmd = /<command-name>([^<]*)<\/command-name>/.exec(s)
  const body = cmd ? `${cmd[1]} ${/<command-args>([^<]*)<\/command-args>/.exec(s)?.[1] ?? ''}` : s
  return body
    .replace(/<\/?[a-z][\w-]*>/g, ' ')
    .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+/gm, '')
    .replace(/\*\*|__|`/g, '')
}

// What a permission prompt asks the person, in words, for "Waiting for you to ...".
const APPROVE: Record<string, string> = { Bash: 'a command', Edit: 'a file edit', Write: 'a new file', WebFetch: 'a web request' }
export const approveReason = (tool: string): string => `approve ${APPROVE[tool] ?? `the ${tool.split('__').pop()} tool`}`

// What a long-running tool is doing, in words, for the board's "now" line: a
// Bash or Agent call carries the model's own description of itself.
export function toolTarget(e: Record<string, unknown>): string {
  const tool = String(e.tool ?? '').split('__').pop() ?? ''
  const file = String(e.file_path ?? e.notebook_path ?? '').split('/').pop()
  const what = e.description && ['Bash', 'Agent', 'Task'].includes(tool) ? String(e.description)
    : ['Edit', 'Write', 'NotebookEdit'].includes(tool) ? `Edit ${file}`
    : tool === 'Read' ? `Read ${file}`
    : ['Grep', 'Glob'].includes(tool) ? 'Search the code'
    : ['WebFetch', 'WebSearch'].includes(tool) ? 'Read the web'
    : tool === 'Bash' ? 'Run a command'
    : `Use ${tool}`
  return clip(what, 60)
}

// --- relay ----------------------------------------------------------------

// One instruction from the lead, as prepare_relay builds it: a line
// `mc-relay/1 {json}`. The engine wraps a peer's text in its own element and
// the envelope sits on a line of its own inside it (measured), so it must be
// the whole line. It is read again unescaped if the wrapper escaped it.
export type Relay = { id: string; from: string; to: string; ts: string; text: string; project?: string }
export const RELAY_TEXT_MAX = 500
const RELAY_LINE = /^mc-relay\/1 (\{.*\})\s*$/m
const ENTITIES: Record<string, string> = { '&quot;': '"', '&apos;': "'", '&lt;': '<', '&gt;': '>', '&amp;': '&' }
const unescape = (s: string) => s.replace(/&(quot|apos|lt|gt|amp|#\d+);/g, e => ENTITIES[e] ?? String.fromCharCode(Number(e.slice(2, -1))))

function readRelay(json: string): Relay | null {
  try {
    const r = JSON.parse(json) as Record<string, unknown>
    const fields = ['id', 'from', 'to', 'ts', 'text'] as const
    if (!fields.every(k => typeof r[k] === 'string' && r[k])) return null
    const project = typeof r.project === 'string' && r.project ? r.project : undefined
    return { id: r.id, from: r.from, to: r.to, ts: r.ts, text: r.text, project } as Relay
  } catch {
    return null
  }
}

// A line shaped like a relay, read or not: one that does not parse is still
// a relay and never reaches the model.
export const looksLikeRelay = (text: string): boolean => RELAY_LINE.test(text)

export function parseRelay(text: string): Relay | null {
  const m = RELAY_LINE.exec(text)
  if (!m) return null
  return readRelay(m[1]) ?? readRelay(unescape(m[1]))
}

// `/missioncache:relay <project> <text>`: the first word names the project.
export function relayArgs(args: string): { project_name: string; text: string } | null {
  const m = /^\s*(\S+)\s+([\s\S]*\S)\s*$/.exec(args)
  return m ? { project_name: m[1], text: m[2] } : null
}
