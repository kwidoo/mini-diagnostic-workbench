import type { Point } from '../types'

/**
 * Chart maths that stays safe for long recordings (hundreds of thousands of points):
 * nothing here spreads an array into a function call (`Math.min(...xs)` throws a RangeError
 * once xs exceeds the engine's argument limit), and nothing mutates the caller's data.
 * Downsampling only ever affects what is DRAWN; storage, analysis, exports and the data
 * table always use the original points.
 */

/** Iterative [min, max] of the finite numbers in `values`; null when there are none. */
export function extent(values: Iterable<number | null | undefined>): [number, number] | null {
  let lo = Infinity
  let hi = -Infinity
  for (const v of values) {
    if (typeof v !== 'number' || !Number.isFinite(v)) continue
    if (v < lo) lo = v
    if (v > hi) hi = v
  }
  return lo === Infinity ? null : [lo, hi]
}

/** Streams one coordinate of many point lists without building a combined array. */
export function* coords(lists: Iterable<readonly Point[]>, axis: 0 | 1): Generator<number> {
  for (const pts of lists) for (const p of pts) yield p[axis]
}

/** Time domain [first, last] with a minimum width of 1 s; null when nothing has a finite time. */
export function timeDomain(times: Iterable<number | null | undefined>): [number, number] | null {
  const e = extent(times)
  return e ? [e[0], Math.max(e[1], e[0] + 1)] : null
}

/** Points with a finite time and value (a gap is never bridged; invalid points are not drawn). */
export function validPoints(points: readonly Point[]): Point[] {
  let ok = true
  for (const p of points) if (!Number.isFinite(p[0]) || !Number.isFinite(p[1])) { ok = false; break }
  return ok ? (points as Point[]) : points.filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]))
}

/** Split points into runs wherever an observation is missing; a gap is never bridged by a line. */
export function segments(points: readonly Point[], maxGap?: number): Point[][] {
  if (points.length === 0) return []
  let limit = maxGap
  if (limit === undefined) {
    const gaps = new Float64Array(points.length - 1)
    for (let i = 1; i < points.length; i++) gaps[i - 1] = points[i][0] - points[i - 1][0]
    gaps.sort()
    const median = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 0
    limit = Math.max(3 * median, 1)
  }
  const out: Point[][] = [[points[0]]]
  for (let i = 1; i < points.length; i++) {
    if (points[i][0] - points[i - 1][0] > limit) out.push([])
    out[out.length - 1].push(points[i])
  }
  return out
}

/**
 * Deterministic min/max decimation of ONE gap-free segment for drawing: the segment is cut into
 * `buckets` equal-count buckets and each keeps its first, minimum, maximum and last point in time
 * order. Every local extreme and both ends survive, so the drawn envelope matches the data; the
 * same input always gives the same output. Segments already small enough are returned unchanged.
 */
export function decimate(seg: readonly Point[], buckets: number): Point[] {
  if (seg.length <= buckets * 4) return seg as Point[]
  const out: Point[] = []
  const size = seg.length / buckets
  for (let b = 0; b < buckets; b++) {
    const from = Math.floor(b * size)
    const to = Math.min(seg.length, Math.floor((b + 1) * size))
    if (to <= from) continue
    let iMin = from
    let iMax = from
    for (let i = from + 1; i < to; i++) {
      if (seg[i][1] < seg[iMin][1]) iMin = i
      if (seg[i][1] > seg[iMax][1]) iMax = i
    }
    for (const i of [...new Set([from, iMin, iMax, to - 1])].sort((a, b2) => a - b2)) out.push(seg[i])
  }
  return out
}

/** Segments to draw for one series: gaps kept, each segment decimated in proportion to its length. */
export function drawSegments(points: readonly Point[], budget = 2000): Point[][] {
  const segs = segments(validPoints(points))
  let total = 0
  for (const s of segs) total += s.length
  if (total <= budget * 4) return segs
  return segs.map((s) => decimate(s, Math.max(1, Math.round((budget * s.length) / total))))
}

export function niceTicks(min: number, max: number, count = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return []
  if (min === max) return [min]
  const raw = (max - min) / count
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw
  const ticks: number[] = []
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) ticks.push(Number(v.toFixed(6)))
  return ticks
}

/** Point closest in time to t. Binary search on time-ordered points (falls back to a scan). */
export function nearest(points: readonly Point[], t: number): Point | null {
  const n = points.length
  if (!n || !Number.isFinite(t)) return null
  let sorted = true
  for (let i = 1; i < n && sorted; i++) if (points[i][0] < points[i - 1][0]) sorted = false
  if (!sorted) {
    let best: Point | null = null
    for (const p of points) if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p
    return best
  }
  let lo = 0
  let hi = n - 1
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (points[mid][0] < t) lo = mid + 1
    else hi = mid
  }
  if (lo > 0 && Math.abs(points[lo - 1][0] - t) <= Math.abs(points[lo][0] - t)) return points[lo - 1]
  return points[lo]
}
