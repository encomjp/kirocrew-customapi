/**
 * Push-to-talk / tap-to-toggle keyboard driver for voice input.
 *
 * Owns ONLY the key state machine; capture itself belongs to `useVoiceInput`,
 * which is injected as {@link VoiceControls} so this hook is testable without a
 * microphone.
 *
 * ```
 *  IDLE ──keydown(match)──▶ ARMING ──holdMs elapses──▶ HOLDING
 *    ▲                        │                          │
 *    │                     keyup (tap)                 keyup / watchdog
 *    └────────────────────────┴──────────────────────────┘
 * ```
 *
 * Two things make the ARMING state load-bearing rather than a nuisance delay:
 *
 * 1. **It disambiguates a tap from a hold** — the whole point of hybrid mode.
 * 2. **It is the mic pre-warm window.** `getUserMedia` plus the first audio frame
 *    costs 50-200ms on macOS, and a single-key press has no earlier moment to
 *    hide that in, so the opening word gets clipped (Whisper then hallucinates
 *    the silence into a canned phrase). Arming calls `prewarm()` immediately and
 *    `start()` only once the threshold passes, by which point the stream is live.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import {
  loadPttConfig,
  matchesBinding,
  MAX_HOLD_MS,
  PTT_CHANGED_EVENT,
  type PttConfig,
  isBareModifier,
  stillHeld,
} from '../lib/pushToTalk'

/** The slice of `useVoiceInput` this hook drives. */
export interface VoiceControls {
  recording: boolean
  start: () => Promise<void> | void
  stop: () => void
  /** Acquire the mic ahead of `start()` so capture begins instantly. */
  prewarm: () => void
  /** End capture WITHOUT transcribing; also releases a merely pre-warmed mic. */
  cancel: () => void
  /**
   * Whether capture runs through the STREAMING path.
   *
   * Decides what a release DURING startup means, because the two paths differ in
   * whether audio has been captured by then. Batch has no recorder until
   * `MediaRecorder.start()`, so nothing was said into it and discarding is free.
   * Streaming connects its worklet and BUFFERS PCM before the server's `ready`
   * frame arrives (`useStreamingStt` keeps a bounded buffer and flushes it on
   * ready), so a hold released during a slow handshake has real speech in it —
   * discarding that is data loss.
   */
  streamEnabled?: boolean
}

type Phase = 'idle' | 'arming' | 'holding'

export interface UsePushToTalkOpts {
  /** Disable entirely (e.g. STT off, or a modal owns the keyboard). */
  disabled?: boolean
}

/**
 * True when the keystroke came from inside an embedded terminal, where the key
 * belongs to the PTY. Mirrors `useKeyboardShortcuts.isTerminalTarget`.
 */
function isTerminalTarget(target: EventTarget | null): boolean {
  const el = target as Element | null
  return !!el && typeof el.closest === 'function' && !!el.closest('.xterm')
}

export function usePushToTalk(voice: VoiceControls, { disabled }: UsePushToTalkOpts = {}) {
  const [cfg, setCfg] = useState<PttConfig>(() => loadPttConfig())
  // Mirrored so the keydown handler reads the CURRENT phase without being
  // re-created (and re-bound) on every transition.
  const phaseRef = useRef<Phase>('idle')
  const [phase, setPhaseState] = useState<Phase>('idle')
  const setPhase = useCallback((p: Phase) => { phaseRef.current = p; setPhaseState(p) }, [])

  const armTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const capTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  /**
   * True while `start()`'s async startup is in flight — from just before the
   * call until its promise settles.
   *
   * Load-bearing for the stuck-mic guarantee, because a startup can fail to
   * settle AT ALL: the streaming path awaits a `ready` frame from the backend,
   * and a socket that opens and then goes silent leaves that await pending
   * forever. Any cleanup that runs only in the promise's own `.then()` therefore
   * inherits its liveness, and the hard cap — the one mechanism that does not —
   * has already been cleared by the time the key comes up.
   *
   * So a disarm during this window does not wait for anything: it calls
   * `cancel()` synchronously. `stop()` would be a no-op there (no recorder, no
   * live socket yet), whereas `cancel()` aborts the startup itself — it trips the
   * streaming session's `cancelled` flag and closes the socket, whose `onclose`
   * settles the pending `ready` await, and it releases the batch path's warm mic
   * so `acquireWarm` rejects instead of handing back a stream. Nothing was
   * captured yet, so discarding rather than committing loses no audio.
   */
  const startPendingRef = useRef(false)
  /**
   * Bumped on every arm AND by `disarm`, so a timer armed by an earlier hold
   * cannot fire against a later one. Deliberately NOT the guard for the async
   * `start()` resolution -- `disarm` bumping it is exactly what made that guard
   * dead; see `beginHold`.
   */
  const genRef = useRef(0)
  // Live refs for the voice controls: the document-level listeners are bound
  // once, so reading through refs avoids re-binding them whenever the parent
  // re-renders and hands over new callback identities.
  const voiceRef = useRef(voice)
  voiceRef.current = voice
  const cfgRef = useRef(cfg)
  cfgRef.current = cfg
  const disabledRef = useRef(disabled)
  disabledRef.current = disabled

  useEffect(() => {
    const onChange = () => setCfg(loadPttConfig())
    window.addEventListener(PTT_CHANGED_EVENT, onChange)
    // 'storage' fires for OTHER tabs/windows, so a rebind in Settings reaches a
    // second dashboard window too.
    window.addEventListener('storage', onChange)
    return () => {
      window.removeEventListener(PTT_CHANGED_EVENT, onChange)
      window.removeEventListener('storage', onChange)
    }
  }, [])

  const clearTimers = useCallback(() => {
    if (armTimerRef.current) { clearTimeout(armTimerRef.current); armTimerRef.current = null }
    if (capTimerRef.current) { clearTimeout(capTimerRef.current); capTimerRef.current = null }
  }, [])

  /** Leave any armed/holding state, committing (`stop`) or discarding as told. */
  const disarm = useCallback((commit: boolean) => {
    const was = phaseRef.current
    clearTimers()
    genRef.current++
    setPhase('idle')
    if (was === 'holding') {
      // Startup still in flight. What that means depends on the path:
      //
      //   - STREAMING has already connected its worklet and is buffering PCM
      //     while it waits for the server's `ready` frame, so the user's speech
      //     is really in there. Commit it — `streamStop()` is not a no-op here
      //     (the socket is open, so it sends the stop frame) and it arms its own
      //     8s force-cleanup, so the stuck-mic ceiling still holds.
      //   - BATCH has no recorder yet, so nothing was captured; `stop()` would
      //     do nothing at all, and only `cancel()` actually aborts the startup.
      if (startPendingRef.current) {
        if (voiceRef.current.streamEnabled) voiceRef.current.stop()
        else voiceRef.current.cancel()
      } else if (commit) voiceRef.current.stop()
      else voiceRef.current.cancel()
    } else if (was === 'arming') {
      // Never started capturing — release the pre-warmed mic rather than leaving
      // it live for the remainder of the prewarm idle window.
      voiceRef.current.cancel()
    }
  }, [clearTimers, setPhase])

  /**
   * Monotonic per-`start()` sequence, bumped at EVERY call site.
   *
   * Lets a late-resolving startup tell "the session I opened" from "a session
   * someone else opened after me". Without it the settle handler's phase test is
   * an unconditional "not mine" and stops whatever is live — so a user who
   * releases and then immediately taps to latch (or clicks the mic button)
   * inside the `getUserMedia` window gets their new session killed by the old
   * hold's resolution. `useVoiceInput`'s own re-entrancy guard swallows that
   * second `start()`, so the FIRST promise is the one that actually goes live,
   * and leaving it running is what the user asked for.
   */
  const startSeqRef = useRef(0)

  /**
   * Startup SUCCEEDED. If the hold is over by now the session has no holder —
   * the key is already up, no keyup is coming, and the cap timer was cleared —
   * so stop it.
   *
   * The test is the PHASE, not the generation: `disarm` bumps `genRef`, so a
   * generation comparison here is always false by the time a released hold
   * resolves; it reads like a guard and is dead code. And it is scoped to `seq`
   * so it only ever stops the session this call opened.
   */
  const settleStart = useCallback((seq: number) => {
    startPendingRef.current = false
    if (startSeqRef.current !== seq) return
    if (phaseRef.current !== 'holding') voiceRef.current.stop()
  }, [])

  /**
   * Startup FAILED. Nothing to commit, and a rejection can arrive with resources
   * already half-acquired: `useStreamingStt` builds its `AudioContext` and worklet
   * AFTER `getUserMedia` and the socket handshake, outside any `try`, and
   * `useVoiceInput`'s streaming branch re-raises rather than catching. So a throw
   * there leaves the mic stream open with no session to stop — `cancel()` is what
   * tears it down.
   *
   * Only acts while still holding: if the key is already up, the release path
   * cancelled the pending startup itself.
   */
  const failStart = useCallback(() => {
    startPendingRef.current = false
    if (phaseRef.current === 'holding') disarm(false)
  }, [disarm])

  const beginHold = useCallback(() => {
    const gen = ++genRef.current
    setPhase('holding')
    // Hard ceiling: a release we never hear about must not hold the mic forever.
    capTimerRef.current = setTimeout(() => {
      if (genRef.current === gen && phaseRef.current === 'holding') disarm(true)
    }, MAX_HOLD_MS)
    startPendingRef.current = true
    const seq = ++startSeqRef.current
    const started = voiceRef.current.start()
    if (started && typeof (started as Promise<void>).then === 'function') {
      void (started as Promise<void>).then(
        () => { settleStart(seq) },
        () => { failStart() },
      )
    } else {
      startPendingRef.current = false
    }
  }, [disarm, setPhase, settleStart, failStart])

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (disabledRef.current) return
      // Auto-repeat: a held key fires keydown ~30x/sec. Only the first is an arm.
      if (e.repeat) return
      const { binding, mode, holdMs } = cfgRef.current

      // Any non-matching keystroke is also our chance to reconcile.
      if (!matchesBinding(e, binding)) {
        const phase = phaseRef.current
        if (phase === 'idle') return
        // The bound modifier is no longer physically down, so we missed its
        // keyup — commit what was said.
        if (!stillHeld(e, binding)) { disarm(true); return }
        // It IS still down and another key joined it: the user is typing a CHORD
        // with the bound modifier, not dictating. On macOS that is how you type
        // half the special characters (⌥V, ⌥3, ⌥5), so without this a quick ⌥V
        // read as a tap and LATCHED recording on, and a slower one started a
        // hold. Discard while arming — nothing was captured, and this is also
        // what stops the release from counting as a tap. Commit while holding:
        // a real utterance survives an accidental keypress, and a chord held
        // barely past the threshold yields a blob too short to transcribe.
        disarm(phase === 'holding')
        return
      }
      if (isTerminalTarget(e.target)) return
      // A chord binding's primary key would type a character (Space) or scroll;
      // claim it. A bare modifier produces nothing, so leave it alone — calling
      // preventDefault on a lone modifier can suppress legitimate chords the
      // user goes on to type.
      if (!isBareModifier(binding)) e.preventDefault()

      // Already capturing (latched by an earlier tap, or started from the mic
      // button): this press ENDS it, and does not arm a new hold.
      if (voiceRef.current.recording && phaseRef.current === 'idle') {
        voiceRef.current.stop()
        return
      }
      if (phaseRef.current !== 'idle') return

      if (mode === 'toggle') {
        startSeqRef.current++
        voiceRef.current.start()
        return
      }
      setPhase('arming')
      voiceRef.current.prewarm()
      armTimerRef.current = setTimeout(() => {
        armTimerRef.current = null
        if (phaseRef.current === 'arming') beginHold()
      }, holdMs)
    }

    const onKeyUp = (e: KeyboardEvent) => {
      const { binding, mode } = cfgRef.current
      if (e.code !== binding.code) return
      const was = phaseRef.current
      if (was === 'idle') return
      if (was === 'holding') {
        // If startup is still in flight this stop is a no-op (no recorder yet);
        // `beginHold`'s resolution handler is what stops that late session.
        disarm(true)
        return
      }
      // Released before the threshold — a TAP.
      clearTimers()
      genRef.current++
      setPhase('idle')
      if (mode === 'hybrid') {
        // Latch on. The pre-warmed mic acquired while arming carries straight
        // into this start(), so the tap path is fast too.
        startSeqRef.current++
        voiceRef.current.start()
      } else {
        // Pure push-to-talk: a tap means nothing. Release the warm mic.
        voiceRef.current.cancel()
      }
    }

    // A release that never arrives is the defining failure of a hold binding.
    // Losing focus or visibility mid-hold is the common cause, so commit what
    // was said instead of leaving the mic open.
    const onBlur = () => { if (phaseRef.current !== 'idle') disarm(true) }
    const onVisibility = () => { if (document.hidden && phaseRef.current !== 'idle') disarm(true) }

    document.addEventListener('keydown', onKeyDown, true)
    document.addEventListener('keyup', onKeyUp, true)
    window.addEventListener('blur', onBlur)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      document.removeEventListener('keyup', onKeyUp, true)
      window.removeEventListener('blur', onBlur)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [beginHold, clearTimers, disarm, setPhase])

  // Unmounting mid-hold would orphan the timers and the session.
  useEffect(() => () => { clearTimers() }, [clearTimers])

  return { config: cfg, phase, holding: phase === 'holding' }
}
