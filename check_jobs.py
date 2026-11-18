#!/usr/bin/env python3
"""JOB ALERT — 한국 채용 공고 감시기 (Apple, Amazon/AWS, Microsoft, Google).

회사별 수집기(sources.py)로 한국 공고를 가져와, 처음 보는 대상 공고마다 Gmail 알림 1통을 보내고
seen.json 에 기록한다. 한 회사 수집이 실패해도 나머지는 계속 진행하고, 1시간 넘게 실패하면 경고 메일을 1회 보낸다.

환경 변수:
  GMAIL_USER          보내는 Gmail 주소
  GMAIL_APP_PASSWORD  Gmail 앱 비밀번호 (16자리)
  MAIL_TO             받는 주소 (생략 시 GMAIL_USER, 여러 개는 쉼표)

옵션:
  --dry-run           메일을 보내지 않고 상태 파일도 바꾸지 않음
  --kinds=인턴,정규직  알림 받을 고용 형태 (기본: 인턴). --all 은 전체
  --only=apple,google 일부 회사만 실행
  --no-screen         profile.json 기반 지원 불가 공고 거르기를 끔
"""
import json
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from html import escape
from pathlib import Path

from screening import load_profile, screen
from sources import SOURCES

HERE = Path(__file__).parent
SEEN_FILE = HERE / "seen.json"
STATUS_FILE = HERE / "status.json"
SCREENED_FILE = HERE / "screened.json"  # profile 기준 지원 불가로 걸러진 공고 (profile 바꾸면 이 파일 삭제 → 재판정)
LOGO_FILE = HERE / "assets" / "job-alert.png"
SENDER_NAME = "JOB ALERT"
KST = timezone(timedelta(hours=9))
FAIL_ALERT_AFTER = 6  # 10분 간격 × 6 ≈ 1시간 연속 실패 시 경고

RED, RED_BG, INK, SUB, MUTED, LINE, GRAY_BG = "#d71920", "#fdecec", "#111111", "#555555", "#999999", "#e6e6e6", "#f4f4f5"
FONT = "-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',Arial,sans-serif"


# ───────────────────────── JD 일정 파싱 ─────────────────────────

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
YEAR = r"(?:,?\s*(20\d{2}))?"
DATE_PATTERNS = [
    (re.compile(rf"\b{MON}\s+(\d{{1,2}})(?!\d)(?:st|nd|rd|th)?(?:\s*[-–~]\s*(\d{{1,2}})\b)?{YEAR}", re.I),
     lambda g: (MONTHS[g[0][:3].lower()], g[1], g[2], g[3])),
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{MON}{YEAR}", re.I),
     lambda g: (MONTHS[g[1][:3].lower()], g[0], None, g[2])),
    (re.compile(r"(?:(20\d{2})\s*년\s*)?(\d{1,2})\s*월\s*(\d{1,2})\s*일"),
     lambda g: (int(g[1]), g[2], None, g[0])),
    (re.compile(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b"),
     lambda g: (int(g[1]), g[2], None, g[0])),
]
DEADLINE_RE = re.compile(r"deadline|apply by|closing|close[sd]? on|마감", re.I)
INTERVIEW_RE = re.compile(r"interview|면접", re.I)
PERIOD_RE = re.compile(r"timing|period|duration|during|internship|start|기간|근무", re.I)
WEEKDAYS = "월화수목금토일"


def find_dates(text, ref):
    """text 안의 날짜들을 [(start, end, span)] 로. 연도 없으면 ref(게시일) 기준으로 추정."""
    found, taken = [], []
    for rx, conv in DATE_PATTERNS:
        for m in rx.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            try:
                mon, d1, d2, yr = conv(m.groups())
                y = int(yr) if yr else ref.year
                start = date(y, mon, int(d1))
                if not yr and start < ref - timedelta(days=60):
                    start = date(y + 1, mon, int(d1))
                end = date(start.year, mon, int(d2)) if d2 else start
            except (ValueError, KeyError, TypeError):
                continue
            found.append((start, end, m.span()))
            taken.append(m.span())
    return sorted(found, key=lambda x: x[2][0])


def clean_line(line):
    return re.sub(r"\s+", " ", line.strip().lstrip("•·-*–●▪ ").strip())


def fmt_day(d, weekday=False):
    s = f"{d.month}.{d.day}"
    return f"{s} ({WEEKDAYS[d.weekday()]})" if weekday else s


def fmt_range(a, b):
    if a == b:
        return fmt_day(a)
    if a.month == b.month:
        return f"{a.month}.{a.day}–{b.day}"
    return f"{fmt_day(a)} – {fmt_day(b)}"


def analyze(detail, ref):
    """JD 에서 마감·근무 기간·전형 일정을 뽑는다."""
    info = {"deadline": None, "period": None, "timeline": []}
    for fld in ("description", "responsibilities"):
        for raw in (detail.get(fld) or "").splitlines():
            line = clean_line(raw)
            if not line or len(line) > 300:
                continue
            dates = find_dates(line, ref)
            if not dates:
                continue
            label = line
            for _, _, (a, b) in reversed(dates):
                label = label[:a] + label[b:]
            label = re.sub(r"^[\s:：,\-–~()]+|[\s:：,\-–~()]+$", "", re.sub(r"\s+", " ", label))

            if info["deadline"] is None and DEADLINE_RE.search(line):
                info["deadline"] = dates[0][0]
            if info["period"] is None and len(dates) >= 2 and PERIOD_RE.search(line):
                info["period"] = (dates[0][0], dates[-1][1])
                continue
            if len(label) <= 60 and len(dates) == 1:
                start, end, _ = dates[0]
                if not any(t[2] == label for t in info["timeline"]):
                    info["timeline"].append((start, end, label or line))
    if info["period"]:
        info["timeline"].append((info["period"][0], info["period"][0], "Internship / role starts"))
    info["timeline"].sort(key=lambda t: t[0])
    return info


def lines_of(text):
    return [clean_line(l) for l in (text or "").splitlines() if clean_line(l)]


def short(text, n):
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= n else text[:n].rstrip() + "…"


# ───────────────────────── 메일 HTML (Gmail 호환: table + inline CSS) ─────────────────────────

def badge(text, fg, bg):
    return (f'<span style="display:inline-block;background:{bg};color:{fg};font-size:12px;font-weight:600;'
            f'padding:3px 10px;border-radius:6px;margin:0 4px 4px 0">{escape(text)}</span>')


def fact_cell(label, value, alert=False):
    fg, bg = (RED, RED_BG) if alert else (INK, GRAY_BG)
    lab = RED if alert else SUB
    return (f'<td width="50%" valign="top" style="padding:4px">'
            f'<div style="background:{bg};border-radius:8px;padding:10px 12px">'
            f'<div style="font-size:12px;color:{lab}">{escape(label)}</div>'
            f'<div style="font-size:{15 if len(value) <= 20 else 13}px;font-weight:600;color:{fg};margin-top:2px;'
            f'line-height:1.35">{escape(value)}</div></div></td>')


def section(icon, title, inner):
    return (f'<tr><td style="padding:16px 24px;border-top:1px solid {LINE}">'
            f'<div style="font-size:13px;font-weight:600;color:{SUB};margin-bottom:8px">{icon} {escape(title)}</div>'
            f'{inner}</td></tr>')


def bullet_list(items, color=INK, limit=6):
    if not items:
        return ""
    lis = "".join(f'<li style="margin:0 0 4px">{escape(short(i, 220))}</li>' for i in items[:limit])
    return f'<ul style="margin:0;padding-left:18px;font-size:14px;line-height:1.55;color:{color}">{lis}</ul>'


def build_email(job, detail, today):
    ref = job.posted or today
    info = analyze(detail, ref)
    url = escape(job.url)
    dl = info["deadline"]

    if dl:
        diff = (dl - today).days
        if diff < 0:
            status = badge(f"마감 D+{-diff} · 지원 서둘러야 함", "#ffffff", RED)
        elif diff == 0:
            status = badge("오늘 마감 · D-day", "#ffffff", RED)
        elif diff <= 3:
            status = badge(f"마감 D-{diff} · 임박", "#7a4a00", "#fff1cc")
        else:
            status = badge(f"마감 D-{diff}", "#1b5e20", "#e3f4e4")
    else:
        status = badge("마감일 미기재 · 빠른 지원 권장", "#7a4a00", "#fff1cc")
    status += badge(f"{job.company} · {job.kind}" + (f" · {short(job.team, 30)}" if job.team else ""), SUB, GRAY_BG)

    meta = [short(job.location, 60)]
    if job.posted:
        meta.append(f"게시 {job.posted.year}.{job.posted.month}.{job.posted.day}")
    if job.hours:
        meta.append(f"주 {job.hours}시간")

    p = info["period"]
    first_iv = next((t for t in info["timeline"] if INTERVIEW_RE.search(t[2])), None)
    min_q = lines_of(detail.get("minimumQualifications"))
    facts = [
        ("지원 마감", fmt_day(dl, True) if dl else "JD 확인", bool(dl)),
        ("근무 기간", fmt_range(*p) + f" ({max(1, round(((p[1] - p[0]).days + 1) / 7))}주)", False) if p
        else ("고용 형태", job.kind, False),
        ("첫 면접", fmt_range(first_iv[0], first_iv[1]), False) if first_iv
        else ("근무지", short(job.location, 40) or "JD 확인", False),
        ("자격 핵심", short(min_q[0], 64) if min_q else "JD 확인", False),
    ]
    fact_rows = (f'<tr>{fact_cell(*facts[0])}{fact_cell(*facts[1])}</tr>'
                 f'<tr>{fact_cell(*facts[2])}{fact_cell(*facts[3])}</tr>')

    tl_rows = ""
    for start, end, label in info["timeline"]:
        if end < today:
            d_style = f"color:{MUTED}"
            l_html = f'<span style="color:{MUTED};text-decoration:line-through">{escape(label)}</span>'
        elif start <= today <= end:
            d_style = f"color:{RED};font-weight:600"
            l_html = f'<b style="color:{INK}">{escape(label)}</b> <span style="font-size:12px;color:{RED}">· 진행 중</span>'
        else:
            d_style, l_html = f"color:{INK}", f'<span style="color:{INK}">{escape(label)}</span>'
        tl_rows += (f'<tr><td width="84" valign="top" style="padding:4px 0;font-size:14px;{d_style}">'
                    f'{escape(fmt_range(start, end))}</td>'
                    f'<td valign="top" style="padding:4px 0;font-size:14px">{l_html}</td></tr>')
    timeline = section("📅", "전형 일정",
                       f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{tl_rows}</table>'
                       ) if tl_rows else ""

    pref_q = lines_of(detail.get("preferredQualifications"))
    quals = ""
    if min_q:
        quals += f'<div style="font-size:12px;color:{SUB};margin:0 0 4px">Minimum</div>' + bullet_list(min_q)
    if pref_q:
        quals += (f'<div style="font-size:12px;color:{SUB};margin:10px 0 4px">Preferred</div>'
                  + bullet_list(pref_q, SUB))
    resp = lines_of(detail.get("responsibilities"))
    about = "" if resp else short(detail.get("description"), 500)

    logo = ('<img src="cid:logo" width="40" height="40" alt="JOB ALERT" '
            'style="display:block;border:0;border-radius:50%">') if LOGO_FILE.exists() else ""

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;padding:0;background:#f2f2f2">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f2f2"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:12px;font-family:{FONT};color:{INK}">

<tr><td style="padding:20px 24px 4px">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%"><tr>
    <td valign="top">{status}</td>
    <td width="40" valign="top" align="right">{logo}</td>
  </tr></table>
  <div style="font-size:21px;font-weight:700;line-height:1.3;margin:8px 0 4px">
    <a href="{url}" style="color:{INK};text-decoration:none">{escape(job.title)}</a></div>
  <div style="font-size:13px;color:{SUB}">{escape(" · ".join(m for m in meta if m))}</div>
</td></tr>

<tr><td style="padding:12px 20px 4px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{fact_rows}</table>
</td></tr>

<tr><td style="padding:10px 24px 20px">
  <a href="{url}" style="display:block;background:{INK};color:#ffffff;text-align:center;padding:13px;border-radius:8px;
     font-size:15px;font-weight:600;text-decoration:none">지원하러 가기 →</a>
</td></tr>

{timeline}
{section("✅", "지원 자격", quals) if quals else ""}
{section("💼", "주요 업무", bullet_list(resp, SUB)) if resp else ""}
{section("📝", "About", f'<p style="margin:0;font-size:14px;line-height:1.55;color:{SUB}">{escape(about)}</p>') if about else ""}

<tr><td style="padding:14px 24px 20px;border-top:1px solid {LINE};font-size:12px;color:{MUTED}">
  <a href="{url}" style="color:{MUTED}">원문 JD 보기</a> &nbsp;·&nbsp;
  <a href="{escape(job.all_url)}" style="color:{MUTED}">전체 {escape(job.company)} 공고</a>
</td></tr>
</table></td></tr></table></body></html>"""

    dl_txt = f"{fmt_day(dl)} 마감" if dl else "마감일 미기재"
    return f"[{job.kind}] {job.title} ({dl_txt})", html


def build_failure_email(source, count, error, recovered=False):
    if recovered:
        subject = f"[JOB ALERT] {source} 공고 수집 복구됨"
        body = f"{source} 공고 수집이 다시 정상 동작합니다."
    else:
        subject = f"[JOB ALERT] {source} 공고 수집 실패 중"
        body = (f"{source} 공고를 {count}회 연속(약 {count * 10}분) 가져오지 못했습니다.<br>"
                f"사이트 구조가 바뀌었을 수 있어 수집 코드 확인이 필요합니다.<br><br>"
                f"<code>{escape(short(str(error), 500))}</code>")
    return subject, f'<div style="font-family:{FONT};font-size:14px;color:{INK}">{body}</div>'


# ───────────────────────── 발송 / 상태 ─────────────────────────

def send_email(subject, html):
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    to = os.environ.get("MAIL_TO") or user

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = formataddr((str(Header(SENDER_NAME, "utf-8")), user))
    msg["To"], msg["Date"] = to, formatdate(localtime=True)
    msg.attach(MIMEText(html, "html", "utf-8"))
    if LOGO_FILE.exists() and "cid:logo" in html:
        img = MIMEImage(LOGO_FILE.read_bytes(), "png")
        img.add_header("Content-ID", "<logo>")
        img.add_header("Content-Disposition", "inline", filename="job-alert.png")
        msg.attach(img)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_seen():
    seen = load_json(SEEN_FILE)
    # 예전 형식(Apple 전용, 접두어 없음) → "apple:<id>"
    return {(k if ":" in k else f"apple:{k}"): v for k, v in seen.items()}


def arg(name, default=None):
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return default


def main():
    dry = "--dry-run" in sys.argv
    kinds = None if "--all" in sys.argv else set(arg("kinds", "인턴").split(","))
    only = arg("only")
    names = [n for n in SOURCES if not only or n in only.split(",")]

    no_screen = "--no-screen" in sys.argv
    seen, status, screened = load_seen(), load_json(STATUS_FILE), load_json(SCREENED_FILE)
    profile = load_profile()
    today = datetime.now(KST).date()
    seen_dirty = status_dirty = screened_dirty = False

    for name in names:
        try:
            jobs = SOURCES[name]()
        except Exception as e:  # noqa: BLE001  한 회사가 실패해도 나머지는 진행
            st = status.setdefault(name, {"fails": 0, "alerted": False})
            st["fails"] += 1
            status_dirty = True
            print(f"[{name}] 수집 실패 ({st['fails']}회 연속): {e}", file=sys.stderr)
            if st["fails"] >= FAIL_ALERT_AFTER and not st["alerted"] and not dry:
                send_email(*build_failure_email(name, st["fails"], e))
                st["alerted"] = True
            continue

        st = status.get(name)
        if st and st.get("fails"):
            if st.get("alerted") and not dry:
                send_email(*build_failure_email(name, 0, None, recovered=True))
            status[name] = {"fails": 0, "alerted": False}
            status_dirty = True

        targets = [j for j in jobs if kinds is None or j.kind in kinds]
        new = [j for j in targets if j.key not in seen and j.key not in screened]
        print(f"[{name}] 공고 {len(jobs)}건, 대상 {len(targets)}건, 신규 {len(new)}건")
        for j in new:
            detail = j.detail()
            reasons = [] if no_screen else screen(j, detail, profile)
            if reasons:
                print(f"  SKIP {j.key} {j.title} — {', '.join(reasons)}")
                if not dry:
                    screened[j.key] = {"title": j.title, "company": j.company, "kind": j.kind,
                                       "reasons": reasons, "url": j.url,
                                       "screened": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                    screened_dirty = True
                continue
            subject, html = build_email(j, detail, today)
            print(f"  NEW {j.key} → {subject}")
            if dry:
                continue
            send_email(subject, html)
            seen[j.key] = {"title": j.title, "company": j.company, "kind": j.kind,
                           "posted": j.posted.isoformat() if j.posted else None,
                           "notified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            save_json(SEEN_FILE, seen)  # 한 통마다 저장 → 중간에 실패해도 중복 발송 없음
            seen_dirty = True

    if not dry:
        if not seen_dirty and any(":" not in k for k in load_json(SEEN_FILE)):
            save_json(SEEN_FILE, seen)  # 키 형식 마이그레이션
        if status_dirty:
            save_json(STATUS_FILE, status)
        if screened_dirty:
            save_json(SCREENED_FILE, screened)


if __name__ == "__main__":
    main()
