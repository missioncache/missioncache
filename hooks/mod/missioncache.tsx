// The MissionCache mod. In every session it reports what the session is doing
// (report_session_status, once per change, no model call). In the lead session
// it draws a two-row band above the prompt from get_lead_board: the counts
// that matter now and the link to the dashboard's Lead view.
import type { EngineInterface, Register } from 'claude-code'
import { drawBand, type Board } from './board.tsx'
import { clip, decodeMcp, OWN_TOOLS, SERVER, toolTarget } from './parse.ts'

const POLL_MS = 5_000
// A session that dies without a last report changes no stored value, so the
// watermark cannot see it go. One poll a minute rebuilds regardless.
const FULL_EVERY = 12
const SLOW_TOOL_MS = 5_000
// /missioncache:lead's command.run returns before the model has run the skill's
// `lead set`, so the first polls can still say another session leads. The band
// waits this many polls for the role before it gives up.
const CONFIRM_POLLS = 24

// --- the reporter ---------------------------------------------------------

let sent = { state: '', reason: '' }
let turns = { day: '', count: 0 }
let compactedThisTurn = false
// True while the session waits on the person (a permission prompt or a
// question): nothing but the tool call resuming may report working then.
let waitingOnPerson = false
const failures = new Set<string>()

function logOnce($: EngineInterface, what: string, err: unknown) {
  const reason = `${what}: ${String(err)}`
  if (failures.has(reason)) return
  failures.add(reason)
  $.ui.log(`[missioncache] ${reason}`)
}

type Report = {
  state: 'working' | 'idle' | 'needs_you' | 'compacting'
  needs_reason?: string
  prompt?: string
  now?: string
  context_pct?: number
  cost_usd?: number
  turns_today?: number
  last_answer?: string
  last_turn_ms?: number
  compacted?: boolean
}

// Writes only when the state or its reason changed, unless forced: a turn's
// start and end, and a slow tool, always carry new fields.
function report($: EngineInterface, r: Report, force = false) {
  const reason = r.needs_reason ?? ''
  if (!force && r.state === sent.state && reason === sent.reason) return
  sent = { state: r.state, reason }
  void $.mcp.call(SERVER, 'report_session_status', r).then(
    res => { if (res.isError) logOnce($, 'report_session_status', decodeMcp<{ message?: string }>(res).message) },
    err => logOnce($, 'report_session_status', err),
  )
}

function countTurn(nowMs: number): number {
  const day = new Date(nowMs).toDateString()
  if (turns.day !== day) turns = { day, count: 0 }
  return ++turns.count
}

// --- the lead board -------------------------------------------------------

const lead = {
  active: false,
  confirmed: false,
  hidden: false,
  board: null as Board | null,
  // The feed itself is the Lead view's; the cursor only keeps each answer small.
  cursor: null as number | null,
  polls: 0,
  timer: null as { cancel: () => void } | null,
}

async function poll($: EngineInterface, full = false) {
  const args: Record<string, unknown> = {}
  if (lead.cursor !== null) args.after_id = lead.cursor
  if (++lead.polls % FULL_EVERY !== 0 && !full && lead.board) args.watermark = lead.board.watermark
  try {
    const res = decodeMcp<Board & { changed: boolean; last_event_id: number; lead: { is_me: boolean } }>(
      await $.mcp.call(SERVER, 'get_lead_board', args),
    )
    if (!res.changed) return
    if (!res.lead.is_me) {
      if (lead.confirmed) {
        stop($)
        $.ui.toast('MissionCache: another session is the lead now, the band is closed')
      } else if (lead.polls >= CONFIRM_POLLS) {
        stop($)
      }
      return
    }
    lead.confirmed = true
    lead.cursor = Math.max(lead.cursor ?? 0, res.last_event_id ?? 0)
    lead.board = res
    lead.hidden = false
    $.ui.invalidate('ui.render')
  } catch (err) {
    logOnce($, 'get_lead_board', err)
  }
}

function start($: EngineInterface) {
  if (lead.active) return
  lead.active = true
  void poll($, true)
  lead.timer = $.clock.every(POLL_MS, () => { void poll($) })
}

function stop($: EngineInterface) {
  lead.timer?.cancel()
  Object.assign(lead, { active: false, confirmed: false, board: null, cursor: null, polls: 0, timer: null })
  $.ui.invalidate('ui.render')
}

// --- hooks ----------------------------------------------------------------

// A hook that gates an event carries a .catch that lets the event go on: a
// failed report or board call must never hold up a tool, a command or a prompt.

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const r = await next(e)
    // After a resume the MCP server may not be up yet, so try a few times.
    void (async () => {
      for (let attempt = 0; attempt < 5; attempt++) {
        try {
          const res = decodeMcp<{ lead?: { is_me: boolean } }>(await $.mcp.call(SERVER, 'get_lead_board', { watermark: '' }))
          if (res.lead?.is_me) start($)
          return
        } catch {
          await $.clock.sleep(2_000)
        }
      }
    })()
    return r
  })

  on('command.run', { command: 'missioncache:lead' }, async ($, e, next) => {
    const r = await next(e)
    start($)
    return r
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'missioncache:unlead' }, async ($, e, next) => {
    const r = await next(e)
    stop($)
    return r
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const below = await next(e)
    if (!lead.active || lead.hidden || !lead.board || e.props.hasSurvey) return below
    try {
      // 0 hides the band until the next poll that finds a change.
      return drawBand($.ui.resolve(e), below, lead.board, () => {
        lead.hidden = true
        $.ui.invalidate('ui.render')
      })
    } catch (err) {
      logOnce($, 'board render', err)
      return below
    }
  })

  on('turn.start', async ($, e, next) => {
    if (!e.agentId) {
      compactedThisTurn = false
      report($, { state: 'working', prompt: clip(e.text, 80) }, true)
    }
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    if (e.agentId || OWN_TOOLS.has(e.tool)) return next(e)
    if (e.tool === 'AskUserQuestion') {
      waitingOnPerson = true
      report($, { state: 'needs_you', needs_reason: 'question' })
    }
    const slow = $.clock.after(SLOW_TOOL_MS, () => {
      if (waitingOnPerson) return
      report($, { state: 'working', now: toolTarget(e as unknown as Record<string, unknown>) }, true)
    })
    try {
      return await next(e)
    } finally {
      slow.cancel()
      // The call resumed, so the person answered: clear the needs_you.
      waitingOnPerson = false
      report($, { state: 'working' })
    }
  }).catch(($, e, next) => next(e))

  on('classic.PermissionRequest', async ($, e, next) => {
    waitingOnPerson = true
    report($, { state: 'needs_you', needs_reason: `permission: ${e.tool_name}` })
    return next(e)
  }).catch(($, e, next) => next(e))

  on('session.compact', async ($, e, next) => {
    if (!e.agentId) {
      compactedThisTurn = true
      report($, { state: 'compacting' })
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    if (e.agentId) return r
    try {
      const u = await $.session.usage()
      report($, {
        state: 'idle',
        context_pct: u.context.percent === undefined ? undefined : Math.round(u.context.percent),
        cost_usd: u.cost?.usd,
        turns_today: countTurn(Date.now()),
        last_answer: e.reason === 'answer' ? clip(e.answer, 60) : '',
        last_turn_ms: e.durationMs,
        compacted: compactedThisTurn,
      }, true)
    } catch (err) {
      logOnce($, 'turn.complete', err)
    }
    return r
  })
}
