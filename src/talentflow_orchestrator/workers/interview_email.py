"""TalentFlow interview mail presentation and portable calendar export.

Calendar links only pre-fill a candidate's calendar; saving remains the recipient's choice.
The .ics attachment lets calendar clients import the same event without an OAuth grant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from urllib.parse import urlencode
from uuid import UUID

from talentflow_orchestrator.domain.models import JobKind


def google_calendar_url(
    *, position: str, company: str, start: datetime, end: datetime, timezone: str
) -> str:
    utc_start = start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    utc_end = end.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return "https://calendar.google.com/calendar/render?" + urlencode(
        {
            "action": "TEMPLATE",
            "text": f"{company} — {position} interview",
            "dates": f"{utc_start}/{utc_end}",
            "details": (
                "TalentFlow AI interview. Open the TalentFlow AI mobile app "
                "shortly before your scheduled time to join the interview."
            ),
            "ctz": timezone,
        }
    )


def calendar_ics(
    *,
    interview_id: UUID,
    version: int,
    company: str,
    position: str,
    start: datetime,
    end: datetime,
    cancelled: bool = False,
) -> str:
    def safe(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("\r", "")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;")
        )

    method = "CANCEL" if cancelled else "PUBLISH"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TalentFlow AI//Interview Reminders//EN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:talentflow-interview-{interview_id}@talant-flow.app",
        f"SEQUENCE:{version}",
        f"DTSTAMP:{datetime.now(UTC):%Y%m%dT%H%M%SZ}",
        f"DTSTART:{start.astimezone(UTC):%Y%m%dT%H%M%SZ}",
        f"DTEND:{end.astimezone(UTC):%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{safe(company + ' — ' + position + ' interview')}",
        "DESCRIPTION:Open TalentFlow AI mobile app to join your interview.",
        f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def render_email_html(
    *,
    candidate: str,
    company: str,
    position: str,
    start: datetime,
    timezone: str,
    kind: JobKind,
    calendar_url: str | None,
) -> str:
    headings = {
        JobKind.SEND_INVITATION: "Your interview has been scheduled",
        JobKind.SEND_RESCHEDULE: "Your interview has been rescheduled",
        JobKind.SEND_CANCELLATION: "Your interview has been cancelled",
    }
    if kind not in headings:
        raise ValueError("Unsupported interview email type")
    heading = headings[kind]
    when = f"{start:%B} {start.day}, {start:%Y} · {start:%I:%M %p}"
    intro = (
        f"Hi {escape(candidate)}, <strong>{escape(company)}</strong> "
        + (
            "has cancelled your interview."
            if kind == JobKind.SEND_CANCELLATION
            else "has rescheduled your interview."
            if kind == JobKind.SEND_RESCHEDULE
            else "has scheduled an interview with you."
        )
    )
    reminder = (
        "This interview has been cancelled. You do not need to join at the previously scheduled time."
        if kind == JobKind.SEND_CANCELLATION
        else "Please open the TalentFlow AI mobile app before the scheduled time and make sure "
        "your internet connection, microphone, and camera are ready."
    )
    cta = (
        f'<div style="margin-top:22px;text-align:center;">'
        f'<a href="{escape(calendar_url, quote=True)}" '
        'style="display:inline-block;padding:13px 22px;background:#246bfd;color:#ffffff;'
        'text-decoration:none;border-radius:12px;font-size:14px;font-weight:700;">'
        'Add to Google Calendar</a></div>'
        '<div style="margin-top:10px;text-align:center;font-size:12px;color:#64748b;">'
        'For Apple Calendar or Outlook, open the attached .ics file. '
        'On Google Calendar, press Save to add the interview.</div>'
        if calendar_url
        else ""
    )
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f6f8fc;font-family:Arial,Helvetica,sans-serif;color:#0f172a;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f6f8fc;">
<tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:620px;background:#ffffff;border-radius:20px;overflow:hidden;border:1px solid #e2e8f0;box-shadow:0 12px 32px rgba(15,23,42,.08);">
<tr><td style="padding:0;"><div style="height:7px;background:linear-gradient(90deg,#246bfd,#5b8cff,#06b6d4);"></div></td></tr>
<tr><td style="padding:30px 32px 16px 32px;"><div style="font-size:22px;font-weight:800;color:#0f172a;">TalentFlow AI</div><div style="margin-top:5px;font-size:12px;color:#64748b;">AI-powered interview experience</div></td></tr>
<tr><td style="padding:0 32px 32px 32px;">
<h1 style="margin:0 0 10px 0;font-size:24px;line-height:1.25;color:#0f172a;">{heading}</h1>
<p style="margin:0 0 22px 0;font-size:14px;line-height:1.7;color:#475569;">{intro}</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;">
<tr><td style="padding:18px 20px;border-bottom:1px solid #e2e8f0;"><div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:.6px;">Position</div><div style="margin-top:5px;font-size:16px;font-weight:700;color:#0f172a;">{escape(position)}</div></td></tr>
<tr><td style="padding:18px 20px;border-bottom:1px solid #e2e8f0;"><div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:.6px;">Company</div><div style="margin-top:5px;font-size:15px;font-weight:700;color:#0f172a;">{escape(company)}</div></td></tr>
<tr><td style="padding:18px 20px;"><div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:.6px;">Date &amp; time</div><div style="margin-top:5px;font-size:15px;font-weight:700;color:#0f172a;">{escape(when)}</div><div style="margin-top:4px;font-size:11px;color:#64748b;">{escape(timezone)}</div></td></tr>
</table>
<div style="margin-top:20px;padding:14px 16px;border-radius:14px;background:#eff6ff;color:#1e40af;font-size:13px;line-height:1.6;">{reminder}</div>
{cta}
</td></tr></table>
<div style="max-width:620px;padding:16px 8px 0 8px;font-size:11px;line-height:1.6;color:#94a3b8;text-align:center;">This is an automated TalentFlow AI message related to your interview.</div>
</td></tr></table></body></html>'''
