import { describe, expect, mock, test } from 'claude-code/testing'

// Invented data only: this repo is public.
const SID = 'aaaaaaaa-1111-2222-3333-444444444444'
const session = (state: string, extra: Record<string, unknown> = {}) => ({
  session_id: SID, title: 'api-gateway', state, state_since: '2026-10-18 14:29:00', needs_reason: null,
  prompt: 'add retries to the upstream client', now: null, context_pct: 71, cost_usd: 4.12, turns_today: 23,
  last_turn_at: null, last_answer: 'The retry path is wired.', last_turn_ms: 130_000, compacted_at: null,
  updated_at: null, ...extra,
})
const project = (name: string, extra: Record<string, unknown> = {}) => ({
  name, completed_count: 12, total_count: 20, completion_pct: 60, next_up: 'run the e2e suite', due_date: null,
  days_to_due: null, at_risk: false, overdue_count: 0, open_count: 0, active_task: { number: 14, title: 'wire the retry path' },
  left_off: 'retry path wired', last_event: { kind: 'task_done', what: 'task 12 done', created_at: '2026-10-18 14:28:00' },
  last_save: '2026-10-18 14:02', waiting: { count: 0, oldest: null }, session: null, session_count: 0, ...extra,
})
const board = (projects: unknown[], extra: Record<string, unknown> = {}) => ({
  success: true, changed: true, watermark: 'w1', generated_at: '2026-10-18T14:32:00', projects,
  needs_you: [], events: [], has_more: false, last_event_id: 7, live_count: 1,
  lead: { session_id: SID, is_me: true }, focus_available: true, status_error: null,
  counts: { needs_you: 0, working: 0, sessions: 1, changes: 0 }, ...extra,
})
const text = (value: unknown) => ({ content: [{ type: 'text', text: JSON.stringify(value) }], isError: false })
const PROPS = { hasSurvey: false, isWorking: false, maxRows: 12, bodyColumns: 225 }
const LEAD = { command: 'missioncache:lead', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 230 } } as const

// What the engine would answer beneath the mod: an empty band, and quiet
// logs and toasts.
function bottom(on: any) {
  on('ui.render', ($: any, e: any) => { const { Box } = $.ui.resolve(e); return <Box /> })
  for (const event of ['ui.log', 'ui.toast']) on(event, () => ({ value: undefined }))
}

// Loads the mod as the lead with `answer` behind get_lead_board (a list is
// answered one poll at a time, its last item from then on), and records every
// MCP call it makes.
async function asLead($: any, on: any, answer: unknown) {
  const answers = Array.isArray(answer) ? [...answer] : [answer]
  const calls: { tool: string; args: Record<string, unknown> }[] = []
  on('mcp.call', (_$: unknown, e: { tool: string; args: Record<string, unknown> }) => {
    calls.push({ tool: e.tool, args: e.args })
    if (e.tool !== 'get_lead_board') return { value: text({ success: true }) }
    return { value: text(answers.length > 1 ? answers.shift() : answers[0]) }
  })
  on('command.run', () => ({ text: '' }))
  bottom(on)
  const clock = mock.clock(on)
  await $.command.run(LEAD)
  await clock.settle()
  return { calls, clock }
}
const mountBand = ($: any) => $.ui.mount({ plugin: 'missioncache', surface: 'terminal', component: 'AbovePrompt', props: PROPS })

describe('lead band', () => {
  test('draws the counts, the project that needs you and the Lead view link', async ($, on) => {
    await asLead($, on, board([
      project('api-gateway', { session: session('needs_you', { needs_reason: 'permission: Bash' }) }),
      project('docs-site', { session: session('working') }),
    ], {
      needs_you: [{ kind: 'session', project_name: 'api-gateway', what: 'permission: Bash' }],
      counts: { needs_you: 1, working: 1, sessions: 2, changes: 3 },
    }))
    const ui = await $.ui.mount({ plugin: 'missioncache', surface: 'terminal', component: 'AbovePrompt', props: PROPS })
    expect(await ui.find({ type: 'Text', text: '1 needs you' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '1 working' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'api-gateway' })).toBeDefined()
    expect(await ui.find({ key: 'lead-link' })).toBeDefined()
    await ui.unmount()
  })

  test('draws nothing for a session that is not the lead', async ($, on) => {
    bottom(on)
    const ui = await $.ui.mount({ plugin: 'missioncache', surface: 'terminal', component: 'AbovePrompt', props: PROPS })
    expect(await ui.find({ key: 'lead-link' })).toBeUndefined()
    await ui.unmount()
  })

  test('sends the watermark on the next poll and forces a rebuild once a minute', async ($, on) => {
    const { calls, clock } = await asLead($, on, board([project('docs-site')]))
    await clock.advance(5_000)
    const boards = calls.filter(c => c.tool === 'get_lead_board')
    expect(boards[0]!.args.watermark).toBeUndefined()
    expect(boards[1]!.args.watermark).toBe('w1')
    expect(boards[1]!.args.after_id).toBe(7)
    await clock.advance(10 * 5_000)
    const twelfth = calls.filter(c => c.tool === 'get_lead_board')[11]!
    expect(twelfth.args.watermark).toBeUndefined()
  })

  test('waits for the lead role the skill sets after the command returns', async ($, on) => {
    const { clock } = await asLead($, on, [
      board([project('docs-site')], { lead: { session_id: 'other', is_me: false } }),
      board([project('docs-site')]),
    ])
    let ui = await mountBand($)
    expect(await ui.find({ key: 'lead-link' })).toBeUndefined()
    await ui.unmount()
    await clock.advance(5_000)
    ui = await mountBand($)
    expect(await ui.find({ key: 'lead-link' })).toBeDefined()
    await ui.unmount()
  })

  test('closes when another session takes the lead', async ($, on) => {
    const { clock } = await asLead($, on, [
      board([project('docs-site')]),
      board([project('docs-site')], { watermark: 'w2', lead: { session_id: 'other', is_me: false } }),
    ])
    await clock.advance(5_000)
    const ui = await mountBand($)
    expect(await ui.find({ key: 'lead-link' })).toBeUndefined()
    await ui.unmount()
  })

  test('0 hides the band until a poll finds a change', async ($, on) => {
    const { clock } = await asLead($, on, [
      board([project('docs-site')]),
      { changed: false, watermark: 'w1' },
      board([project('docs-site')], { watermark: 'w2' }),
    ])
    let ui = await mountBand($)
    await $.ui.press({ plugin: 'missioncache', key: 'band-hide' })
    expect(await ui.find({ key: 'lead-link' })).toBeUndefined()
    await ui.unmount()
    await clock.advance(5_000)
    ui = await mountBand($)
    expect(await ui.find({ key: 'lead-link' })).toBeUndefined()
    await ui.unmount()
    await clock.advance(5_000)
    ui = await mountBand($)
    expect(await ui.find({ key: 'lead-link' })).toBeDefined()
    await ui.unmount()
  })
})

describe('reporter', () => {
  // Records each report_session_status the mod writes.
  function reports(on: any) {
    const written: Record<string, unknown>[] = []
    on('mcp.call', (_$: unknown, e: { tool: string; args: Record<string, unknown> }) => {
      if (e.tool === 'report_session_status') written.push(e.args)
      return { value: text({ success: true }) }
    })
    on('session.usage', () => ({ value: { startedAt: 0, context: { window: 200_000, percent: 41 }, rateLimits: [], cost: { usd: 1.5 } } }))
    on('turn.start', (_$: unknown, e: { turnId: string }) => ({ turnId: e.turnId }))
    on('turn.complete', () => ({ text: '' }))
    on('classic.PermissionRequest', () => ({}))
    on('tool.call', () => ({ result: { text: 'ok' } }))
    mock.clock(on)
    bottom(on)
    return written
  }
  const TURN = { turnId: 't1' }
  const DONE = { answer: 'Done, the tests pass.', durationMs: 4_000, isAborted: false, turnId: 't1', reason: 'answer' }

  test('a turn with a permission prompt writes working, needs_you, working, idle', async ($, on) => {
    const written = reports(on)
    await $.turn.start({ text: 'run the suite', ...TURN })
    await $.classic.PermissionRequest({ tool_name: 'Bash', tool_input: {} })
    await $.tool.call({ tool: 'Bash', command: 'pytest -q' })
    await $.turn.complete(DONE)
    expect(written.map(w => w.state)).toEqual(['working', 'needs_you', 'working', 'idle'])
    expect(written[1]!.needs_reason).toBe('permission: Bash')
    expect(written[3]).toEqual(expect.objectContaining({ context_pct: 41, cost_usd: 1.5, turns_today: 1, last_answer: 'Done, the tests pass.', last_turn_ms: 4_000 }))
  })

  test('the lead\'s own polling never marks the idle session working', async ($, on) => {
    const written = reports(on)
    await $.turn.start({ text: 'look', ...TURN })
    await $.turn.complete(DONE)
    await $.tool.call({ tool: 'mcp__plugin_missioncache_pm__get_lead_board' })
    expect(written.map(w => w.state)).toEqual(['working', 'idle'])
  })

  test('a question waiting longer than the slow-tool timer stays needs you', async ($, on) => {
    const written: Record<string, unknown>[] = []
    on('mcp.call', (_$: unknown, e: { tool: string; args: Record<string, unknown> }) => {
      if (e.tool === 'report_session_status') written.push(e.args)
      return { value: text({ success: true }) }
    })
    on('turn.start', (_$: unknown, e: { turnId: string }) => ({ turnId: e.turnId }))
    const clock = mock.clock(on)
    // The person answers 20 seconds after the question appears.
    on('tool.call', async () => { await clock.sleep(20_000); return { result: { text: 'yes' } } })
    bottom(on)
    await $.turn.start({ text: 'ask me', ...TURN })
    const call = $.tool.call({ tool: 'AskUserQuestion', questions: [] })
    await clock.advance(10_000)
    expect(written.map(w => w.state)).toEqual(['working', 'needs_you'])
    await clock.advance(10_000)
    await call
    expect(written.map(w => w.state)).toEqual(['working', 'needs_you', 'working'])
  })

  test('a subagent\'s turn is not the session\'s', async ($, on) => {
    const written = reports(on)
    await $.turn.start({ text: 'sub', ...TURN, agentId: 'a1' })
    await $.turn.complete({ ...DONE, agentId: 'a1' })
    expect(written).toEqual([])
  })
})
