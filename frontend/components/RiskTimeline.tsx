"use client";

/**
 * 7-day risk timeline.
 *
 * Two measures with different units — risk (0–1) and rainfall (mm) — so they
 * get two stacked panels sharing one x-axis, never a dual y-axis. A dual axis
 * would let the reader infer a crossover that the data does not contain.
 *
 * Each panel carries one series, so neither needs a legend; the panel title
 * names it. Colors are the validated --viz-* tokens, not brand purple for both
 * (purple↔blue is a protan/deutan confusion pair, hence aqua for rainfall).
 */

import { useId, useMemo, useState } from "react";

import type { ForecastPoint } from "@/lib/types";

const W = 720;
const PAD_L = 46;
const PAD_R = 14;
const RISK_TOP = 14;
const RISK_H = 148;
const GAP = 30;
const RAIN_H = 88;
const AXIS_H = 26;

const RAIN_TOP = RISK_TOP + RISK_H + GAP;
const RAIN_BASE = RAIN_TOP + RAIN_H;
const H = RAIN_BASE + AXIS_H;
const PLOT_W = W - PAD_L - PAD_R;

interface Props {
  forecast: ForecastPoint[];
  /** Shown when rainfall came back empty because no source answered. */
  rainfallAvailable?: boolean;
}

export default function RiskTimeline({ forecast, rainfallAvailable = true }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);
  const clipId = useId();

  const points = forecast.slice(0, 7);

  const geometry = useMemo(() => {
    const n = points.length;
    if (n === 0) return null;

    const band = PLOT_W / n;
    const cx = (i: number) => PAD_L + band * (i + 0.5);
    const riskY = (r: number) => RISK_TOP + (1 - clamp01(r)) * RISK_H;

    // Round the rainfall scale up to a friendly ceiling so the axis reads in
    // whole numbers rather than to the exact maximum.
    const rawMax = Math.max(...points.map((p) => p.rainfall_mm), 0);
    const rainMax = rawMax <= 0 ? 1 : niceCeiling(rawMax);
    const rainH = (mm: number) => Math.max(0, (mm / rainMax) * RAIN_H);

    const line = points.map((p, i) => `${cx(i)},${riskY(p.risk)}`).join(" ");
    const area =
      `${PAD_L + band * 0.5},${RISK_TOP + RISK_H} ` +
      line +
      ` ${cx(n - 1)},${RISK_TOP + RISK_H}`;

    const peakRisk = points.reduce(
      (best, p, i) => (p.risk > points[best].risk ? i : best),
      0,
    );
    const peakRain = points.reduce(
      (best, p, i) => (p.rainfall_mm > points[best].rainfall_mm ? i : best),
      0,
    );

    return { n, band, cx, riskY, rainMax, rainH, line, area, peakRisk, peakRain };
  }, [points]);

  if (!geometry) {
    return <p className="muted">No forecast available for this area yet.</p>;
  }

  const { n, band, cx, riskY, rainMax, rainH, line, area, peakRisk, peakRain } =
    geometry;
  const active = hover === null ? null : points[hover];

  return (
    <div>
      <div style={{ position: "relative" }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height="auto"
          role="img"
          aria-label={`Seven-day outlook. Peak risk ${(points[peakRisk].risk * 100).toFixed(0)} percent on day ${points[peakRisk].day}.`}
          style={{ display: "block", overflow: "visible" }}
          onMouseLeave={() => setHover(null)}
        >
          <defs>
            <clipPath id={clipId}>
              <rect x={PAD_L} y={RISK_TOP} width={PLOT_W} height={RISK_H} />
            </clipPath>
          </defs>

          {/* ---- Panel 1: risk ---- */}
          <text x={PAD_L} y={RISK_TOP - 2} className="viz-panel-title">
            Risk level
          </text>

          {[0, 0.25, 0.5, 0.75, 1].map((t) => (
            <g key={t}>
              <line
                x1={PAD_L}
                x2={W - PAD_R}
                y1={riskY(t)}
                y2={riskY(t)}
                stroke="var(--viz-grid)"
                strokeWidth={1}
              />
              <text
                x={PAD_L - 8}
                y={riskY(t) + 4}
                textAnchor="end"
                className="viz-tick"
              >
                {t === 0 || t === 1 ? `${t * 100}%` : ""}
              </text>
            </g>
          ))}

          <g clipPath={`url(#${clipId})`}>
            <polygon points={area} fill="var(--viz-risk-fill)" />
            <polyline
              points={line}
              fill="none"
              stroke="var(--viz-risk)"
              strokeWidth={2}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          </g>

          {points.map((p, i) => (
            <circle
              key={p.day}
              cx={cx(i)}
              cy={riskY(p.risk)}
              r={hover === i ? 6 : 4.5}
              fill="var(--viz-risk)"
              // 2px surface ring keeps the marker legible where it overlaps
              // the area fill.
              stroke="var(--surface-raised)"
              strokeWidth={2}
            />
          ))}

          {/* One direct label, on the peak — never a number on every point. */}
          <text
            x={cx(peakRisk)}
            y={riskY(points[peakRisk].risk) - 12}
            textAnchor="middle"
            className="viz-label"
          >
            {(points[peakRisk].risk * 100).toFixed(0)}%
          </text>

          {/* ---- Panel 2: rainfall ---- */}
          <text x={PAD_L} y={RAIN_TOP - 8} className="viz-panel-title">
            Forecast rainfall{" "}
            <tspan className="viz-tick">
              {rainfallAvailable ? "(mm/day)" : "— unavailable this cycle"}
            </tspan>
          </text>

          <line
            x1={PAD_L}
            x2={W - PAD_R}
            y1={RAIN_BASE}
            y2={RAIN_BASE}
            stroke="var(--viz-grid)"
            strokeWidth={1}
          />
          <text x={PAD_L - 8} y={RAIN_TOP + 10} textAnchor="end" className="viz-tick">
            {rainMax}
          </text>

          {points.map((p, i) => {
            const h = rainH(p.rainfall_mm);
            // 2px surface gap between adjacent bars.
            const bw = Math.max(6, band - 14);
            return (
              <rect
                key={p.day}
                x={cx(i) - bw / 2}
                y={RAIN_BASE - h}
                width={bw}
                height={h}
                rx={4}
                fill="var(--viz-rain)"
                opacity={hover === null || hover === i ? 1 : 0.55}
              />
            );
          })}

          {rainfallAvailable && points[peakRain].rainfall_mm > 0 && (
            <text
              x={cx(peakRain)}
              y={RAIN_BASE - rainH(points[peakRain].rainfall_mm) - 7}
              textAnchor="middle"
              className="viz-label"
            >
              {points[peakRain].rainfall_mm.toFixed(0)}
            </text>
          )}

          {/* ---- Shared x-axis ---- */}
          {points.map((p, i) => (
            <text
              key={p.day}
              x={cx(i)}
              y={H - 8}
              textAnchor="middle"
              className="viz-tick"
            >
              {p.day === 0 ? "Today" : dayLabel(p.date)}
            </text>
          ))}

          {/* ---- Crosshair + hit targets (wider than the marks) ---- */}
          {hover !== null && (
            <line
              x1={cx(hover)}
              x2={cx(hover)}
              y1={RISK_TOP}
              y2={RAIN_BASE}
              stroke="var(--viz-axis)"
              strokeWidth={1}
              strokeDasharray="3 3"
              opacity={0.6}
            />
          )}
          {points.map((p, i) => (
            <rect
              key={p.day}
              x={PAD_L + band * i}
              y={RISK_TOP}
              width={band}
              height={RAIN_BASE - RISK_TOP}
              fill="transparent"
              onMouseEnter={() => setHover(i)}
              onFocus={() => setHover(i)}
              onBlur={() => setHover(null)}
              tabIndex={0}
              role="button"
              aria-label={`Day ${p.day}: risk ${(p.risk * 100).toFixed(0)} percent, rainfall ${p.rainfall_mm.toFixed(0)} millimetres`}
              style={{ cursor: "crosshair", outlineOffset: -2 }}
            />
          ))}
        </svg>

        {active && (
          <div
            className="viz-tooltip"
            style={{
              left: `${((cx(hover!) ) / W) * 100}%`,
            }}
          >
            <strong>
              {active.day === 0 ? "Today" : fullLabel(active.date)}
            </strong>
            <span>
              <i style={{ background: "var(--viz-risk)" }} />
              Risk {(active.risk * 100).toFixed(0)}%
            </span>
            <span>
              <i style={{ background: "var(--viz-rain)" }} />
              Rain {active.rainfall_mm.toFixed(0)} mm
            </span>
            {active.note && <em>{active.note}</em>}
          </div>
        )}
      </div>

      <button
        type="button"
        className="viz-table-toggle"
        onClick={() => setShowTable((v) => !v)}
        aria-expanded={showTable}
      >
        {showTable ? "Hide data table" : "View as table"}
      </button>

      {showTable && (
        <table className="table" style={{ marginTop: 10 }}>
          <caption className="viz-tick" style={{ textAlign: "left", paddingBottom: 6 }}>
            Seven-day outlook
          </caption>
          <thead>
            <tr>
              <th scope="col">Day</th>
              <th scope="col">Date</th>
              <th scope="col">Risk</th>
              <th scope="col">Rainfall (mm)</th>
            </tr>
          </thead>
          <tbody>
            {points.map((p) => (
              <tr key={p.day}>
                <td>{p.day === 0 ? "Today" : `+${p.day}`}</td>
                <td>{fullLabel(p.date)}</td>
                <td>{(p.risk * 100).toFixed(0)}%</td>
                <td>{p.rainfall_mm.toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <style jsx>{`
        .viz-tooltip {
          position: absolute;
          top: 4px;
          transform: translateX(-50%);
          pointer-events: none;
          background: var(--surface-raised);
          border: 1px solid var(--hairline-strong);
          border-radius: 10px;
          padding: 8px 11px;
          font-size: 12px;
          line-height: 1.5;
          color: var(--text-primary);
          box-shadow: var(--shadow-card);
          display: grid;
          gap: 2px;
          white-space: nowrap;
          z-index: 5;
        }
        .viz-tooltip span {
          display: flex;
          align-items: center;
          gap: 6px;
          color: var(--text-secondary);
        }
        .viz-tooltip i {
          width: 8px;
          height: 8px;
          border-radius: 2px;
          display: inline-block;
        }
        .viz-tooltip em {
          color: var(--text-muted);
          font-style: normal;
          font-size: 11px;
        }
        .viz-table-toggle {
          margin-top: 12px;
          background: none;
          border: 0;
          padding: 0;
          font: inherit;
          font-size: 13px;
          color: var(--accent);
          cursor: pointer;
          text-decoration: underline;
        }
      `}</style>

      {/* SVG text can't be styled by styled-jsx scoping, so these live global. */}
      <style jsx global>{`
        .viz-panel-title {
          font-size: 12px;
          font-weight: 650;
          fill: var(--text-primary);
        }
        .viz-tick {
          font-size: 11px;
          fill: var(--text-muted);
          font-weight: 400;
        }
        .viz-label {
          font-size: 11px;
          font-weight: 650;
          fill: var(--text-secondary);
        }
      `}</style>
    </div>
  );
}

function clamp01(v: number): number {
  return Math.max(0, Math.min(1, v));
}

function niceCeiling(v: number): number {
  const step = v <= 10 ? 2 : v <= 50 ? 10 : v <= 200 ? 25 : 50;
  return Math.ceil(v / step) * step;
}

function dayLabel(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleDateString("en-GB", { weekday: "short" });
}

function fullLabel(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}
