"""Send anomaly alerts by Gmail, or write them to the GitHub Actions run summary.

Gmail needs an app password (Google Account -> Security -> 2-Step Verification
-> App passwords). Set GMAIL_ADDRESS, GMAIL_APP_PASSWORD and optionally
ALERT_TO (defaults to GMAIL_ADDRESS).

Without Gmail, alerts still show up: in the job log, and on the run's summary
page in GitHub Actions.
"""

import os
import smtplib
from email.message import EmailMessage

from sqlalchemy import text
from sqlalchemy.engine import Engine

from brain.detector import BASELINE_DAYS, RECENT_DAYS, Anomaly, evidence
from core.config import Config


def gmail_configured() -> bool:
    return bool(os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD"))


def describe(a: Anomaly, config: Config, quotes: list[str]) -> str:
    brand = config.brand(a.brand_id)
    if brand.is_client:
        headline = f"PROBLEM: {brand.name} {a.what} are rising"
    else:
        headline = f"OPPORTUNITY: competitor {brand.name} {a.what} are rising"
    lines = [
        headline,
        f"  Last {RECENT_DAYS} days to {a.window_end}: {a.recent_share:.0%} of reviews "
        f"({a.recent_hits} of {a.recent_total})",
        f"  Previous {BASELINE_DAYS} days: {a.baseline_share:.0%} "
        f"({a.baseline_hits} of {a.baseline_total})",
        f"  Confidence: z = {a.z_score} (3+ is very unlikely to be chance)",
    ]
    if quotes:
        lines.append("  What people say:")
        lines += [f'    - "{q[:160]}"' for q in quotes]
    if not brand.is_client:
        lines.append(f"  Idea: target {brand.name}'s unhappy customers on this point.")
    return "\n".join(lines)


def build_message(anomalies: list[Anomaly], engine: Engine, config: Config) -> tuple[str, str]:
    client = config.client.name
    problems = sum(config.brand(a.brand_id).is_client for a in anomalies)
    opportunities = len(anomalies) - problems
    parts = []
    if problems:
        parts.append(f"{problems} problem{'s' * (problems > 1)}")
    if opportunities:
        parts.append(f"{opportunities} opportunit{'ies' if opportunities > 1 else 'y'}")
    subject = f"[{client} consultant] " + " and ".join(parts)
    body = "\n\n".join(describe(a, config, evidence(engine, a)) for a in anomalies)
    body += (
        "\n\nThese are statistical signals, not conclusions. "
        "Check the quotes before acting.\n"
        "-- Brand Consultant"
    )
    return subject, body


def send_gmail(subject: str, body: str) -> None:
    sender = os.environ["GMAIL_ADDRESS"]
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = os.environ.get("ALERT_TO") or sender
    message.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(message)


def write_github_summary(subject: str, body: str) -> bool:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"## {subject}\n\n```\n{body}\n```\n")
    return True


def deliver(subject: str, body: str) -> str:
    """Print, add to the GitHub run summary, and email if Gmail is set up. Returns what happened."""
    print(f"\n{subject}\n\n{body}\n")
    in_summary = write_github_summary(subject, body)
    if not gmail_configured():
        return "logged only (Gmail not configured)" + (" + run summary" if in_summary else "")
    send_gmail(subject, body)
    return "emailed"


def send_alerts(engine: Engine, config: Config, anomalies: list[Anomaly]) -> str:
    """Deliver alerts for new anomalies. Returns how they were delivered."""
    if not anomalies:
        return "nothing to send"
    subject, body = build_message(anomalies, engine, config)
    outcome = deliver(subject, body)
    if outcome != "emailed":
        return outcome
    with engine.begin() as conn:
        for a in anomalies:
            conn.execute(
                text("UPDATE anomalies SET status = 'alerted' WHERE id = :id AND status = 'new'"),
                {"id": a.id},
            )
    return f"emailed {len(anomalies)} alert(s)"


def send_diagnoses(engine: Engine, config: Config, diagnoses: list) -> str:
    """Email finished diagnoses and new questions for the brand team."""
    from brain.diagnosis import format_diagnosis

    ready = [d for d in diagnoses if d.status in ("done", "waiting", "gave_up")]
    if not ready:
        return "nothing to send"
    done = sum(d.status == "done" for d in ready)
    waiting = sum(d.status == "waiting" for d in ready)
    parts = []
    if done:
        parts.append("1 diagnosis" if done == 1 else f"{done} diagnoses")
    if waiting:
        parts.append(f"{waiting} question{'s' * (waiting > 1)} for you")
    if len(ready) - done - waiting:
        parts.append(f"{len(ready) - done - waiting} inconclusive")
    subject = f"[{config.client.name} consultant] " + ", ".join(parts)
    body = "\n\n".join(format_diagnosis(engine, config, d) for d in ready)
    body += "\n\nHypotheses are ranked by the agent's confidence. Quotes are real reviews.\n-- Brand Consultant"
    return deliver(subject, body)
