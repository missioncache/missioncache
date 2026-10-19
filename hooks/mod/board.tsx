// The lead band: two rows above the prompt that say how the day stands and
// where the full board lives. The board itself is the dashboard's Lead view;
// a terminal band of half the screen cannot hold a project list readably.
// No `$` here, so a test mounts it with any board it likes.
import type { RenderSurface } from 'claude-code'

export type Board = {
  watermark: string
  needs_you: { kind: string; project_name: string | null }[]
  counts: { needs_you: number; working: number; sessions: number }
  dashboard_url?: string
}

export const LEAD_URL = 'http://localhost:8787/#lead'
const BAR_BG = '#1e293b'

export function drawBand(el: any, below: unknown, board: Board, hide: (surface: RenderSurface) => void) {
  const { Box, Text, Button, Link } = el
  const c = board.counts
  const url = board.dashboard_url || LEAD_URL
  // Only names and counts on these rows: free text could carry Hebrew, and
  // iTerm2 orders a whole row by its first strong letter.
  const first = board.needs_you.find(n => n.kind === 'session')
  return (
    <Box flexDirection="column">
      {below}
      <Box key="band-top" backgroundColor={BAR_BG} paddingX={1} columnGap={3}>
        <Text color="claude" weight="bold">◆ MissionCache lead</Text>
        <Text color={c.needs_you ? 'warning' : undefined} weight={c.needs_you ? 'bold' : undefined} dimColor={!c.needs_you}>{`● ${c.needs_you} need${c.needs_you === 1 ? 's' : ''} you`}</Text>
        <Text color={c.working ? 'success' : undefined} dimColor={!c.working}>{`● ${c.working} working`}</Text>
        <Text dimColor>{`${c.sessions} session${c.sessions === 1 ? '' : 's'}`}</Text>
        {first?.project_name ? <Text wrap="truncate-end" color="warning">{`▸ ${first.project_name}`}</Text> : null}
      </Box>
      <Box key="band-link" paddingX={1} columnGap={2}>
        <Text dimColor>Projects, live feed and today's agenda:</Text>
        <Box key="lead-link"><Text><Link href={url} label={url} /></Text></Box>
        <Button key="band-hide" plain hotkey="0" onPress={(press: { surface: RenderSurface }) => hide(press.surface)}><Text dimColor>hide</Text></Button>
      </Box>
    </Box>
  )
}
