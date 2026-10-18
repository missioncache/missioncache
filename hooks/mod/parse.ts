// Pure helpers for the MissionCache mod: no `$`, so the tests call them as is.

export const SERVER = 'plugin:missioncache:pm'

// The tools the mod calls itself. Its own `$.mcp.call` raises a `tool.call`
// under these names, which the reporter must not take for the model working.
export const OWN_TOOLS: ReadonlySet<string> = new Set(
  ['report_session_status', 'get_lead_board'].map(
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

// What a long-running tool is doing, for the board's "now" line.
export function toolTarget(e: Record<string, unknown>): string {
  const tool = String(e.tool ?? '')
  const target = e.command ?? e.file_path ?? e.pattern ?? e.url ?? e.description ?? ''
  return clip(`${tool.split('__').pop()}  ${String(target)}`, 60)
}
