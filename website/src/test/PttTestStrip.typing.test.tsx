import { describe, expect, it } from 'vitest'

import { isTypingTarget } from '../components/PttTestStrip'

/**
 * The test strip listens on the document in the CAPTURE phase so it can see a key
 * press wherever focus happens to be. That reach is also its hazard: a first-run
 * UX review found that typing into a sibling setting (the Language box below it)
 * flashed the strip's amber "that was a different key" state on every keystroke —
 * a false-alarm loop triggered by editing an unrelated row.
 */
describe('isTypingTarget — the strip must ignore typing in text fields', () => {
  const el = (tag: string, contentEditable = false) => {
    const node = document.createElement(tag)
    if (contentEditable) Object.defineProperty(node, 'isContentEditable', { value: true })
    return node
  }

  it('excludes text-entry targets', () => {
    expect(isTypingTarget(el('input'))).toBe(true)
    expect(isTypingTarget(el('textarea'))).toBe(true)
    expect(isTypingTarget(el('div', true))).toBe(true)
  })

  // Picking the shortcut key leaves focus on that dropdown, which is exactly
  // when the user reaches for the strip — and a select takes no character input.
  it('does NOT exclude a select', () => {
    expect(isTypingTarget(el('select'))).toBe(false)
  })

  it('does NOT exclude ordinary page targets', () => {
    expect(isTypingTarget(el('div'))).toBe(false)
    expect(isTypingTarget(el('button'))).toBe(false)
    expect(isTypingTarget(document.body)).toBe(false)
  })

  it('survives a target that is not an element', () => {
    expect(isTypingTarget(null)).toBe(false)
    expect(isTypingTarget(new EventTarget())).toBe(false)
    expect(isTypingTarget(document)).toBe(false)
  })
})
