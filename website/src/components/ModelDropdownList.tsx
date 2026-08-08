import { useRef, useEffect, useMemo } from 'react'
import { Check, Image as ImageIcon } from 'lucide-react'

import { isPricedMultiplier } from '../providers/modelList'
import type { ModelInfo } from '../providers/types'
import { fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'

/**
 * A row this list can render. Derived from `ModelInfo` rather than re-declared,
 * so the field TYPES cannot drift from the provider contract.
 *
 * Note what this still does not buy: every field but `name` is optional, so
 * adding a field to `ModelInfo` is not a type error here — it simply never
 * reaches the row until someone widens this Pick. Reachability is a manual step;
 * only the shapes are pinned.
 */
export type ModelItem =
  Pick<ModelInfo, 'name'> & Partial<Pick<ModelInfo, 'description' | 'rateMultiplier' | 'supportsVision'>>

/** The multiplier Auto is pinned at, and the baseline the badges are relative to. */
const BASELINE = 1

/**
 * ASCII 'x', not the multiplication sign U+00D7 (`×`).
 *
 * Two reasons, and the second is the decisive one. U+00D7 is a math operator
 * drawn to sit between operands in an equation, so fonts draw it at x-height or
 * below — measured in the shipped bundle it is 63% of a digit's ink height
 * against 73% for 'x', which next to bold lining figures reads as a typo rather
 * than a suffix. And kiro spells it 'x' everywhere itself: `kiro-cli chat
 * --list-models` prints `2.20x` and kiro.dev's comparison table writes `2.2x`.
 * Matching the product beats typographic correctness for a value users will
 * cross-reference against those surfaces.
 */
const SUFFIX = 'x'

/**
 * Render a credit multiplier the way kiro publishes its own rates: at least one
 * decimal, at most two. 1 -> "1.0", 2.2 -> "2.2", 0.25 -> "0.25", 0.05 -> "0.05".
 *
 * Minimum one decimal so the column reads as a consistent set of rates rather
 * than a mix of "1" and "2.2" — this matches kiro.dev's own comparison table,
 * which writes 1.0x / 2.2x / 0.25x / 0.05x.
 *
 * Routed through `fmtNumber`, NOT `toFixed`. The digits land inside translated
 * sentences (`credit_multiplier`, `credit_cost_legend`, …), and `toFixed` always
 * emits Latin digits — wrong for `bn`, which otherwise renders
 * `Auto-এর তুলনায় 2.2x ক্রেডিট খরচ` with a Latin numeral mid-sentence.
 * `website/AGENTS.md` makes the locale seam mandatory ("Never format a date,
 * number, or sort order without naming a locale"), and
 * `website/docs/i18n-catalog.md` names `toFixed` as the case no source scan can
 * detect. `utils/formatCost.ts` is the in-tree precedent.
 *
 * The sub-0.005 branch stays hand-rolled on purpose. `maximumFractionDigits: 2`
 * would flatten anything smaller to "0", and a "0x" badge reads as "this model
 * is free" — the exact misreading `isPricedMultiplier` rejects a literal 0 to
 * avoid. Today's cheapest rate is 0.01x so it is latent, but reading the rate
 * live is precisely an admission that kiro re-prices models. That branch pins
 * its own significant digits and still formats through the locale seam.
 */
export function formatMultiplier(value: number): string {
  // Rounding, NOT formatting — the result is re-rendered through fmtNumber
  // below and never reaches the DOM as a string. Deliberately not
  // `Number(value.toFixed(2))`: that reads as a formatting call at a glance,
  // which is precisely what the locale rule forbids and what a reviewer will
  // stop on.
  const rounded = Math.round(value * 100) / 100
  const digits = rounded === 0
    // Keep enough precision to stay non-zero, then let the locale render it.
    ? fmtNumber(value, { maximumSignificantDigits: 1 })
    : fmtNumber(rounded, { minimumFractionDigits: 1, maximumFractionDigits: 2 })
  return `${digits}${SUFFIX}`
}

/**
 * Cost tier, used ONLY to pick the badge's border colour.
 *
 * Thresholds are set against the real served spread (0.01x to 4.4x as of
 * 2026-08): 'standard' brackets the two multipliers a default user actually sits
 * on (Auto/Terra at 1.0x, Sonnet at 1.3x), so the coloured tiers mean "cheaper
 * than your default" and "dearer than your default" rather than an arbitrary cut.
 */
export function costTier(value: number): 'budget' | 'standard' | 'premium' {
  if (value < BASELINE) return 'budget'
  return value > 1.5 ? 'premium' : 'standard'
}

/** Border-only tint per tier. The digits stay `text-text` in every tier, so the
 *  badge's text contrast is the same guaranteed-legible pair the model name
 *  already uses — the hue never has to carry a contrast minimum, which is what
 *  lets one rule hold across all 22 themes instead of needing per-theme hues.
 *
 *  The tier hue identifies a state, so the BORDER still owes WCAG 1.4.11's 3:1
 *  against the badge surface. `--border-strong` — the obvious neutral, and what
 *  the approved mockup used — measures 1.8:1 dark and 1.48:1 light, i.e. the
 *  standard-tier badge reads as having no outline at all and so is hard to tell
 *  from the no-badge case. `--muted` is the next token up and clears the bar in
 *  both (5.24:1 / 6.78:1) while staying visibly quieter than the ok/warn hues. */
const TIER_BORDER: Record<ReturnType<typeof costTier>, string> = {
  budget: 'border-ok',
  standard: 'border-muted',
  premium: 'border-warn',
}

/** Vision rows: a muted, rounded pill with an image glyph — native, not loud.
 *  The composer's `FilePreviewStrip` already reads as an image surface (80px
 *  thumbs with numbered accent circles), so the picker should echo that surface
 *  with one small tag. Border-only, muted: it must not compete with the credit
 *  multiplier that sits at the same trailing edge, and it stays legible on any
 *  theme because the glyph is `text-muted` against `bg-bg-elevated`. */
function VisionBadge() {
  return (
    <span aria-hidden="true" className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full border border-border bg-bg-elevated text-[10px] leading-none text-muted">
      <ImageIcon size={10} className="lucide-inline" />
      Image
    </span>
  )
}

/** A provider-coloured top accent for grouped sections — one thin bar, not a chip.
 *  Uses the existing chat palette (accent / ok / info / warn) so the picker
 *  reads as themeless, like ContextBar's layered bar. No new colour system. */
function GroupLabel({ label, accentClass }: { label: string; accentClass: string }) {
  return (
    <div className="flex items-center gap-2 px-2.5 pt-3 pb-1">
      <span className={`inline-block h-3 w-1 rounded-full ${accentClass}`} aria-hidden="true" />
      <span className="text-[10.5px] font-semibold tracking-wide uppercase text-muted">{label}</span>
    </div>
  )
}

function ModelRow({
  model, active, activeRef, onSelect,
}: {
  model: ModelItem
  active: boolean
  activeRef: React.Ref<HTMLButtonElement> | undefined
  onSelect: (name: string) => void
}) {
  const mult = model.rateMultiplier
  const vision = model.supportsVision === true
  return (
    <button
      key={model.name}
      ref={active ? activeRef : undefined}
      role="option"
      aria-selected={active}
      tabIndex={-1}
      data-vision={vision ? '1' : undefined}
      data-model-name={model.name}
      className={`w-full text-left px-2.5 py-2 flex flex-col gap-0.5 rounded-md cursor-pointer transition-all border-none bg-transparent ${active ? 'bg-accent-subtle' : 'hover:bg-bg-hover'}`}
      onClick={() => onSelect(model.name)}
    >
      <div className="flex items-center gap-2">
        <span data-model-name className={`text-[13px] font-mono font-semibold truncate ${active ? 'text-accent' : 'text-text'}`}>{model.name}</span>
        {active && <span className="text-accent text-[12px]"><Check className="lucide-inline" /></span>}
        <span className="ml-auto inline-flex items-center gap-1.5 shrink-0">
          {vision && <VisionBadge />}
          {isPricedMultiplier(mult) && (
            <span className={`shrink-0 min-w-[2.9rem] text-center px-1.5 py-[3px] rounded-full border bg-bg-elevated font-mono font-semibold tabular-nums text-[10.5px] leading-none text-text ${TIER_BORDER[costTier(mult)]}`}>
              <span aria-hidden="true">{formatMultiplier(mult)}</span>
              <span className="sr-only">{i18nT(
                model.name === 'auto'
                  ? 'components.modelDropdownList.credit_multiplier_baseline'
                  : 'components.modelDropdownList.credit_multiplier',
                { value: formatMultiplier(mult) },
              )}</span>
            </span>
          )}
        </span>
      </div>
      {model.name === 'auto'
        ? <span className="text-[12px] text-muted leading-tight">{i18nT('components.modelDropdownList.auto_default')}</span>
        : model.description && <span className="text-[12px] text-muted leading-tight">{model.description}</span>}
      <span className="sr-only">{vision ? 'Supports image input' : ''}</span>
    </button>
  )
}

/** Shared model list used in dropdown portals across AgentsPage and ChatPage */
export default function ModelDropdownList({ models, activeModel, onSelect }: {
  models: ModelItem[]; activeModel: string; onSelect: (name: string) => void
}) {
  const activeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'center', behavior: 'instant' })
  }, [])
  const groups = useMemo(() => {
    // Group the already-ordered list (withAutoFirst's order is canonical) so the
    // existing provider ordering is kept, but vision rows sit under a native-
    // feeling Vision section. Unknown rows (no flag) fall into Text-only so a
    // member whose gateway predates the flag does not lose its rows to a phantom.
    const vision = models.filter(m => m.supportsVision === true)
    const text = models.filter(m => m.supportsVision !== true)
    if (vision.length === 0) return null
    if (text.length === 0) return { vision, text: null as ModelItem[] | null }
    return { vision, text }
  }, [models])
  if (groups) {
    return (
      <div className="overflow-y-auto flex flex-col gap-0.5">
        <GroupLabel label="Vision — image input" accentClass="bg-accent" />
        {groups.vision.map(m => (
          <ModelRow
            key={m.name}
            model={m}
            active={activeModel === m.name}
            activeRef={activeModel === m.name ? activeRef : undefined}
            onSelect={onSelect}
          />
        ))}
        {groups.text && groups.text.length > 0 && (
          <>
            <GroupLabel label="Text" accentClass="bg-muted" />
            {groups.text.map(m => (
              <ModelRow
                key={m.name}
                model={m}
                active={activeModel === m.name}
                activeRef={activeModel === m.name ? activeRef : undefined}
                onSelect={onSelect}
              />
            ))}
          </>
        )}
        {models.length === 0 && <div className="px-3 py-2 text-[13px] text-muted italic">{i18nT('components.modelDropdownList.no_matches')}</div>}
      </div>
    )
  }
  return (
    <div className="overflow-y-auto flex flex-col gap-0.5">
      {models.map(m => {
        const active = activeModel === m.name
        return (
          <ModelRow
            key={m.name}
            model={m}
            active={active}
            activeRef={active ? activeRef : undefined}
            onSelect={onSelect}
          />
        )
      })}
      {models.length === 0 && <div className="px-3 py-2 text-[13px] text-muted italic">{i18nT('components.modelDropdownList.no_matches')}</div>}
    </div>
  )
}
