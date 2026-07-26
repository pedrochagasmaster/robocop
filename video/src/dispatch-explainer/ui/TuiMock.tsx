import React from "react";
import { colors, mono } from "../theme";

/**
 * Mock of the Dispatch Textual UI. The layout follows the real Overview screen:
 * a docked sidebar, a status strip, one unified Jobs table with running jobs
 * pinned first, a live log pane, and the action bar.
 *
 * Designed at these dimensions and scaled to whatever slot a scene gives it, so
 * the mock is identical in every scene that shows the TUI.
 */
export const TUI = { width: 1080, height: 700 };

const NAV_ITEMS = [
  { icon: "⌂", label: "Overview" },
  { icon: "⊞", label: "New Job" },
  { icon: "▸", label: "View Logs" },
  { icon: "◷", label: "History" },
  { icon: "☰", label: "Browse" },
];

const JOB_ROWS = [
  {
    id: "20260726T141203Z",
    source: "churn_base.sql",
    destination: "ads_lab.you_churn_base + Csv",
    state: "Running",
    elapsed: "04:12",
  },
  {
    id: "20260726T132251Z",
    source: "regions.sql",
    destination: "regions.csv",
    state: "Finished",
    elapsed: "11:38",
  },
  {
    id: "20260726T101812Z",
    source: "monthly_spend.sql",
    destination: "ads_lab.you_monthly_spend",
    state: "Finished",
    elapsed: "26:05",
  },
  {
    id: "20260725T173044Z",
    source: "ads_lab.you_dim_geo",
    destination: "you_dim_geo.csv",
    state: "Finished",
    elapsed: "02:57",
  },
];

const STATE_COLORS: Record<string, string> = {
  Running: colors.accent,
  Finished: colors.textMuted,
  Failed: colors.danger,
};

const COLUMNS = [
  { key: "id", label: "ID", width: 200 },
  { key: "source", label: "Source", width: 190 },
  { key: "destination", label: "Destination", width: 280 },
  { key: "state", label: "State", width: 110 },
  { key: "elapsed", label: "Elapsed", width: 90 },
];

const KEY_HINTS = [
  ["N", "New Job"],
  ["V", "View Logs"],
  ["C", "Cancel"],
  ["H", "History"],
  ["B", "Browse"],
  ["/", "Filter"],
];

export const TuiMock: React.FC<{
  /** Rendered width; the mock scales itself from its design size. */
  width: number;
  /** How many rows of the Jobs table are painted, for staggered repopulation. */
  visibleJobRows?: number;
  logTail: string[];
}> = ({ width, visibleJobRows = JOB_ROWS.length, logTail }) => {
  return (
    <div
      style={{
        width,
        height: (width / TUI.width) * TUI.height,
        overflow: "hidden",
        borderRadius: 12,
        border: `1px solid ${colors.border}`,
        backgroundColor: colors.bgDeep,
      }}
    >
      <div
        style={{
          width: TUI.width,
          height: TUI.height,
          scale: width / TUI.width,
          transformOrigin: "top left",
          display: "flex",
          fontFamily: mono,
          fontSize: 22,
          color: colors.text,
        }}
      >
        <Sidebar />
        <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
          <StatusStrip />
          <JobsTable visibleJobRows={visibleJobRows} />
          <DetailPane logTail={logTail} />
          <ActionBar />
          <Footer />
        </div>
      </div>
    </div>
  );
};

const Sidebar: React.FC = () => {
  return (
    <div
      style={{
        width: 210,
        flexShrink: 0,
        backgroundColor: colors.panelAlt,
        borderRight: `1px solid ${colors.border}`,
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          padding: "16px 22px",
          fontWeight: 700,
          backgroundColor: colors.bgDeep,
        }}
      >
        Dispatch
      </div>
      <div style={{ padding: "14px 0", flex: 1 }}>
        {NAV_ITEMS.map((item, index) => {
          const active = index === 0;
          return (
            <div
              key={item.label}
              style={{
                padding: "10px 18px",
                display: "flex",
                gap: 12,
                color: active ? colors.text : colors.textMuted,
                fontWeight: active ? 700 : 400,
                backgroundColor: active ? colors.accentSoft : "transparent",
                borderLeft: `4px solid ${active ? colors.accent : "transparent"}`,
              }}
            >
              <span>{item.icon}</span>
              <span>{item.label}</span>
            </div>
          );
        })}
      </div>
      <div
        style={{
          padding: "0 22px 16px",
          display: "flex",
          flexDirection: "column",
          gap: 8,
          color: colors.textMuted,
          fontSize: 20,
        }}
      >
        <span style={{ color: colors.accent }}>KRB 7h 41m</span>
        <span>v1.0</span>
        <span style={{ color: colors.textDim }}>? help</span>
      </div>
    </div>
  );
};

const StatusStrip: React.FC = () => {
  const cells = [
    { label: "KERBEROS", value: "7h 41m", tone: colors.accent },
    { label: "RUNNING", value: "1 / 2", tone: colors.text },
    { label: "FINISHED 7D", value: "6", tone: colors.text },
    { label: "FAILED 7D", value: "0", tone: colors.textMuted },
  ];

  return (
    <div
      style={{
        display: "flex",
        gap: 34,
        padding: "16px 22px",
        borderBottom: `1px solid ${colors.border}`,
      }}
    >
      {cells.map((cell) => (
        <span key={cell.label} style={{ display: "flex", gap: 10 }}>
          <span style={{ color: colors.textDim }}>{cell.label}</span>
          <span style={{ color: cell.tone }}>{cell.value}</span>
        </span>
      ))}
    </div>
  );
};

const JobsTable: React.FC<{ visibleJobRows: number }> = ({
  visibleJobRows,
}) => {
  return (
    <div style={{ padding: "14px 22px 0" }}>
      <div style={{ display: "flex", gap: 10, paddingBottom: 12 }}>
        <span style={{ fontWeight: 700 }}>Jobs</span>
        <span style={{ color: colors.textDim }}>
          · running first · last 7 days
        </span>
      </div>
      <div
        style={{
          display: "flex",
          gap: 18,
          paddingBottom: 8,
          borderBottom: `1px solid ${colors.border}`,
          color: colors.textDim,
          fontSize: 20,
        }}
      >
        {COLUMNS.map((column) => (
          <span key={column.key} style={{ width: column.width }}>
            {column.label}
          </span>
        ))}
      </div>
      {JOB_ROWS.map((row, index) => {
        const painted = index < visibleJobRows;
        const selected = index === 0;
        return (
          <div
            key={row.id}
            style={{
              display: "flex",
              gap: 18,
              padding: "9px 0",
              opacity: painted ? 1 : 0,
              backgroundColor: selected && painted ? colors.panelRaised : "transparent",
            }}
          >
            <span style={{ width: COLUMNS[0].width, color: colors.textMuted }}>
              {row.id}
            </span>
            <span style={{ width: COLUMNS[1].width }}>{row.source}</span>
            <span style={{ width: COLUMNS[2].width, color: colors.textMuted }}>
              {row.destination}
            </span>
            <span
              style={{
                width: COLUMNS[3].width,
                color: STATE_COLORS[row.state],
                fontWeight: 700,
              }}
            >
              {row.state}
            </span>
            <span style={{ width: COLUMNS[4].width, color: colors.textMuted }}>
              {row.elapsed}
            </span>
          </div>
        );
      })}
    </div>
  );
};

const DetailPane: React.FC<{ logTail: string[] }> = ({ logTail }) => {
  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        margin: "16px 22px 0",
        padding: "12px 16px",
        border: `1px solid ${colors.border}`,
        borderRadius: 8,
        backgroundColor: colors.panelAlt,
        display: "flex",
        flexDirection: "column",
        gap: 8,
        overflow: "hidden",
      }}
    >
      <span style={{ color: colors.textDim, fontSize: 20 }}>
        20260726T141203Z · run.log
      </span>
      <div style={{ fontSize: 20, lineHeight: 1.5, color: colors.textMuted }}>
        {logTail.map((line, index) => (
          <div key={index} style={{ whiteSpace: "pre" }}>
            {line}
          </div>
        ))}
      </div>
    </div>
  );
};

const ActionBar: React.FC = () => {
  const buttons = [
    { label: "New Job [N]", primary: true },
    { label: "View Logs [V]", primary: false },
    { label: "Cancel [C]", primary: false },
  ];

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "14px 22px",
      }}
    >
      <span style={{ flex: 1, color: colors.textDim, fontSize: 20 }}>
        [14:16:04] job 20260726T141203Z launched
      </span>
      {buttons.map((button) => (
        <span
          key={button.label}
          style={{
            padding: "7px 16px",
            borderRadius: 6,
            fontSize: 20,
            color: button.primary ? colors.bgDeep : colors.textMuted,
            backgroundColor: button.primary ? colors.accent : colors.panelRaised,
            border: `1px solid ${button.primary ? colors.accent : colors.border}`,
          }}
        >
          {button.label}
        </span>
      ))}
    </div>
  );
};

const Footer: React.FC = () => {
  return (
    <div
      style={{
        display: "flex",
        gap: 22,
        padding: "10px 22px",
        backgroundColor: colors.panelAlt,
        borderTop: `1px solid ${colors.border}`,
        fontSize: 19,
      }}
    >
      {KEY_HINTS.map(([key, label]) => (
        <span key={key} style={{ display: "flex", gap: 7 }}>
          <span style={{ color: colors.accent }}>{key}</span>
          <span style={{ color: colors.textDim }}>{label}</span>
        </span>
      ))}
    </div>
  );
};
