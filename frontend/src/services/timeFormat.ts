export interface TimeUnit { name: string; ticks: number }
export interface CalendarMonth { name: string; days: number }
export interface CalendarRules {
  mode?: 'fixed_units' | 'variable_months'
  months?: CalendarMonth[]
  leap_rule?: { cycle: number; extra_days?: number }
  year_zero?: boolean
  era?: string
}
export interface TimeSystemFormat {
  epoch_label?: string
  unit_name: string
  units: TimeUnit[]
  calendar_rules?: CalendarRules
}

function validVariableCalendar(rules?: CalendarRules): rules is CalendarRules & { mode: 'variable_months'; months: CalendarMonth[] } {
  return rules?.mode === 'variable_months'
    && Array.isArray(rules.months)
    && rules.months.length > 0
    && rules.months.every(month => typeof month.name === 'string' && month.name.trim() && Number.isInteger(month.days) && month.days > 0)
}

function yearLength(year: number, rules: CalendarRules & { mode: 'variable_months'; months: CalendarMonth[] }): number {
  const base = rules.months.reduce((total, month) => total + month.days, 0)
  const leap = rules.leap_rule
  return base + (leap && Number.isInteger(leap.cycle) && leap.cycle > 0 && year > 0 && year % leap.cycle === 0
    ? Math.max(0, leap.extra_days || 0)
    : 0)
}

/** Format a normalized tick coordinate using a deterministic custom calendar. */
function formatVariableCalendar(value: number, rules: CalendarRules & { mode: 'variable_months'; months: CalendarMonth[] }, unitName: string): string {
  const negative = value < 0
  let remaining = Math.abs(Math.trunc(value))
  let year = 0

  if (negative) {
    while (remaining > 0) {
      const previous = year - 1
      const length = yearLength(previous, rules)
      if (remaining < length) {
        year = previous
        break
      }
      remaining -= length
      year = previous
    }
  } else {
    while (remaining >= yearLength(year, rules)) {
      remaining -= yearLength(year, rules)
      year += 1
    }
  }

  const monthTotal = rules.months.reduce((total, month) => total + month.days, 0)
  if (remaining >= monthTotal) {
    const era = rules.era?.trim() || ''
    const eraSuffix = !rules.year_zero && year <= 0 ? '前' : ''
    return `${era ? `${era}${eraSuffix} ` : ''}${negative ? '−' : ''}${!rules.year_zero && year <= 0 ? `${Math.abs(year) + 1}年` : `${year}年`} 闰日${remaining - monthTotal + 1}${unitName}`
  }

  let monthIndex = 0
  while (monthIndex < rules.months.length && remaining >= rules.months[monthIndex].days) {
    remaining -= rules.months[monthIndex].days
    monthIndex += 1
  }
  const month = monthIndex < rules.months.length ? rules.months[monthIndex].name : '闰日'
  const yearLabel = !rules.year_zero && year <= 0 ? `${Math.abs(year) + 1}年` : `${year}年`
  const era = rules.era?.trim() || ''
  const eraSuffix = !rules.year_zero && year <= 0 ? '前' : ''
  return `${era ? `${era}${eraSuffix} ` : ''}${negative ? '−' : ''}${yearLabel} ${month}${remaining + 1}${unitName}`
}

/** Format a normalized tick coordinate using largest-first display units. */
export function formatWorldTime(value: number | null | undefined, system?: TimeSystemFormat): string {
  if (value === null || value === undefined) return '持续'
  if (!Number.isFinite(value)) return '—'
  if (!system) return String(value)

  const calendar = system.calendar_rules
  if (validVariableCalendar(calendar)) {
    return formatVariableCalendar(value, calendar, system.unit_name)
  }

  const sign = value < 0 ? '−' : ''
  let remaining = Math.abs(Math.trunc(value))
  const parts: string[] = []
  const units = [...(system.units || [])]
    .filter(unit => Number.isInteger(unit.ticks) && unit.ticks > 0 && unit.name.trim())
    .sort((a, b) => b.ticks - a.ticks)

  for (const unit of units) {
    const count = Math.floor(remaining / unit.ticks)
    if (count > 0) {
      parts.push(`${count}${unit.name}`)
      remaining %= unit.ticks
    }
  }
  if (remaining > 0 || parts.length === 0) parts.push(`${remaining}${system.unit_name}`)
  const epoch = system.epoch_label?.trim()
  return `${epoch ? `${epoch} ` : ''}${sign}${parts.join(' ')}`
}
