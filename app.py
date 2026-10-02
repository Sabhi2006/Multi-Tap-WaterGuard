
import re

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="Multi-Tap WaterGuard",
    page_icon="💧",
    layout="wide",
)

# ============================================================
# Firmware-aligned configuration
# ============================================================

FLOW_CHANGE_THRESHOLD_LPM = 0.8
LEAK_DEVIATION_THRESHOLD_LPM = 0.8
WARNING_TIME_S = 20
CRITICAL_TIME_S = 25
DEFAULT_PROTECTION_LIMIT_S = 30

# These are the demonstration values used by the current firmware simulation.
TAP_MAX_FLOW = {
    "A": 5.0,
    "B": 7.0,
    "C": 9.0,
}

# ============================================================
# Robust parser for the current main.cpp serial format
# ============================================================

STATUS_START_RE = re.compile(
    r"^-+\s*MULTI-TAP STATUS\s*-+$",
    re.IGNORECASE,
)

FIELD_PATTERNS = {
    "tap_a": re.compile(
        r"TAP A:\s*(\d+)%\s*\|\s*Pot V=([0-9.]+)\s*V\s*\|\s*V1=([0-9.]+)\s*V\s*\|\s*CMD=([A-Z]+)",
        re.IGNORECASE,
    ),
    "tap_b": re.compile(
        r"TAP B:\s*(\d+)%\s*\|\s*Pot V=([0-9.]+)\s*V\s*\|\s*V2=([0-9.]+)\s*V\s*\|\s*CMD=([A-Z]+)",
        re.IGNORECASE,
    ),
    "tap_c": re.compile(
        r"TAP C:\s*(\d+)%\s*\|\s*Pot V=([0-9.]+)\s*V\s*\|\s*V3=([0-9.]+)\s*V\s*\|\s*CMD=([A-Z]+)",
        re.IGNORECASE,
    ),
    "v_sum": re.compile(r"V1\s*\+\s*V2\s*\+\s*V3\s*=\s*([-0-9.]+)\s*V", re.IGNORECASE),
    "expected": re.compile(r"EXPECTED FLOW:\s*([-0-9.]+)\s*L/min", re.IGNORECASE),
    "measured": re.compile(r"MEASURED FLOW:\s*([-0-9.]+)\s*L/min", re.IGNORECASE),
    "deviation": re.compile(r"FLOW DEVIATION:\s*([-0-9.]+)\s*L/min", re.IGNORECASE),
    "valve": re.compile(r"COMMON VALVE:\s*([A-Z]+)", re.IGNORECASE),
    "alarm": re.compile(r"ALARM:\s*([A-Z]+)", re.IGNORECASE),
    "leak_sim": re.compile(r"LEAK SIM:\s*([A-Z]+)", re.IGNORECASE),
    "timer": re.compile(r"PROTECTION TIMER:\s*(\d+)\s*/\s*(\d+)s", re.IGNORECASE),
    "water": re.compile(r"TOTAL EST\. WATER:\s*([-0-9.]+)\s*L", re.IGNORECASE),
}

EVENT_RULES = [
    ("Auto Shutoff", re.compile(r"AUTO SHUTOFF", re.IGNORECASE)),
    ("Warning", re.compile(r"WARNING:\s*LONG WATER USAGE", re.IGNORECASE)),
    ("Critical", re.compile(r"CRITICAL:\s*APPROACHING AUTO SHUTOFF", re.IGNORECASE)),
    ("Reset", re.compile(r"RESET BUTTON PRESSED", re.IGNORECASE)),
    ("Timer Reset", re.compile(r"TIMER START/RESET", re.IGNORECASE)),
    ("Flow Change", re.compile(r"FLOW CHANGE CONFIRMED", re.IGNORECASE)),
    ("Post-Shutoff Restart", re.compile(r"POST-SHUTOFF RESTART", re.IGNORECASE)),
    ("Leak Simulation", re.compile(r"LEAK SIM:\s*(ON|OFF)", re.IGNORECASE)),
    ("Leak Status", re.compile(r"LEAK STATUS:\s*POSSIBLE LEAK", re.IGNORECASE)),
]

def parse_status_block(lines, sample_no):
    text = "\n".join(lines)

    parsed = {"sample": sample_no}

    for key in ("tap_a", "tap_b", "tap_c"):
        m = FIELD_PATTERNS[key].search(text)
        if not m:
            return None

    ma = FIELD_PATTERNS["tap_a"].search(text)
    mb = FIELD_PATTERNS["tap_b"].search(text)
    mc = FIELD_PATTERNS["tap_c"].search(text)

    parsed.update({
        "tap_a_pct": int(ma.group(1)),
        "tap_a_pot_v": float(ma.group(2)),
        "tap_a_v": float(ma.group(3)),
        "tap_a_cmd": ma.group(4).upper(),

        "tap_b_pct": int(mb.group(1)),
        "tap_b_pot_v": float(mb.group(2)),
        "tap_b_v": float(mb.group(3)),
        "tap_b_cmd": mb.group(4).upper(),

        "tap_c_pct": int(mc.group(1)),
        "tap_c_pot_v": float(mc.group(2)),
        "tap_c_v": float(mc.group(3)),
        "tap_c_cmd": mc.group(4).upper(),
    })

    scalar_map = {
        "v_sum": "v_sum",
        "expected": "expected_flow",
        "measured": "measured_flow",
        "deviation": "flow_deviation",
        "valve": "valve",
        "alarm": "alarm",
        "leak_sim": "leak_sim",
        "water": "water_l",
    }

    for pattern_key, output_key in scalar_map.items():
        m = FIELD_PATTERNS[pattern_key].search(text)
        if not m:
            return None
        value = m.group(1)
        if output_key in {"valve", "alarm", "leak_sim"}:
            parsed[output_key] = value.upper()
        else:
            parsed[output_key] = float(value)

    mt = FIELD_PATTERNS["timer"].search(text)
    if not mt:
        return None

    parsed["timer_s"] = int(mt.group(1))
    parsed["timer_limit_s"] = int(mt.group(2))
    return parsed


def parse_log(raw):
    lines = raw.splitlines()
    blocks = []
    current = None

    for line in lines:
        if STATUS_START_RE.match(line.strip()):
            if current:
                blocks.append(current)
            current = [line]
        elif current is not None:
            # The next dashed separator marks the end of the block.
            if line.strip().startswith("-") and "MULTI-TAP STATUS" not in line.upper():
                blocks.append(current)
                current = None
            else:
                current.append(line)

    if current:
        blocks.append(current)

    records = []
    for n, block in enumerate(blocks, start=1):
        row = parse_status_block(block, n)
        if row:
            records.append(row)

    df = pd.DataFrame(records)

    # Key events from the raw log.
    events = []
    for line_no, line in enumerate(lines, start=1):
        s = line.strip()
        if not s:
            continue

        for name, rule in EVENT_RULES:
            if rule.search(s):
                events.append({
                    "line": line_no,
                    "event": name,
                    "detail": s,
                })
                break

    event_df = pd.DataFrame(events)

    return df, event_df


# ============================================================
# Styling
# ============================================================

st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1.3rem;
        padding-bottom: 2rem;
    }

    .water-card {
        border: 1px solid rgba(128,128,128,.28);
        border-radius: 14px;
        padding: 15px 16px;
        background: rgba(128,128,128,.06);
        min-height: 88px;
    }

    .ok { color: #2e7d32; font-weight: 700; }
    .warn { color: #b26a00; font-weight: 700; }
    .bad { color: #c62828; font-weight: 700; }

    .subtle {
        opacity: .70;
        font-size: .88rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ============================================================
# Header
# ============================================================

st.title("💧 Multi-Tap WaterGuard")
st.caption(
    "ESP32-S3 • 3 potentiometer-based tap controls • 1 common flow measurement • "
    "1 common solenoid valve • automatic shutoff"
)

# ============================================================
# Upload — only user-selected data
# ============================================================

st.sidebar.header("Wokwi Log")

uploaded = st.sidebar.file_uploader(
    "Upload Wokwi Serial Monitor output",
    type=["txt", "log"],
    key="waterguard_log",
    help="Upload the complete text copied from the current WaterGuard main.cpp output.",
)

if uploaded is None:
    st.info("Upload your Wokwi Serial Monitor `.txt` file to populate the dashboard.")
    st.stop()

raw = uploaded.getvalue().decode("utf-8", errors="replace")
st.sidebar.success(f"Loaded: {uploaded.name}")
st.sidebar.caption(f"{len(raw.encode('utf-8')):,} bytes")

required_markers = [
    "MULTI-TAP STATUS",
    "EXPECTED FLOW:",
    "MEASURED FLOW:",
    "PROTECTION TIMER:",
]

missing = [m for m in required_markers if m not in raw]
if missing:
    st.error(
        "This file does not match the current WaterGuard firmware output. "
        f"Missing: {', '.join(missing)}"
    )
    with st.expander("Uploaded file preview"):
        st.code(raw[:5000], language="text")
    st.stop()

df, events = parse_log(raw)

if df.empty:
    st.error(
        "The WaterGuard markers were found, but complete status blocks could not be parsed. "
        "Check that the file contains the current main.cpp Serial Monitor output."
    )
    with st.expander("Uploaded file preview"):
        st.code(raw[:5000], language="text")
    st.stop()

latest = df.iloc[-1]

st.caption(
    f"Showing **{uploaded.name}** • **{len(df)}** complete status samples parsed"
)

# ============================================================
# Derived values aligned with main.cpp
# ============================================================

abnormal = df["flow_deviation"] > LEAK_DEVIATION_THRESHOLD_LPM

auto_shutoffs = (
    int((events["event"] == "Auto Shutoff").sum())
    if not events.empty else 0
)
warnings = (
    int((events["event"] == "Warning").sum())
    if not events.empty else 0
)
criticals = (
    int((events["event"] == "Critical").sum())
    if not events.empty else 0
)
resets = (
    int((events["event"] == "Reset").sum())
    if not events.empty else 0
)

peak_expected = float(df["expected_flow"].max())
peak_measured = float(df["measured_flow"].max())
max_deviation = float(df["flow_deviation"].max())
abnormal_count = int(abnormal.sum())
abnormal_share = abnormal.mean() * 100.0

# Samples where each command is active.
util_a = float((df["tap_a_cmd"] == "ACTIVE").mean() * 100)
util_b = float((df["tap_b_cmd"] == "ACTIVE").mean() * 100)
util_c = float((df["tap_c_cmd"] == "ACTIVE").mean() * 100)

# ============================================================
# KPI cards
# ============================================================

k1, k2, k3, k4, k5 = st.columns(5)

k1.metric("Expected Flow", f"{latest.expected_flow:.2f} L/min")
k2.metric("Measured Flow", f"{latest.measured_flow:.2f} L/min")
k3.metric("Deviation", f"{latest.flow_deviation:+.2f} L/min")
k4.metric(
    "Protection Timer",
    f"{int(latest.timer_s)}/{int(latest.timer_limit_s)} s",
)
k5.metric("Total Est. Water", f"{latest.water_l:.3f} L")

# ============================================================
# Current system state
# ============================================================

st.subheader("Current System State")

s1, s2, s3, s4 = st.columns(4)

valve_class = "ok" if latest.valve == "OPEN" else "bad"
alarm_class = "bad" if latest.alarm == "ACTIVE" else "ok"

if latest.flow_deviation > LEAK_DEVIATION_THRESHOLD_LPM:
    flow_class = "warn"
    flow_label = "POSSIBLE LEAK"
else:
    flow_class = "ok"
    flow_label = "NORMAL"

s1.markdown(
    f'<div class="water-card"><b>Common Solenoid</b><br>'
    f'<span class="{valve_class}">{latest.valve}</span></div>',
    unsafe_allow_html=True,
)

s2.markdown(
    f'<div class="water-card"><b>Alarm</b><br>'
    f'<span class="{alarm_class}">{latest.alarm}</span></div>',
    unsafe_allow_html=True,
)

s3.markdown(
    f'<div class="water-card"><b>Flow Status</b><br>'
    f'<span class="{flow_class}">{flow_label}</span></div>',
    unsafe_allow_html=True,
)

s4.markdown(
    f'<div class="water-card"><b>Leak SIM</b><br>'
    f'<span class="{("warn" if latest.leak_sim == "ON" else "ok")}">{latest.leak_sim}</span></div>',
    unsafe_allow_html=True,
)

# Informational note to avoid misleading physical interpretation.
st.info(
    "Hardware architecture: one common solenoid controls the shared water supply. "
    "The dashboard displays the firmware's logical tap commands; one physical common valve "
    "cannot independently isolate individual plumbing branches."
)

# ============================================================
# Tap status
# ============================================================

st.subheader("Tap Status")

tap_cols = st.columns(3)

for col, tap in zip(tap_cols, ["a", "b", "c"]):
    pct = int(latest[f"tap_{tap}_pct"])
    pot_v = latest[f"tap_{tap}_pot_v"]
    cmd = latest[f"tap_{tap}_cmd"]
    normalized_v = latest[f"tap_{tap}_v"]

    if cmd == "ACTIVE":
        col.success(f"Tap {tap.upper()} — ACTIVE")
    else:
        col.error(f"Tap {tap.upper()} — OFF")

    col.metric("Knob Position", f"{pct}%")
    col.write(f"Potentiometer: **{pot_v:.2f} V**")
    col.write(f"Normalized V{tap.upper()}: **{normalized_v:.2f} V**")
    col.write(f"Command: **{cmd}**")

# ============================================================
# Run summary
# ============================================================

st.subheader("Run Summary")

r1, r2, r3, r4, r5 = st.columns(5)
r1.metric("Auto Shutoffs", auto_shutoffs)
r2.metric("Warnings", warnings)
r3.metric("Critical Alerts", criticals)
r4.metric("Resets", resets)
r5.metric("Peak Measured Flow", f"{peak_measured:.2f} L/min")

# ============================================================
# Flow history
# ============================================================

st.subheader("Flow History")

fig = go.Figure()

fig.add_trace(
    go.Scatter(
        x=df["sample"],
        y=df["expected_flow"],
        mode="lines",
        name="Expected Flow",
    )
)

fig.add_trace(
    go.Scatter(
        x=df["sample"],
        y=df["measured_flow"],
        mode="lines",
        name="Measured Flow",
    )
)

if abnormal.any():
    fig.add_trace(
        go.Scatter(
            x=df.loc[abnormal, "sample"],
            y=df.loc[abnormal, "measured_flow"],
            mode="markers",
            name="Possible Leak",
            marker={"size": 9},
        )
    )

fig.update_layout(
    xaxis_title="Status sample",
    yaxis_title="Flow (L/min)",
    hovermode="x unified",
    height=430,
    margin={"l": 20, "r": 20, "t": 20, "b": 20},
    legend={"orientation": "h"},
)

st.plotly_chart(fig, use_container_width=True)

# ============================================================
# Protection cycle
# ============================================================

st.subheader("Protection Cycle")

fig_p = go.Figure()

fig_p.add_trace(
    go.Scatter(
        x=df["sample"],
        y=df["timer_s"],
        mode="lines+markers",
        name="Protection Timer",
    )
)

fig_p.add_hline(
    y=WARNING_TIME_S,
    line_dash="dash",
    annotation_text="Warning: 20 s",
)

fig_p.add_hline(
    y=CRITICAL_TIME_S,
    line_dash="dash",
    annotation_text="Critical: 25 s",
)

fig_p.update_layout(
    xaxis_title="Status sample",
    yaxis_title="Protection time (s)",
    height=350,
    margin={"l": 20, "r": 20, "t": 20, "b": 20},
)

st.plotly_chart(fig_p, use_container_width=True)

# ============================================================
# Tap opening history
# ============================================================

st.subheader("Tap Opening History")

fig_t = go.Figure()

for tap in ["a", "b", "c"]:
    fig_t.add_trace(
        go.Scatter(
            x=df["sample"],
            y=df[f"tap_{tap}_pct"],
            mode="lines",
            name=f"Tap {tap.upper()}",
        )
    )

fig_t.update_layout(
    xaxis_title="Status sample",
    yaxis_title="Tap opening (%)",
    yaxis={"range": [0, 100]},
    hovermode="x unified",
    height=330,
    margin={"l": 20, "r": 20, "t": 20, "b": 20},
)

st.plotly_chart(fig_t, use_container_width=True)

# ============================================================
# Tap utilization
# ============================================================

st.subheader("Tap Command Utilization")

util_df = pd.DataFrame(
    {
        "Tap": ["Tap A", "Tap B", "Tap C"],
        "Active samples": [
            int((df["tap_a_cmd"] == "ACTIVE").sum()),
            int((df["tap_b_cmd"] == "ACTIVE").sum()),
            int((df["tap_c_cmd"] == "ACTIVE").sum()),
        ],
        "Utilization": [
            f"{util_a:.1f}%",
            f"{util_b:.1f}%",
            f"{util_c:.1f}%",
        ],
    }
)

st.dataframe(util_df, use_container_width=True, hide_index=True)

# ============================================================
# Water consumption
# ============================================================

st.subheader("Estimated Water Usage")

fig_w = go.Figure()

fig_w.add_trace(
    go.Scatter(
        x=df["sample"],
        y=df["water_l"],
        mode="lines",
        name="Total Estimated Water",
    )
)

fig_w.update_layout(
    xaxis_title="Status sample",
    yaxis_title="Water (L)",
    height=320,
    margin={"l": 20, "r": 20, "t": 20, "b": 20},
)

st.plotly_chart(fig_w, use_container_width=True)

# ============================================================
# Leak / anomaly analysis
# ============================================================

st.subheader("Leak & Anomaly Analysis")

a1, a2, a3 = st.columns(3)
a1.metric("Abnormal Samples", abnormal_count)
a2.metric("Abnormal Share", f"{abnormal_share:.1f}%")
a3.metric("Maximum Deviation", f"{max_deviation:+.2f} L/min")

if abnormal.any():
    anomaly_df = df.loc[
        abnormal,
        [
            "sample",
            "expected_flow",
            "measured_flow",
            "flow_deviation",
            "timer_s",
            "leak_sim",
        ],
    ].copy()

    st.dataframe(
        anomaly_df,
        use_container_width=True,
        hide_index=True,
    )
else:
    st.success(
        f"No samples exceeded the {LEAK_DEVIATION_THRESHOLD_LPM:.1f} L/min "
        "deviation threshold."
    )

# ============================================================
# Event timeline
# ============================================================

st.subheader("Key Event Timeline")

if events.empty:
    st.info("No recognized firmware events were found.")
else:
    st.dataframe(
        events,
        use_container_width=True,
        hide_index=True,
    )

# ============================================================
# Latest technical snapshot
# ============================================================

st.subheader("Latest Firmware Status")

snapshot = pd.DataFrame(
    {
        "Parameter": [
            "Tap A position",
            "Tap B position",
            "Tap C position",
            "Tap A command",
            "Tap B command",
            "Tap C command",
            "Pot A voltage",
            "Pot B voltage",
            "Pot C voltage",
            "V1 + V2 + V3",
            "Expected flow",
            "Measured flow",
            "Flow deviation",
            "Common valve",
            "Alarm",
            "Leak SIM",
            "Protection timer",
            "Total estimated water",
        ],
        "Value": [
            f"{int(latest.tap_a_pct)}%",
            f"{int(latest.tap_b_pct)}%",
            f"{int(latest.tap_c_pct)}%",
            latest.tap_a_cmd,
            latest.tap_b_cmd,
            latest.tap_c_cmd,
            f"{latest.tap_a_pot_v:.2f} V",
            f"{latest.tap_b_pot_v:.2f} V",
            f"{latest.tap_c_pot_v:.2f} V",
            f"{latest.v_sum:.2f} V",
            f"{latest.expected_flow:.2f} L/min",
            f"{latest.measured_flow:.2f} L/min",
            f"{latest.flow_deviation:+.2f} L/min",
            latest.valve,
            latest.alarm,
            latest.leak_sim,
            f"{int(latest.timer_s)}/{int(latest.timer_limit_s)} s",
            f"{latest.water_l:.3f} L",
        ],
    }
)

st.dataframe(snapshot, use_container_width=True, hide_index=True)

# ============================================================
# Export
# ============================================================

st.subheader("Export")

e1, e2 = st.columns(2)

with e1:
    st.download_button(
        "Download Parsed Status CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="waterguard_status.csv",
        mime="text/csv",
        key="download_status",
    )

with e2:
    st.download_button(
        "Download Event CSV",
        data=events.to_csv(index=False).encode("utf-8") if not events.empty else b"",
        file_name="waterguard_events.csv",
        mime="text/csv",
        key="download_events",
    )

st.caption(
    "Dashboard aligned to the current WaterGuard firmware output. "
    "Flow figures are simulation/demo values unless replaced by calibrated real-sensor data."
)
