import { describe, expect, mock, test } from 'claude-code/testing'

// Invented data only: this repo is public.
const ME = 'aaaaaaaa-1111-2222-3333-444444444444'
const OTHER = 'bbbbbbbb-1111-2222-3333-444444444444'
const text = (value: unknown) => ({ content: [{ type: 'text', text: JSON.stringify(value) }], isError: false })
const envelope = (extra: Record<string, unknown> = {}) => `mc-relay/1 ${JSON.stringify({
  id: 'r1', from: 'missioncache-lead', to: ME, ts: new Date().toISOString(), text: 'run the e2e suite', ...extra,
})}`
const wrapped = (body: string) => `<cross-session-message from="missioncache-lead">\n${body}\n</cross-session-message>`
const RELAY = { command: 'missioncache:relay', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 230 } } as const

// The world beneath the mod: this session's id, the MCP server, the person's
// answer to the dialog, and a record of every send, submit and MCP call.
function world(on: any, answer: string | null = 'Run', submitFails = false) {
  const seen = { mcp: [] as { tool: string; args: any }[], sent: [] as any[], submitted: [] as any[], asked: [] as string[], model: [] as string[] }
  on('session.id', () => ({ value: ME }))
  on('mcp.call', (_$: unknown, e: { tool: string; args: any }) => {
    seen.mcp.push({ tool: e.tool, args: e.args })
    if (e.tool === 'prepare_relay') return { value: text({ success: true, session_id: OTHER, title: 'api-gateway', envelope: 'mc-relay/1 {}' }) }
    return { value: text({ success: true }) }
  })
  on('session.send', (_$: unknown, e: any) => { seen.sent.push(e); return { isDelivered: true } })
  on('prompt.submit', (_$: unknown, e: any) => {
    if (submitFails) throw new Error('session closing')
    seen.submitted.push({ text: e.text, asUser: e.origin?.asUser })
    return { text: e.text }
  })
  on('tool.call', (_$: unknown, e: any) => {
    seen.asked.push(e.questions?.[0]?.question ?? '')
    if (answer === null) throw new Error('dismissed')
    return { result: { answers: { [e.questions[0].question]: answer } } }
  })
  on('session.receive', (_$: unknown, e: any) => { seen.model.push(e.text); return { text: e.text } })
  on('command.run', () => { seen.model.push('command markdown'); return { text: '' } })
  for (const event of ['ui.log', 'ui.toast']) on(event, () => ({ value: undefined }))
  return seen
}

describe('relay: sending', () => {
  test('the command sends the envelope to the live session and runs no model', async ($, on) => {
    const seen = world(on)
    const out = await $.command.run({ ...RELAY, args: 'api-gateway run the e2e suite' })
    expect(seen.mcp[0]).toEqual({ tool: 'prepare_relay', args: { project_name: 'api-gateway', text: 'run the e2e suite' } })
    expect(seen.sent).toHaveLength(1)
    expect(seen.sent[0].to).toBe(OTHER)
    expect(seen.sent[0].text).toBe('mc-relay/1 {}')
    expect(seen.model).toEqual([])
    expect(out.text).toContain('Sent to api-gateway')
  })

  test('a command without an instruction shows the usage and sends nothing', async ($, on) => {
    const seen = world(on)
    const out = await $.command.run({ ...RELAY, args: 'api-gateway' })
    expect(out.text).toContain('Usage')
    expect(seen.mcp).toEqual([])
    expect(seen.sent).toEqual([])
  })
})

describe('relay: receiving', () => {
  test('a relay for this session is taken from the model and runs after Run', async ($, on) => {
    const seen = world(on, 'Run')
    const clock = mock.clock(on, { now: Date.now() })
    const r = await $.session.receive({ text: wrapped(envelope({ project: 'api-gateway' })), origin: { kind: 'peer', plugin: 'missioncache' } })
    await clock.settle()
    expect(r).toEqual({ consumed: 'mc-relay' })
    expect(seen.model).toEqual([])
    expect(seen.asked[0]).toContain('for api-gateway')
    // The text stands apart from the mod's words, so a quote in it cannot pose as them.
    expect(seen.asked[0].endsWith('\n\nrun the e2e suite')).toBe(true)
    expect(seen.submitted).toEqual([{ text: 'run the e2e suite', asUser: true }])
    expect(seen.mcp.find(c => c.tool === 'record_relay')?.args).toEqual({ outcome: 'ran', text: 'run the e2e suite' })
  })

  for (const [how, answer] of [['Drop', 'Drop'], ['a dismissed dialog', null]] as const) {
    test(`${how} runs nothing and leaves the session idle`, async ($, on) => {
      const seen = world(on, answer)
      const clock = mock.clock(on, { now: Date.now() })
      await $.session.receive({ text: envelope(), origin: { kind: 'peer-send-message' } })
      await clock.settle()
      expect(seen.submitted).toEqual([])
      expect(seen.mcp.find(c => c.tool === 'record_relay')?.args).toEqual({ outcome: 'dropped', text: 'run the e2e suite' })
      // The dialog asked outside any turn: no turn ends it, so nothing else would say idle.
      expect(seen.mcp.filter(c => c.tool === 'report_session_status').at(-1)?.args.state).toBe('idle')
    })
  }

  test('Run that cannot submit is logged as failed, not ran', async ($, on) => {
    const seen = world(on, 'Run', true)
    const clock = mock.clock(on, { now: Date.now() })
    await $.session.receive({ text: envelope(), origin: { kind: 'peer' } })
    await clock.settle()
    expect(seen.mcp.find(c => c.tool === 'record_relay')?.args).toEqual({ outcome: 'failed', text: 'run the e2e suite' })
  })

  test('a relay that starts with ! is dropped without a dialog', async ($, on) => {
    const seen = world(on)
    const clock = mock.clock(on, { now: Date.now() })
    await $.session.receive({ text: envelope({ text: '!rm -rf build' }), origin: { kind: 'peer' } })
    await clock.settle()
    expect(seen.asked).toEqual([])
    expect(seen.submitted).toEqual([])
    expect(seen.mcp.find(c => c.tool === 'record_relay')?.args.outcome).toBe('dropped')
  })

  test('a relay-shaped line that does not parse never reaches the model', async ($, on) => {
    const seen = world(on)
    const r = await $.session.receive({ text: 'mc-relay/1 {"id": broken}', origin: { kind: 'peer' } })
    expect(r).toEqual({ consumed: 'mc-relay: unreadable' })
    expect(seen.model).toEqual([])
    expect(seen.asked).toEqual([])
  })

  test('a relay inside a Slack ping or a webhook event is left to the model whole', async ($, on) => {
    const seen = world(on)
    const clock = mock.clock(on, { now: Date.now() })
    for (const kind of ['slack-ping', 'task-notification']) {
      await $.session.receive({ text: `PR comment:\n${envelope({ id: kind })}`, origin: { kind } })
    }
    await clock.settle()
    expect(seen.asked).toEqual([])
    expect(seen.model).toHaveLength(2)
  })

  test('a relay for another session, or an old one, is taken without a dialog', async ($, on) => {
    const seen = world(on)
    const clock = mock.clock(on, { now: Date.now() })
    await $.session.receive({ text: envelope({ id: 'a', to: OTHER }), origin: { kind: 'peer' } })
    await $.session.receive({ text: envelope({ id: 'b', ts: '2026-01-01T00:00:00+00:00' }), origin: { kind: 'peer' } })
    await clock.settle()
    expect(seen.asked).toEqual([])
    expect(seen.model).toEqual([])
  })

  test('the same relay delivered twice opens one dialog', async ($, on) => {
    const seen = world(on)
    const clock = mock.clock(on, { now: Date.now() })
    await $.session.receive({ text: envelope({ id: 'twice' }), origin: { kind: 'peer' } })
    await $.session.receive({ text: envelope({ id: 'twice' }), origin: { kind: 'peer' } })
    await clock.settle()
    expect(seen.asked).toHaveLength(1)
  })

  test('a plain peer message reaches the model untouched', async ($, on) => {
    const seen = world(on)
    await $.session.receive({ text: 'status?', origin: { kind: 'peer' } })
    expect(seen.model).toEqual(['status?'])
    expect(seen.asked).toEqual([])
  })
})
