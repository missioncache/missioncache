// The MissionCache mod. In every session it reports what the session is doing
// (report_session_status, once per change, no model call). In the lead session
// it draws a two-row band above the prompt from get_lead_board: the counts
// that matter now and the link to the dashboard's Lead view. And it carries
// relays: /missioncache:relay sends one instruction from the lead, and the
// receiving session runs it only after the person there picks Run.
import type { EngineInterface, Register } from 'claude-code'
import { drawBand, type Board } from './board.tsx'
import { approveReason, clip, decodeMcp, looksLikeRelay, OWN_TOOLS, parseRelay, plain, type Relay, RELAY_TEXT_MAX, relayArgs, SERVER, toolTarget } from './parse.ts'

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
// A relay's dialog is a question asked outside any turn: answering it must
// leave the session idle, not working.
let inTurn = false
const failures = new Set<string>()

function logOnce($: EngineInterface, what: string, err: unknown) {
  const reason = `${what}: ${String(err)}`
  if (failures.has(reason)) return
  failures.add(reason)
  $.ui.log(reason)
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

// --- relay ----------------------------------------------------------------

// prepare_relay stamps the envelope; an older one is a replay, not a request.
const RELAY_MAX_AGE_MS = 10 * 60_000
// A NaN from an unreadable time compares false, so it is never fresh.
const fresh = (ts: string) => Math.abs(Date.now() - Date.parse(ts)) < RELAY_MAX_AGE_MS
// A redelivered relay must not open a second dialog.
const seenRelays = new Set<string>()
// Only another session's message carries a relay. A Slack ping or a webhook
// event holds outsiders' text, and reaches the model whole.
const RELAY_ORIGINS = new Set(['peer', 'peer-send-message'])
const relayShaped = (e: { agentId?: string; origin?: { kind: string }; text: string }) =>
  !e.agentId && RELAY_ORIGINS.has(e.origin?.kind ?? '') && looksLikeRelay(e.text)

async function sendRelay($: EngineInterface, args: string): Promise<string> {
  const parsed = relayArgs(args)
  if (!parsed) return 'Usage: /missioncache:relay <project> <instruction>'
  const res = decodeMcp<{ success?: boolean; reason?: string; message?: string; session_id?: string; title?: string | null; envelope?: string }>(
    await $.mcp.call(SERVER, 'prepare_relay', parsed),
  )
  if (!res.success || !res.session_id || !res.envelope) return `Not sent: ${res.reason ?? res.message ?? 'no answer'}`
  const sent = await $.session.send({ to: { sessionId: res.session_id }, text: res.envelope })
  if (!sent.isDelivered) return `Not sent: ${sent.reason}`
  return `Sent to ${res.title ?? parsed.project_name}. It runs there once the person picks Run.`
}

// Not deduplicated like logOnce: each relay is one action the person took.
async function recordRelay($: EngineInterface, outcome: 'ran' | 'dropped' | 'failed', text: string) {
  try {
    const res = decodeMcp<{ success?: boolean; reason?: string; message?: string }>(
      await $.mcp.call(SERVER, 'record_relay', { outcome, text }),
    )
    if (!res.success) $.ui.log(`relay ${outcome}, not logged: ${res.reason ?? res.message ?? 'no answer'}`)
  } catch (err) {
    $.ui.log(`relay ${outcome}, not logged: ${String(err)}`)
  }
}

// Nothing a peer sends runs by itself: the person in this session sees the
// text and picks. Any session can forge an envelope, so this dialog is the
// only gate, and the text run is the cleaned text the person read. It sits on
// a line of its own, apart from the mod's words, so a quote in it cannot pose
// as the mod.
async function confirmRelay($: EngineInterface, relay: Relay) {
  const text = clip(relay.text, RELAY_TEXT_MAX)
  // A leading ! runs a shell command outside the permission prompts.
  if (text.startsWith('!')) {
    $.ui.log('relay dropped: it starts with !, which would run a shell command')
    return recordRelay($, 'dropped', text)
  }
  const where = relay.project ? ` for ${clip(relay.project, 60)}` : ''
  let choice = ''
  try {
    choice = await $.ui.ask(`Run this relay${where}? It says it is from "${clip(relay.from, 40)}", which nothing can verify. It runs as if you typed:\n\n${text}`, { options: ['Run', 'Drop'], header: 'Relay' })
  } catch (err) {
    // Dismissed, or no one to ask: the same as Drop.
    $.ui.log(`relay dialog closed: ${String(err)}`, { to: 'debug' })
  }
  if (choice !== 'Run') return recordRelay($, 'dropped', text)
  // An answer long after it arrived is no longer the request that was sent.
  if (!fresh(relay.ts)) {
    $.ui.log('relay dropped: it expired while the dialog was open')
    return recordRelay($, 'dropped', text)
  }
  try {
    await $.prompt.submit({ text, asUser: true })
  } catch (err) {
    $.ui.toast(`MissionCache: the relay could not run: ${String(err)}`)
    return recordRelay($, 'failed', text)
  }
  return recordRelay($, 'ran', text)
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

  // Answered here without `next`, so no model turn runs. A failure falls
  // through to the command's markdown, which has the model send it instead.
  on('command.run', { command: 'missioncache:relay' }, async ($, e) => ({ text: await sendRelay($, e.args) }))
    .catch(($, e, next) => {
      $.ui.log(`relay command failed, the model sends it instead: ${String(next.error)}`)
      return next(e)
    })

  // A relay never reaches the model: the mod's send arrives as a peer, a
  // model's SendMessage as a peer-send-message, and either is taken here.
  on('session.receive', async ($, e, next) => {
    if (!relayShaped(e)) return next(e)
    const relay = parseRelay(e.text)
    if (!relay) {
      $.ui.log('relay dropped: it could not be read')
      return { consumed: 'mc-relay: unreadable' }
    }
    if (seenRelays.has(relay.id)) return { consumed: 'mc-relay: already shown' }
    const mine = relay.to === await $.session.id()
    if (!mine || !fresh(relay.ts)) {
      $.ui.log(`relay ignored: ${mine ? 'too old' : 'meant for another session'}`)
      return { consumed: `mc-relay: ${mine ? 'too old' : 'not for this session'}` }
    }
    seenRelays.add(relay.id)
    void confirmRelay($, relay).catch(err => $.ui.log(`relay failed: ${String(err)}`))
    return { consumed: 'mc-relay' }
  }).catch(($, e, next) => {
    // Failing here must not hand the relay to the model either.
    if (!relayShaped(e)) return next(e)
    $.ui.log(`relay dropped: the mod failed reading it: ${String(next.error)}`)
    return { consumed: 'mc-relay: failed' }
  })

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
      inTurn = true
      report($, { state: 'working', prompt: clip(plain(e.text), 80) }, true)
    }
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    if (e.agentId || OWN_TOOLS.has(e.tool)) return next(e)
    if (e.tool === 'AskUserQuestion') {
      waitingOnPerson = true
      report($, { state: 'needs_you', needs_reason: 'answer a question' })
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
      report($, { state: inTurn ? 'working' : 'idle' })
    }
  }).catch(($, e, next) => next(e))

  on('classic.PermissionRequest', async ($, e, next) => {
    waitingOnPerson = true
    report($, { state: 'needs_you', needs_reason: approveReason(e.tool_name) })
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
    inTurn = false
    try {
      const u = await $.session.usage()
      report($, {
        state: 'idle',
        context_pct: u.context.percent === undefined ? undefined : Math.round(u.context.percent),
        cost_usd: u.cost?.usd,
        turns_today: countTurn(Date.now()),
        last_answer: e.reason === 'answer' ? clip(plain(e.answer), 60) : '',
        last_turn_ms: e.durationMs,
        compacted: compactedThisTurn,
      }, true)
    } catch (err) {
      logOnce($, 'turn.complete', err)
    }
    return r
  })
}
