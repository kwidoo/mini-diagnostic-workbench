import type { Point } from '../types'

/** Split points into runs wherever an observation is missing; a gap is never bridged by a line. */
export function segments(points: Point[], maxGap?: number): Point[][] {
  if (points.length === 0) return []
  const gaps = points.slice(1).map((p, i) => p[0] - points[i][0]).sort((a, b) => a - b)
  const median = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 0
  const limit = maxGap ?? Math.max(3 * median, 1)
  const out: Point[][] = [[points[0]]]
  for (let i = 1; i < points.length; i++) {
    if (points[i][0] - points[i - 1][0] > limit) out.push([])
    out[out.length - 1].push(points[i])
  }
  return out
}

export function niceTicks(min: number, max: number, count = 4): number[] {
  if (min === max) return [min]
  const raw = (max - min) / count
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw
  const ticks: number[] = []
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) ticks.push(Number(v.toFixed(6)))
  return ticks
}

export function nearest(points: Point[], t: number): Point | null {
  let best: Point | null = null
  for (const p of points) if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p
  return best
}
