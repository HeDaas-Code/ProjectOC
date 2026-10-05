import { describe, expect, it } from 'vitest'
import { formatWorldTime } from '../timeFormat'

const axis = { epoch_label: '星历', unit_name: '日', units: [{ name: '年', ticks: 360 }, { name: '月', ticks: 30 }] }

describe('formatWorldTime', () => {
  it('decomposes normalized ticks into the world units', () => {
    expect(formatWorldTime(395, axis)).toBe('星历 1年 1月 5日')
  })
  it('supports negative values and zero', () => {
    expect(formatWorldTime(-30, axis)).toBe('星历 −1月')
    expect(formatWorldTime(0, axis)).toBe('星历 0日')
  })
  it('renders open intervals and safely ignores malformed units', () => {
    expect(formatWorldTime(null, axis)).toBe('持续')
    expect(formatWorldTime(4, { unit_name: 'tick', units: [{ name: 'bad', ticks: 0 }] })).toBe('4tick')
  })
})

it('formats variable month calendars and leap days deterministically', () => {
  const calendar = {
    epoch_label: '王国纪年',
    unit_name: '日',
    units: [],
    calendar_rules: {
      mode: 'variable_months' as const,
      months: [{ name: '霜月', days: 28 }, { name: '融月', days: 35 }],
      leap_rule: { cycle: 5, extra_days: 2 },
      year_zero: true,
    },
  }
  expect(formatWorldTime(0, calendar)).toBe('0年 霜月1日')
  expect(formatWorldTime(28, calendar)).toBe('0年 融月1日')
  expect(formatWorldTime(63, calendar)).toBe('1年 霜月1日')
  expect(formatWorldTime(315, calendar)).toBe('5年 霜月1日')
  expect(formatWorldTime(315 + 63, calendar)).toBe('5年 闰日1日')
})
