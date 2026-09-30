"""Streamlit dashboard: the consultant's client-facing view.

Run locally:   streamlit run outputs/dashboard.py
Deploy free:   share.streamlit.io -> New app -> this repo, main file outputs/dashboard.py.
               Add DATABASE_URL and DASHBOARD_PASSWORD under the app's Settings -> Secrets.

A deployed Streamlit app is public, and this one can write answers to the database,
so it refuses to open against a cloud database unless DASHBOARD_PASSWORD is set.
"""

import hmac
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "core" imports work on Streamlit Cloud

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import text

from brain import consultant, diagnosis, teardown
from brain.perception import MIN_SAMPLE, build_radar
from core import db
from core.config import load_config

SECRETS = ("DATABASE_URL", "DASHBOARD_PASSWORD", "GEMINI_API_KEY", "GROQ_API_KEY")

# Diverging palette for net sentiment: red (bad) <- gray (neutral) -> blue (good).
NEGATIVE, POSITIVE = "#e34948", "#2a78d6"
NEUTRAL = {"light": "#f0efec", "dark": "#383835"}
SURFACE = {"light": "#ffffff", "dark": "#0e1117"}  # Streamlit's page backgrounds: the 2px gap between cells
INK = {"light": "#0b0b0b", "dark": "#ffffff"}

st.set_page_config(page_title="Brand Consultant", page_icon=":bar_chart:", layout="wide")


def load_secrets() -> None:
    """Streamlit Cloud keeps secrets in st.secrets; the rest of the code reads os.environ."""
    try:
        for key in SECRETS:
            if key in st.secrets and not os.environ.get(key):
                os.environ[key] = str(st.secrets[key])
    except FileNotFoundError:  # no secrets file when running locally
        pass


def check_password() -> bool:
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        if db.database_url().startswith("sqlite"):
            return True  # local file, local user
        st.error("Set DASHBOARD_PASSWORD in the app's secrets before connecting to a cloud database.")
        return False
    if st.session_state.get("authenticated"):
        return True
    entered = st.text_input("Password", type="password")
    if entered and hmac.compare_digest(entered, password):
        st.session_state["authenticated"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password.")
    return False


@st.cache_resource
def engine():
    e = db.get_engine()
    db.init_db(e)
    return e


def theme() -> str:
    try:
        return st.context.theme.type or "light"
    except AttributeError:
        return "light"


# ---- pages -------------------------------------------------------------------------------

def page_overview(config, days: int) -> None:
    radar = build_radar(engine(), config, days)
    st.caption(f"Reviews since {radar.since}. Net sentiment runs from -1 (all negative) to +1 (all positive).")

    cols = st.columns(len(radar.brands))
    for col, b in zip(cols, radar.brands.values()):
        label = f"{b.name} (client)" if b.is_client else b.name
        col.metric(label, f"{b.net:+.2f}", help="Net sentiment")
        col.caption(f"{b.mentions} reviews · {b.share_of_voice:.0%} share of voice"
                    + (f" · {b.avg_rating:.1f}★" if b.avg_rating is not None else ""))

    rows = [
        {"Brand": b.name, "Issue": issue, "Net sentiment": round(s.net, 2), "Reviews": s.mentions,
         "Positive": s.positive, "Negative": s.negative,
         "Label": f"{s.net:+.2f}" + ("*" if s.low_data else "")}
        for b in radar.brands.values() for issue, s in b.issues.items()
    ]
    if not rows:
        st.info("No tagged reviews yet. Import a CSV and run the daily job.")
        return

    st.subheader("Issues by brand")
    df = pd.DataFrame(rows)
    order = [i for i in config.issues if i in set(df["Issue"])]
    base = alt.Chart(df).encode(
        x=alt.X("Brand:N", sort=[b.name for b in radar.brands.values()], title=None,
                axis=alt.Axis(orient="top", labelAngle=0)),
        y=alt.Y("Issue:N", sort=order, title=None),
    )
    mode = theme()
    cells = base.mark_rect(cornerRadius=4, stroke=SURFACE[mode], strokeWidth=2).encode(
        # Interpolate in Lab so blue -> gray -> red stays on a straight path (the default
        # HCL interpolation bends through purple and tan).
        color=alt.Color("Net sentiment:Q", title="Net sentiment",
                        scale=alt.Scale(domain=[-1, 0, 1], range=[NEGATIVE, NEUTRAL[mode], POSITIVE],
                                        interpolate="lab")),
        tooltip=["Brand", "Issue", "Net sentiment", "Reviews", "Positive", "Negative"],
    )
    strong = "abs(datum['Net sentiment']) >= 0.5"
    labels = base.mark_text(fontSize=12).encode(
        text="Label:N",
        color=alt.condition(strong, alt.value("#ffffff"), alt.value(INK[mode])),
    )
    chart = (cells + labels).properties(height=max(160, 34 * len(order))).configure_view(strokeWidth=0)
    st.altair_chart(chart, use_container_width=True)
    st.caption(f"\\* fewer than {MIN_SAMPLE} reviews: a hint, not a finding.")
    with st.expander("Table view"):
        st.dataframe(df.drop(columns="Label"), hide_index=True, use_container_width=True)

    st.subheader(f"{config.client.name} vs competitors")
    left, right = st.columns(2)
    with left:
        st.markdown("**Strengths**")
        for g in reversed(radar.strengths):
            st.markdown(f"- **{g.issue}**: {g.client_net:+.2f} vs {g.competitor_net:+.2f}")
        if not radar.strengths:
            st.caption("None with enough data.")
    with right:
        st.markdown("**Weaknesses**")
        for g in radar.weaknesses:
            st.markdown(f"- **{g.issue}**: {g.client_net:+.2f} vs {g.competitor_net:+.2f}")
        if not radar.weaknesses:
            st.caption("None with enough data.")


def page_alerts(config) -> None:
    with engine().connect() as conn:
        anomalies = pd.DataFrame(conn.execute(text(
            "SELECT a.id, a.brand_id, a.kind, a.issue, a.window_end, a.recent_hits, a.recent_total, "
            "a.baseline_hits, a.baseline_total, a.z_score, a.status, d.id AS diagnosis_id "
            "FROM anomalies a LEFT JOIN diagnoses d ON d.anomaly_id = a.id "
            "ORDER BY a.created_at DESC LIMIT 50")).mappings().all())
    if anomalies.empty:
        st.info("No alerts yet. The detector needs a few weeks of tagged reviews.")
        return
    names = {b.id: b.name for b in config.brands}
    for _, a in anomalies.iterrows():
        role = "Problem" if a.brand_id == config.client.id else "Opportunity"
        what = f"{a.issue} complaints" if a.kind == "complaint_spike" else "1-2 star reviews"
        before = a.baseline_hits / a.baseline_total if a.baseline_total else 0
        title = (f"{role}: {names.get(a.brand_id, a.brand_id)} {what}, "
                 f"{before:.0%} → {a.recent_hits / a.recent_total:.0%} (week to {a.window_end}, {a.status})")
        with st.expander(title):
            if a.diagnosis_id:
                d = diagnosis.load_diagnosis(engine(), a.diagnosis_id)
                st.code(diagnosis.format_diagnosis(engine(), config, d), language=None, wrap_lines=True)
                with st.popover("How the agent reasoned"):
                    for turn in d.transcript:
                        st.json(turn, expanded=False)
            else:
                st.caption("Not investigated yet. The daily job investigates up to 3 alerts a day.")


def page_requests() -> None:
    pending = consultant.open_requests(engine())
    if not pending:
        st.info("No questions waiting for you.")
        return
    for r in pending:
        with st.form(r["id"]):
            st.markdown(f"**{r['question']}**")
            st.caption(f"Why it matters: {r['why']}")
            answer = st.text_area("Your answer")
            upload = st.file_uploader("Or attach a CSV / text file", type=["csv", "txt"])
            if st.form_submit_button("Send answer"):
                body = answer
                if upload is not None:
                    body = (body + "\n\n" if body else "") + f"File {upload.name}:\n" + upload.getvalue().decode("utf-8", "replace")
                try:
                    consultant.answer(engine(), r["id"], body)
                    st.success("Saved. The investigation resumes on the next daily run.")
                except ValueError as exc:
                    st.error(str(exc))


def page_teardown(config) -> None:
    with engine().connect() as conn:
        row = conn.execute(text("SELECT content, created_at FROM reports WHERE kind = 'teardown' "
                                "ORDER BY created_at DESC LIMIT 1")).first()
    if row:
        st.caption(f"Generated {row[1][:16].replace('T', ' ')} UTC by the weekly job.")
        st.markdown(teardown.format_teardown(engine(), config, json.loads(row[0])).split("\n", 1)[1])
    else:
        st.info("No teardown yet. Run `python -m brain.teardown`, or wait for the weekly job.")
        st.markdown(teardown.format_teardown(engine(), config,
                                             teardown.build_teardown(engine(), config, None)).split("\n", 1)[1])


def page_reviews(config, days: int) -> None:
    c1, c2, c3 = st.columns(3)
    brand = c1.selectbox("Brand", [b.id for b in config.brands],
                         format_func=lambda i: config.brand(i).name)
    issue = c2.selectbox("Issue", ["(any)"] + config.issues)
    sentiment = c3.selectbox("Sentiment", ["(any)", "negative", "positive", "neutral"])
    params = {"brand": brand, "yes": True}
    sql = ("SELECT COALESCE(m.posted_at, m.collected_at) AS date, m.source, m.product, m.rating, "
           "t.overall_sentiment AS overall, m.text FROM mentions m "
           "JOIN mention_tags t ON t.mention_id = m.id ")
    where = ["m.brand_id = :brand", "t.status = 'ok'", "t.relevant = :yes"]
    if issue != "(any)":
        sql += "JOIN aspect_tags a ON a.mention_id = m.id "
        where.append("a.issue = :issue")
        params["issue"] = issue
        if sentiment != "(any)":
            where.append("a.sentiment = :sentiment")
            params["sentiment"] = sentiment
    sql += "WHERE " + " AND ".join(where) + " ORDER BY 1 DESC LIMIT 200"
    with engine().connect() as conn:
        df = pd.DataFrame(conn.execute(text(sql), params).mappings().all())
    st.caption(f"{len(df)} reviews (latest 200)")
    if not df.empty:
        df["date"] = df["date"].str[:10]
        st.dataframe(df, hide_index=True, use_container_width=True,
                     column_config={"text": st.column_config.TextColumn(width="large")})


def page_health() -> None:
    with engine().connect() as conn:
        runs = pd.DataFrame(conn.execute(text(
            "SELECT started_at, job, status, stats, errors FROM runs ORDER BY started_at DESC LIMIT 30"
        )).mappings().all())
        reports = conn.execute(text("SELECT content, created_at FROM reports WHERE kind = 'weekly' "
                                    "ORDER BY created_at DESC LIMIT 1")).first()
    if runs.empty:
        st.info("No runs yet.")
    else:
        failed = runs[runs["status"].isin(["failed", "partial"])]
        st.metric("Runs with problems (last 30)", len(failed))
        st.dataframe(runs, hide_index=True, use_container_width=True)
    if reports:
        with st.expander(f"Latest weekly report ({reports[1][:10]})"):
            st.markdown(reports[0])


def main() -> None:
    load_secrets()
    if not check_password():
        return
    config = load_config()
    st.sidebar.title("Brand Consultant")
    st.sidebar.caption(f"{config.client.name} · {config.category}")
    pages = {
        "Perception Radar": lambda: page_overview(config, days),
        "Alerts & diagnoses": lambda: page_alerts(config),
        f"Questions for you ({len(consultant.open_requests(engine()))})": page_requests,
        "Competitor teardown": lambda: page_teardown(config),
        "Reviews": lambda: page_reviews(config, days),
        "Pipeline health": page_health,
    }
    choice = st.sidebar.radio("View", list(pages))
    days = st.sidebar.select_slider("Window (days)", options=[30, 60, 90, 180, 365], value=90)
    st.title(choice.split(" (")[0])
    pages[choice]()


main()
