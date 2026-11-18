#!/usr/bin/env python3
"""채용 공고 감시기 (현재: Apple Korea).

jobs.apple.com 의 한국 공고를 가져와 처음 보는 대상 공고마다 Gmail 로 알림 1통을 보내고
seen.json 에 기록한다.

환경 변수:
  GMAIL_USER          보내는 Gmail 주소
  GMAIL_APP_PASSWORD  Gmail 앱 비밀번호 (16자리)
  MAIL_TO             받는 주소 (생략 시 GMAIL_USER, 여러 개는 쉼표)

옵션:
  --dry-run   메일을 보내지 않고 seen.json 도 바꾸지 않음
  --all       인턴뿐 아니라 한국 신규 공고 전체를 알림
"""
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from html import escape
from pathlib import Path

COMPANY = "Apple Korea"
LOCALE = "ko-kr"
LOCATION = "korea-republic-of-KOR"
SEARCH_URL = "https://jobs.apple.com/{locale}/search?location={loc}&page={page}"
DETAIL_URL = "https://jobs.apple.com/{locale}/details/{id}/{slug}"
ALL_JOBS_URL = f"https://jobs.apple.com/{LOCALE}/search?location={LOCATION}"

HERE = Path(__file__).parent
SEEN_FILE = HERE / "seen.json"
LOGO_FILE = HERE / "assets" / "job-alert.png"
SENDER_NAME = "JOB ALERT"
KST = timezone(timedelta(hours=9))

INTERN_TEAM_CODES = {"STDNT"}  # Apple 의 '학생' 팀 (인턴십)
INTERN_PATTERN = re.compile(r"intern|인턴|co-?op|student|학생", re.I)
CONTRACT_PATTERN = re.compile(r"contract|계약", re.I)

HYDRATION_RE = re.compile(r"__staticRouterHydrationData = JSON\.parse\((\".*?\")\);", re.S)

# 메일 색상 (로고의 빨강/검정)
RED, RED_BG, INK, SUB, MUTED, LINE, GRAY_BG = "#d71920", "#fdecec", "#111111", "#555555", "#999999", "#e6e6e6", "#f4f4f5"
FONT = "-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',Arial,sans-serif"


# ───────────────────────── 수집 ─────────────────────────

def fetch(url, tries=3):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    })
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            print(f"fetch 실패 ({e}), 재시도...", file=sys.stderr)
            time.sleep(5 * (i + 1))


def hydration(html):
    m = HYDRATION_RE.search(html)
    if not m:
        raise RuntimeError("페이지 구조가 바뀐 것 같음: hydration 데이터를 찾지 못함")
    return json.loads(json.loads(m.group(1)))["loaderData"]


def fetch_all_jobs():
    jobs, page, total = {}, 1, None
    while True:
        search = hydration(fetch(SEARCH_URL.format(locale=LOCALE, loc=LOCATION, page=page)))["search"]
        results = search["searchResults"]
        if total is None:
            total = int(search.get("totalRecords") or 0)
        if not results:
            break
        for r in results:
            jobs[r["positionId"]] = r
        if len(jobs) >= total or page >= 20:
            break
        page += 1
    if total and not jobs:
        raise RuntimeError(f"공고 {total}건이 있다고 나오지만 하나도 파싱하지 못함")
    return list(jobs.values())


def fetch_details(job):
    """공고 상세(JD). 실패해도 알림은 보내야 하므로 빈 dict 반환."""
    try:
        return hydration(fetch(job_url(job)))["jobDetails"]["jobsData"]
    except Exception as e:  # noqa: BLE001
        print(f"상세 페이지 파싱 실패 ({job['positionId']}): {e}", file=sys.stderr)
        return {}


def is_internship(job):
    team = (job.get("team") or {}).get("teamCode", "")
    return team in INTERN_TEAM_CODES or bool(INTERN_PATTERN.search(job.get("postingTitle", "")))


def job_kind(job):
    if is_internship(job):
        return "인턴"
    if CONTRACT_PATTERN.search(job.get("postingTitle", "")):
        return "계약직"
    return "정규직"


def job_url(job):
    return DETAIL_URL.format(locale=LOCALE, id=job["id"], slug=job.get("transformedPostingTitle", ""))


# ───────────────────────── JD 일정 파싱 ─────────────────────────

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
YEAR = r"(?:,?\s*(20\d{2}))?"
# 각 패턴: (정규식, 그룹 → (월, 일, 일2, 연도))
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
    """JD 에서 마감·기간·전형 일정을 뽑는다."""
    info = {"deadline": None, "deadline_text": None, "period": None, "timeline": []}
    for field in ("description", "responsibilities", "jobSummary"):
        for raw in (detail.get(field) or "").splitlines():
            line = clean_line(raw)
            if not line:
                continue
            dates = find_dates(line, ref)
            if not dates:
                continue
            # 날짜 표현을 지운 나머지 = 일정 라벨
            label = line
            for _, _, (a, b) in reversed(dates):
                label = label[:a] + label[b:]
            label = re.sub(r"^[\s:：,\-–~()]+|[\s:：,\-–~()]+$", "", re.sub(r"\s+", " ", label))

            if info["deadline"] is None and DEADLINE_RE.search(line):
                info["deadline"], info["deadline_text"] = dates[0][0], line
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


def posted_date(job):
    try:
        return datetime.fromisoformat(job["postDateInGMT"][:10]).date()
    except (KeyError, ValueError, TypeError):
        return datetime.now(KST).date()


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
            f'line-height:1.35">{escape(value)}</div>'
            f'</div></td>')


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
    ref = posted_date(job)
    info = analyze(detail, ref)
    kind = job_kind(job)
    title = job["postingTitle"]
    url = escape(job_url(job))
    dl = info["deadline"]

    # 상태 배지
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
    team = (job.get("team") or {}).get("teamName", "")
    status += badge(f"{COMPANY} · {kind}" + (f" · {team}" if team else ""), SUB, GRAY_BG)

    locs = ", ".join(l.get("name", "") for l in job.get("locations", []))
    meta = [locs, f"게시 {ref.year}.{ref.month}.{ref.day}"]
    hours = job.get("standardWeeklyHours")
    if hours:
        meta.append(f"주 {hours}시간")

    # 핵심 정보 4칸
    p = info["period"]
    first_iv = next((t for t in info["timeline"] if INTERVIEW_RE.search(t[2])), None)
    min_q = lines_of(detail.get("minimumQualifications"))
    facts = [
        ("지원 마감", fmt_day(dl, True) if dl else "JD 확인", bool(dl)),
        ("근무 기간", (fmt_range(*p) + f" ({max(1, round(((p[1] - p[0]).days + 1) / 7))}주)") if p else "JD 확인", False),
        ("첫 면접", fmt_range(first_iv[0], first_iv[1]) if first_iv else "JD 확인", False),
        ("자격 핵심", short(min_q[0], 64) if min_q else "JD 확인", False),
    ]
    fact_rows = (f'<tr>{fact_cell(*facts[0])}{fact_cell(*facts[1])}</tr>'
                 f'<tr>{fact_cell(*facts[2])}{fact_cell(*facts[3])}</tr>')

    # 전형 타임라인
    tl_rows = ""
    for start, end, label in info["timeline"]:
        if end < today:
            d_style, l_html = f"color:{MUTED}", f'<span style="color:{MUTED};text-decoration:line-through">{escape(label)}</span>'
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

    # 자격 / 업무 (원문)
    pref_q = lines_of(detail.get("preferredQualifications"))
    quals = ""
    if min_q:
        quals += f'<div style="font-size:12px;color:{SUB};margin:0 0 4px">Minimum</div>' + bullet_list(min_q)
    if pref_q:
        quals += (f'<div style="font-size:12px;color:{SUB};margin:10px 0 4px">Preferred</div>'
                  + bullet_list(pref_q, SUB))
    resp = lines_of(detail.get("responsibilities"))
    if not resp and not quals:
        resp = [short(job.get("jobSummary"), 400)]

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
    <a href="{url}" style="color:{INK};text-decoration:none">{escape(title)}</a></div>
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

<tr><td style="padding:14px 24px 20px;border-top:1px solid {LINE};font-size:12px;color:{MUTED}">
  <a href="{url}" style="color:{MUTED}">원문 JD 보기</a> &nbsp;·&nbsp;
  <a href="{ALL_JOBS_URL}" style="color:{MUTED}">전체 {escape(COMPANY)} 공고</a>
</td></tr>
</table></td></tr></table></body></html>"""

    dl_txt = f"{fmt_day(dl)} 마감" if dl else "마감일 미기재"
    subject = f"[{kind}] {title} ({dl_txt})"
    return subject, html


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
    if LOGO_FILE.exists():
        img = MIMEImage(LOGO_FILE.read_bytes(), "png")
        img.add_header("Content-ID", "<logo>")
        img.add_header("Content-Disposition", "inline", filename="job-alert.png")
        msg.attach(img)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    dry = "--dry-run" in sys.argv
    alert_all = "--all" in sys.argv

    jobs = fetch_all_jobs()
    targets = jobs if alert_all else [j for j in jobs if is_internship(j)]
    seen = load_seen()
    new = [j for j in targets if j["positionId"] not in seen]
    print(f"{COMPANY} 공고 {len(jobs)}건, 대상 {len(targets)}건, 신규 {len(new)}건")

    today = datetime.now(KST).date()
    for j in new:
        subject, html = build_email(j, fetch_details(j), today)
        print(f"  NEW {j['positionId']} → {subject}")
        if dry:
            continue
        send_email(subject, html)
        seen[j["positionId"]] = {
            "title": j["postingTitle"], "posted": j.get("postingDate"),
            "notified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        save_seen(seen)  # 한 통 보낼 때마다 저장 → 중간 실패해도 중복 발송 없음
    if new and not dry:
        print("메일 발송 완료")


if __name__ == "__main__":
    main()
