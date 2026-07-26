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
export const TUI = { width: 1080, height: 660 };

const NAV_ITEMS = [
  { icon: "⌂", label: "Overview" },
  { icon: "⊞", label: "New Job" },
  { icon: "▸", label: "View Logs" },
  { icon: "◷", label: "History" },
  { icon: "☰", label: "Browse" },
];

/**
 * IDs are shortened the way `format_job_id` does it, and the Source and
 * Destination labels follow `_source_label` / `_dest_label`: a file or table
 * name on the left, and either `schema.table` or the bare destination type on
 * the right.
 */
const JOB_ROWS = [
  {
    id: "141203Z_k4m2xr",
    source: "churn_base.sql",
    destination: "ads_lab.churn_base",
    state: "Running",
    elapsed: "04:12",
  },
  {
    id: "132251Z_p8q1vd",
    source: "regions.sql",
    destination: "Csv",
    state: "Finished",
    elapsed: "11:38",
  },
  {
    id: "101812Z_a2f9ct",
    source: "monthly_spend.sql",
    destination: "ads_lab.spend_2026_07",
    state: "Finished",
    elapsed: "26:05",
  },
  {
    id: "173044Z_z7t3bn",
    source: "ads_lab.dim_geo",
    destination: "Csv",
    state: "Finished",
    elapsed: "02:57",
  },
];

const STATE_COLORS: Record<string, string> = {
  Running: colors.accent,
  Finished: colors.textMuted,
  Failed: colors.danger,
};

/** Sized to exactly fill the main pane: 168+202+248+100+88 plus four 5px gaps. */
const TABLE_FONT = 19;
const COLUMN_GAP = 5;
const COLUMNS = [
  { key: "id", label: "ID", width: 168 },
  { key: "source", label: "Source", width: 202 },
  { key: "destination", label: "Destination", width: 248 },
  { key: "state", label: "State", width: 100 },
  { key: "elapsed", label: "Elapsed", width: 88, align: "right" as const },
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
        gap: 26,
        padding: "16px 22px",
        fontSize: 20,
        borderBottom: `1px solid ${colors.border}`,
      }}
    >
      {cells.map((cell) => (
        <span
          key={cell.label}
          style={{ display: "flex", gap: 10, whiteSpace: "nowrap" }}
        >
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
          gap: COLUMN_GAP,
          paddingBottom: 8,
          borderBottom: `1px solid ${colors.border}`,
          color: colors.textDim,
          fontSize: TABLE_FONT,
        }}
      >
        {COLUMNS.map((column) => (
          <Cell key={column.key} width={column.width} align={column.align}>
            {column.label}
          </Cell>
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
              gap: COLUMN_GAP,
              padding: "9px 0",
              fontSize: TABLE_FONT,
              opacity: painted ? 1 : 0,
              backgroundColor: selected && painted ? colors.panelRaised : "transparent",
            }}
          >
            <Cell width={COLUMNS[0].width} color={colors.textMuted}>
              {row.id}
            </Cell>
            <Cell width={COLUMNS[1].width}>{row.source}</Cell>
            <Cell width={COLUMNS[2].width} color={colors.textMuted}>
              {row.destination}
            </Cell>
            <Cell width={COLUMNS[3].width} color={STATE_COLORS[row.state]} bold>
              {row.state}
            </Cell>
            <Cell
              width={COLUMNS[4].width}
              color={colors.textMuted}
              align="right"
            >
              {row.elapsed}
            </Cell>
          </div>
        );
      })}
    </div>
  );
};

const Cell: React.FC<{
  width: number;
  color?: string;
  bold?: boolean;
  /** The real table right-justifies Elapsed. */
  align?: "left" | "right";
  children: React.ReactNode;
}> = ({ width, color, bold = false, align = "left", children }) => {
  return (
    <span
      style={{
        width,
        flexShrink: 0,
        color,
        fontWeight: bold ? 700 : 400,
        textAlign: align,
        whiteSpace: "nowrap",
        overflow: "hidden",
      }}
    >
      {children}
    </span>
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
      <span style={{ color: colors.textDim, fontSize: 19 }}>
        141203Z_k4m2xr · run.log
      </span>
      <div style={{ fontSize: 19, lineHeight: 1.5, color: colors.textMuted }}>
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
      <span
        style={{
          flex: 1,
          minWidth: 0,
          color: colors.textDim,
          fontSize: 19,
          whiteSpace: "nowrap",
          overflow: "hidden",
        }}
      >
        [14:16:04] job launched
      </span>
      {buttons.map((button) => (
        <span
          key={button.label}
          style={{
            padding: "7px 14px",
            borderRadius: 6,
            fontSize: 19,
            whiteSpace: "nowrap",
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
