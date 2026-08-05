import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePushToTalk, type VoiceControls } from './usePushToTalk'
import { MAX_HOLD_MS, PTT_STORAGE_KEY, savePttConfig, type PttConfig } from '../lib/pushToTalk'

/** A recording-state-tracking stand-in for useVoiceInput. */
function makeVoice(overrides: Partial<VoiceControls> = {}) {
  const calls: string[] = []
  const v: VoiceControls & { calls: string[] } = {
    calls,
    recording: false,
    start: vi.fn(() => { calls.push('start'); v.recording = true }),
    stop: vi.fn(() => { calls.push('stop'); v.recording = false }),
    prewarm: vi.fn(() => { calls.push('prewarm') }),
    cancel: vi.fn(() => { calls.push('cancel'); v.recording = false }),
    ...overrides,
  }
  return v
}

const MAC_ALT_RIGHT: PttConfig = { mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }

function down(code: string, init: KeyboardEventInit = {}) {
  act(() => {
    document.dispatchEvent(new KeyboardEvent('keydown', { code, bubbles: true, cancelable: true, ...init }))
  })
}
function up(code: string, init: KeyboardEventInit = {}) {
  act(() => {
    document.dispatchEvent(new KeyboardEvent('keyup', { code, bubbles: true, ...init }))
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
})
afterEach(() => {
  vi.useRealTimers()
  localStorage.clear()
})

describe('hybrid mode', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  it('pre-warms on press and starts only after the threshold', () => {
    const voice = makeVoice()
    const { result } = renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    // The pre-warm window: mic acquisition has begun, capture has not.
    expect(voice.calls).toEqual(['prewarm'])
    expect(result.current.phase).toBe('arming')

    act(() => { vi.advanceTimersByTime(500) })
    expect(voice.calls).toEqual(['prewarm', 'start'])
    expect(result.current.holding).toBe(true)
  })

  it('stops on release after a hold', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')
    expect(voice.calls).toEqual(['prewarm', 'start', 'stop'])
  })

  it('latches on a tap released before the threshold', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(200) })
    up('AltRight')
    // Tap = start and STAY recording; no stop.
    expect(voice.calls).toEqual(['prewarm', 'start'])
    expect(voice.recording).toBe(true)
  })

  it('a second tap ends the latched recording', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true }); act(() => { vi.advanceTimersByTime(200) }); up('AltRight')
    expect(voice.recording).toBe(true)
    down('AltRight', { altKey: true })
    expect(voice.calls).toEqual(['prewarm', 'start', 'stop'])
    expect(voice.recording).toBe(false)
  })

  // Auto-repeat fires keydown ~30x/sec while held; only the first may arm.
  it('ignores auto-repeat keydowns', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    for (let i = 0; i < 10; i++) down('AltRight', { altKey: true, repeat: true })
    expect(voice.prewarm).toHaveBeenCalledTimes(1)
    act(() => { vi.advanceTimersByTime(500) })
    expect(voice.start).toHaveBeenCalledTimes(1)
  })
})

describe('push-to-talk mode', () => {
  beforeEach(() => savePttConfig({ ...MAC_ALT_RIGHT, mode: 'ptt' }))

  it('a tap does nothing but release the warm mic', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    up('AltRight')
    expect(voice.calls).toEqual(['prewarm', 'cancel'])
    expect(voice.start).not.toHaveBeenCalled()
  })

  it('a hold records and release stops', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')
    expect(voice.calls).toEqual(['prewarm', 'start', 'stop'])
  })
})

describe('toggle mode', () => {
  beforeEach(() => savePttConfig({ ...MAC_ALT_RIGHT, mode: 'toggle' }))

  it('starts immediately with no arming delay and no pre-warm', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    expect(voice.calls).toEqual(['start'])
    up('AltRight')
    expect(voice.calls).toEqual(['start'])
  })

  it('the next press stops', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true }); up('AltRight')
    down('AltRight', { altKey: true })
    expect(voice.calls).toEqual(['start', 'stop'])
  })
})

describe('binding discrimination', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  it('ignores the other side of the same modifier', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltLeft', { altKey: true })
    expect(voice.calls).toEqual([])
  })

  it('ignores the bound key when another modifier family is also held', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true, ctrlKey: true })
    expect(voice.calls).toEqual([])
  })

  it('leaves a bare modifier keydown un-prevented', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    const ev = new KeyboardEvent('keydown', { code: 'AltRight', altKey: true, bubbles: true, cancelable: true })
    act(() => { document.dispatchEvent(ev) })
    expect(ev.defaultPrevented).toBe(false)
  })

  // A chord's primary key WOULD type a character or scroll the page.
  it('claims a chord binding keydown', () => {
    savePttConfig({ mode: 'hybrid', binding: { code: 'Space', alt: true, shift: true }, holdMs: 500 })
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    const ev = new KeyboardEvent('keydown', { code: 'Space', altKey: true, shiftKey: true, bubbles: true, cancelable: true })
    act(() => { document.dispatchEvent(ev) })
    expect(ev.defaultPrevented).toBe(true)
    expect(voice.calls).toEqual(['prewarm'])
  })

  it('does not fire from inside an embedded terminal', () => {
    const term = document.createElement('div')
    term.className = 'xterm'
    document.body.appendChild(term)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    act(() => {
      term.dispatchEvent(new KeyboardEvent('keydown', { code: 'AltRight', altKey: true, bubbles: true, cancelable: true }))
    })
    expect(voice.calls).toEqual([])
    term.remove()
  })

  it('does nothing at all when disabled', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice, { disabled: true }))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    expect(voice.calls).toEqual([])
  })
})

describe('stuck-mic watchdogs', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  function holdDown(voice: ReturnType<typeof makeVoice>) {
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    expect(voice.calls).toEqual(['prewarm', 'start'])
  }

  it('commits on window blur mid-hold', () => {
    const voice = makeVoice()
    holdDown(voice)
    act(() => { window.dispatchEvent(new Event('blur')) })
    expect(voice.calls).toEqual(['prewarm', 'start', 'stop'])
  })

  it('commits when the document is hidden mid-hold', () => {
    const voice = makeVoice()
    holdDown(voice)
    const spy = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(voice.calls).toEqual(['prewarm', 'start', 'stop'])
    spy.mockRestore()
  })

  it('commits at the hard cap when no keyup ever arrives', () => {
    const voice = makeVoice()
    holdDown(voice)
    act(() => { vi.advanceTimersByTime(MAX_HOLD_MS) })
    expect(voice.calls).toEqual(['prewarm', 'start', 'stop'])
  })

  // A later event's modifier flags report live hardware, so any subsequent
  // keystroke can prove a release we never received.
  it('reconciles from a later keystroke that shows the modifier is up', () => {
    const voice = makeVoice()
    holdDown(voice)
    // altKey false on this event => Option is physically up, so the keyup we
    // never saw did happen.
    down('KeyA')
    expect(voice.calls).toEqual(['prewarm', 'start', 'stop'])
  })

  it('does NOT treat a still-down modifier as a lost release', () => {
    const voice = makeVoice()
    holdDown(voice)
    // The modifier IS still down, so this is not a missed keyup. It is the chord
    // case, which commits (see the chord suite); what must not happen is the
    // watchdog reading it as a lost release and leaving the machine armed.
    down('KeyA', { altKey: true })
    expect(voice.calls).toEqual(['prewarm', 'start', 'stop'])
  })

  it('releases a merely-armed pre-warm on blur without ever starting', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { window.dispatchEvent(new Event('blur')) })
    expect(voice.calls).toEqual(['prewarm', 'cancel'])
    expect(voice.start).not.toHaveBeenCalled()
  })
})

describe('async start race', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  // start() is async — getUserMedia, and a first-ever permission prompt can take
  // seconds. A hold released inside that window resolves into a live session
  // with nobody holding a key, no keyup coming, and the cap timer already
  // cleared: the microphone stays open until the user notices.
  //
  // The fake is deliberately PESSIMISTIC: it lets startup complete and go live
  // even after `cancel()`, which the real streaming/batch paths do not. That
  // keeps the assertion on the hook's defence-in-depth rather than on the fake's
  // cooperation — release-time `cancel()` is the primary abort, and the settle
  // handler is the backstop for a startup that ignores it.
  //
  // `recording` only becomes true when startup RESOLVES, so a stop() issued at
  // release time is a no-op exactly as it is in useVoiceInput (mediaRef is still
  // null). An assertion that merely counts stop() calls therefore cannot detect
  // the bug — release-time disarm satisfies it either way. Assert the END STATE.
  it('stops a session whose start resolved after the key was already released', async () => {
    let resolveStart: () => void = () => {}
    const calls: string[] = []
    const voice: VoiceControls & { calls: string[] } = {
      calls,
      recording: false,
      start: vi.fn(() => new Promise<void>(r => {
        calls.push('start')
        resolveStart = () => { voice.recording = true; calls.push('capture-live'); r() }
      })),
      stop: vi.fn(() => {
        // Mirrors useVoiceInput.stop: nothing to tear down before capture began.
        calls.push(voice.recording ? 'stop-effective' : 'stop-noop')
        voice.recording = false
      }),
      prewarm: vi.fn(() => { calls.push('prewarm') }),
      cancel: vi.fn(() => { calls.push('cancel'); voice.recording = false }),
    }
    renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')
    // cancel(), not stop(): mid-startup there is no recorder for stop() to end.
    expect(calls).toEqual(['prewarm', 'start', 'cancel'])

    await act(async () => { resolveStart(); await Promise.resolve() })

    // The microphone must not be left open by the late resolution.
    expect(voice.recording).toBe(false)
    expect(calls).toEqual(['prewarm', 'start', 'cancel', 'capture-live', 'stop-effective'])
  })

  it('leaves a still-held session running when startup resolves late', async () => {
    let resolveStart: () => void = () => {}
    const voice = makeVoice({ start: vi.fn(() => new Promise<void>(r => { resolveStart = r })) })
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    // Key still down when startup finishes — the session belongs to a live hold.
    await act(async () => { resolveStart(); await Promise.resolve() })
    expect(voice.stop).not.toHaveBeenCalled()
    up('AltRight')
    expect(voice.stop).toHaveBeenCalledTimes(1)
  })

  // The nastier half, and the one a `.then()` handler structurally CANNOT cover:
  // a startup that never settles at all. The streaming path awaits a `ready`
  // frame, so a socket that opens and then goes silent leaves `start()` pending
  // forever — and the hard cap has already been cleared by the release. Cleanup
  // must therefore be synchronous at disarm time, not chained on the promise.
  it('aborts a startup that never settles, without waiting for it', async () => {
    const calls: string[] = []
    const voice: VoiceControls & { calls: string[] } = {
      calls,
      recording: false,
      // Never resolves and never rejects.
      start: vi.fn(() => { calls.push('start'); return new Promise<void>(() => {}) }),
      stop: vi.fn(() => { calls.push('stop') }),
      prewarm: vi.fn(() => { calls.push('prewarm') }),
      cancel: vi.fn(() => { calls.push('cancel') }),
    }
    renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')

    // cancel(), not stop(): stop() is a no-op mid-startup (no recorder, no live
    // socket), while cancel() aborts the startup itself.
    expect(calls).toEqual(['prewarm', 'start', 'cancel'])
    expect(voice.stop).not.toHaveBeenCalled()

    // And nothing is left armed that could resurrect it.
    await act(async () => { vi.advanceTimersByTime(MAX_HOLD_MS * 2); await Promise.resolve() })
    expect(calls).toEqual(['prewarm', 'start', 'cancel'])
  })

  it('aborts a never-settling startup on blur too, not just on release', () => {
    const voice = makeVoice({ start: vi.fn(() => new Promise<void>(() => {})) })
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    act(() => { window.dispatchEvent(new Event('blur')) })
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
  })

  // A REJECTED startup can arrive with resources half-acquired: useStreamingStt
  // builds its AudioContext after getUserMedia and the socket handshake, outside
  // any try, and useVoiceInput's streaming branch re-raises. So the mic stream is
  // open with no session to stop — only cancel() tears it down.
  it('cancels when startup rejects while the key is still held', async () => {
    let rejectStart: (e: Error) => void = () => {}
    const voice = makeVoice({
      start: vi.fn(() => new Promise<void>((_, rej) => { rejectStart = rej })),
    })
    const { result } = renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    expect(result.current.holding).toBe(true)

    await act(async () => { rejectStart(new Error('AudioContext failed')); await Promise.resolve() })

    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
    // And the machine is back to idle rather than stuck in a hold with no session.
    expect(result.current.holding).toBe(false)
  })

  it('does not double-cancel when startup rejects after the key was released', async () => {
    let rejectStart: (e: Error) => void = () => {}
    const voice = makeVoice({
      start: vi.fn(() => new Promise<void>((_, rej) => { rejectStart = rej })),
    })
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')                                  // release cancels the pending startup
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    await act(async () => { rejectStart(new Error('nope')); await Promise.resolve() })
    expect(voice.cancel).toHaveBeenCalledTimes(1)   // not again
  })

  // The settle handler must stop only the session IT opened. Releasing and then
  // immediately latching inside the getUserMedia window used to have the old
  // hold's resolution kill the brand-new session.
  it('does not stop a session a later tap opened', async () => {
    let resolveFirst: () => void = () => {}
    let call = 0
    const voice = makeVoice({
      start: vi.fn(() => {
        call++
        // Only the FIRST start is slow; useVoiceInput's re-entrancy guard makes
        // the second a no-op in reality, so the first is what goes live.
        if (call === 1) return new Promise<void>(r => { resolveFirst = r })
        return undefined
      }),
    })
    renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')                                   // hold over, startup still in flight
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(200) })
    up('AltRight')                                   // a tap → latch on (hybrid)
    const cancelsBefore = voice.cancel.mock.calls.length

    await act(async () => { resolveFirst(); await Promise.resolve() })

    // The tap's session survives the old hold's late resolution.
    expect(voice.stop).not.toHaveBeenCalled()
    expect(voice.cancel.mock.calls.length).toBe(cancelsBefore)
  })
})

// On macOS, Option-chords are how you type ⌥V, ⌥3, ⌥5 and most special
// characters. A bound bare modifier must not turn those into dictation.
describe('modifier chords must not trigger recording', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  it('a quick Option-chord does not latch recording', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })          // bound modifier down
    act(() => { vi.advanceTimersByTime(120) })
    down('KeyV', { altKey: true })              // typing ⌥V
    act(() => { vi.advanceTimersByTime(80) })
    up('KeyV', { altKey: true })
    up('AltRight')                              // released well under 500ms

    // The release must NOT read as a tap. Nothing recorded.
    expect(voice.start).not.toHaveBeenCalled()
    expect(voice.recording).toBe(false)
  })

  it('a chord held past the threshold does not start a hold', () => {
    const voice = makeVoice()
    const { result } = renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    down('KeyV', { altKey: true })
    act(() => { vi.advanceTimersByTime(900) })   // well past 500ms
    expect(voice.start).not.toHaveBeenCalled()
    expect(result.current.holding).toBe(false)
    up('AltRight')
    expect(voice.start).not.toHaveBeenCalled()
  })

  it('a stray keypress mid-dictation commits rather than discarding', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })   // a real hold is running
    expect(voice.start).toHaveBeenCalledTimes(1)
    down('KeyA', { altKey: true })
    // What was already said survives; discarding would lose the utterance.
    expect(voice.calls).toEqual(['prewarm', 'start', 'stop'])
    expect(voice.cancel).not.toHaveBeenCalled()
  })

  it('another modifier joining the press also counts as a chord', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(120) })
    down('ShiftLeft', { altKey: true, shiftKey: true })
    up('AltRight')
    expect(voice.start).not.toHaveBeenCalled()
  })
})

describe('streaming release during startup keeps the buffered speech', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  // useStreamingStt connects its worklet and buffers PCM BEFORE the server's
  // `ready` frame, so a hold released during a slow handshake has real speech in
  // it. cancel() would throw that away; stop() sends the stop frame (the socket
  // is already open) and the upstream 8s force-cleanup keeps the mic ceiling.
  it('commits instead of discarding when streaming', () => {
    const voice = makeVoice({
      streamEnabled: true,
      start: vi.fn(() => new Promise<void>(() => {})),   // handshake never lands
    })
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')
    expect(voice.stop).toHaveBeenCalledTimes(1)
    expect(voice.cancel).not.toHaveBeenCalled()
  })

  it('still discards on the batch path, where nothing was captured', () => {
    const voice = makeVoice({
      streamEnabled: false,
      start: vi.fn(() => new Promise<void>(() => {})),
    })
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
  })
})

describe('live rebinding', () => {
  it('picks up a new binding written by Settings without a remount', () => {
    savePttConfig(MAC_ALT_RIGHT)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))

    act(() => { savePttConfig({ ...MAC_ALT_RIGHT, binding: { code: 'ShiftRight' } }) })
    down('AltRight', { altKey: true })
    expect(voice.calls).toEqual([])
    down('ShiftRight', { shiftKey: true })
    expect(voice.calls).toEqual(['prewarm'])
  })

  it('reacts to a rebind made in another dashboard window', () => {
    savePttConfig(MAC_ALT_RIGHT)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    act(() => {
      localStorage.setItem(PTT_STORAGE_KEY, JSON.stringify({ ...MAC_ALT_RIGHT, binding: { code: 'MetaRight' } }))
      window.dispatchEvent(new Event('storage'))
    })
    down('MetaRight', { metaKey: true })
    expect(voice.calls).toEqual(['prewarm'])
  })
})
