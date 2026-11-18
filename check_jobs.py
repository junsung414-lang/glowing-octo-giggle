#!/usr/bin/env python3
"""Apple Korea 인턴 공고 감시기.

jobs.apple.com 의 한국 공고를 모두 가져와서 인턴 공고 중 처음 보는 것이 있으면
Gmail 로 알림을 보내고 seen.json 에 기록한다.

환경 변수:
  GMAIL_USER          보내는 Gmail 주소
  GMAIL_APP_PASSWORD  Gmail 앱 비밀번호 (16자리)
  MAIL_TO             받는 주소 (생략 시 GMAIL_USER)

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
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from html import escape
from pathlib import Path

LOCALE = "ko-kr"
LOCATION = "korea-republic-of-KOR"
SEARCH_URL = "https://jobs.apple.com/{locale}/search?location={loc}&page={page}"
DETAIL_URL = "https://jobs.apple.com/{locale}/details/{id}/{slug}"
SEEN_FILE = Path(__file__).with_name("seen.json")
SENDER_NAME = "Internship Alerts"

INTERN_TEAM_CODES = {"STDNT"}  # Apple 의 '학생' 팀 (인턴십)
INTERN_PATTERN = re.compile(r"intern|인턴|co-?op|student|학생", re.I)

HYDRATION_RE = re.compile(r"__staticRouterHydrationData = JSON\.parse\((\".*?\")\);", re.S)


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


def parse_page(html):
    m = HYDRATION_RE.search(html)
    if not m:
        raise RuntimeError("페이지 구조가 바뀐 것 같음: hydration 데이터를 찾지 못함")
    data = json.loads(json.loads(m.group(1)))
    search = data["loaderData"]["search"]
    return search["searchResults"], int(search.get("totalRecords") or 0)


def fetch_all_jobs():
    jobs, page, total = {}, 1, None
    while True:
        results, total_records = parse_page(fetch(SEARCH_URL.format(locale=LOCALE, loc=LOCATION, page=page)))
        if total is None:
            total = total_records
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


def is_internship(job):
    team = (job.get("team") or {}).get("teamCode", "")
    return team in INTERN_TEAM_CODES or bool(INTERN_PATTERN.search(job.get("postingTitle", "")))


def job_url(job):
    return DETAIL_URL.format(locale=LOCALE, id=job["id"], slug=job.get("transformedPostingTitle", ""))


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch_details(job):
    """공고 상세(JD) 데이터. 실패해도 알림은 보내야 하므로 빈 dict 반환."""
    try:
        m = HYDRATION_RE.search(fetch(job_url(job)))
        return json.loads(json.loads(m.group(1)))["loaderData"]["jobDetails"]["jobsData"]
    except Exception as e:  # noqa: BLE001
        print(f"상세 페이지 파싱 실패 ({job['positionId']}): {e}", file=sys.stderr)
        return {}


MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
DATE_RE = re.compile(
    rf"\b{MONTHS}\s*\d{{1,2}}\b|\b\d{{1,2}}\s*{MONTHS}|\d{{1,2}}\s*월\s*\d{{1,2}}\s*일|"
    r"\b20\d{2}[./-]\d{1,2}[./-]\d{1,2}\b|\b\d{1,2}/\d{1,2}\b",
    re.I,
)
SCHEDULE_KW_RE = re.compile(
    r"deadline|timeline|timing|interview|duration|period|start date|apply by|closing|"
    r"마감|일정|기간|면접|전형|근무|시작",
    re.I,
)
DEADLINE_RE = re.compile(r"deadline|apply by|closing|close[sd]? on|마감", re.I)


def clean_line(line):
    return re.sub(r"\s+", " ", line.strip().lstrip("•·-*–● ").strip())


def extract_schedule(detail):
    """JD 에서 날짜·일정이 들어간 줄과 지원 마감 줄을 뽑는다."""
    schedule, deadline = [], None
    for field in ("description", "minimumQualifications", "responsibilities", "jobSummary"):
        for raw in (detail.get(field) or "").splitlines():
            line = clean_line(raw)
            if not line or len(line) > 220 or line in schedule:
                continue
            has_date = bool(DATE_RE.search(line))
            if has_date or (SCHEDULE_KW_RE.search(line) and line.endswith(":")):
                schedule.append(line)
                if deadline is None and has_date and DEADLINE_RE.search(line):
                    deadline = line
    return deadline, schedule


def short(text, n):
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= n else text[:n] + "…"


def bullets(text, limit=5):
    items = [clean_line(l) for l in (text or "").splitlines() if clean_line(l)]
    return "".join(f"<li>{escape(short(i, 200))}</li>" for i in items[:limit])


def build_email(new_jobs):
    blocks, deadlines = [], []
    for j in new_jobs:
        d = fetch_details(j)
        deadline, schedule = extract_schedule(d)
        deadlines.append(deadline)
        url = escape(job_url(j))
        loc = ", ".join(l.get("name", "") for l in j.get("locations", []))
        team = (j.get("team") or {}).get("teamName", "")

        if deadline:
            top = (f'<div style="background:#fff1f0;border-left:4px solid #e5484d;padding:10px 14px;'
                   f'font-size:16px;font-weight:600;margin:8px 0">⏰ 지원 마감: {escape(deadline)}</div>')
        else:
            top = ('<div style="background:#f5f5f7;border-left:4px solid #999;padding:10px 14px;margin:8px 0">'
                   '⏰ JD에 지원 마감일이 명시되어 있지 않음 → 최대한 빨리 지원 권장</div>')
        sched_html = (
            f'<p style="margin:12px 0 4px;font-weight:600">📅 일정</p>'
            f'<ul style="margin:0;padding-left:20px">{"".join(f"<li>{escape(s)}</li>" for s in schedule)}</ul>'
            if schedule else ""
        )
        resp = bullets(d.get("responsibilities"), 4)
        quals = bullets(d.get("minimumQualifications"), 4)
        blocks.append(
            '<div style="margin:0 0 28px;padding-bottom:20px;border-bottom:1px solid #ddd">'
            f'<a href="{url}" style="font-size:19px;font-weight:700;color:#06c;text-decoration:none">'
            f'{escape(j["postingTitle"])}</a>'
            f'{top}{sched_html}'
            f'<p style="margin:14px 0"><a href="{url}" style="background:#0071e3;color:#fff;padding:9px 18px;'
            f'border-radius:18px;text-decoration:none;font-weight:600">지원하러 가기 →</a></p>'
            f'<p style="color:#555;font-size:13px;margin:6px 0">{escape(team)} · {escape(loc)} · '
            f'게시일 {escape(j.get("postingDate", ""))}</p>'
            + (f'<p style="margin:12px 0 4px;font-weight:600">주요 업무</p><ul style="margin:0;padding-left:20px;'
               f'color:#333">{resp}</ul>' if resp else "")
            + (f'<p style="margin:12px 0 4px;font-weight:600">필수 자격</p><ul style="margin:0;padding-left:20px;'
               f'color:#333">{quals}</ul>' if quals else "")
            + ("" if resp else f'<p style="color:#333">{escape(short(j.get("jobSummary"), 300))}</p>')
            + "</div>"
        )

    first = new_jobs[0]["postingTitle"]
    dl = f" (마감: {short(deadlines[0], 40)})" if deadlines[0] else ""
    more = f" 외 {len(new_jobs) - 1}건" if len(new_jobs) > 1 else ""
    subject = f"[Apple Korea 인턴] {first}{more}{dl}"
    body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:640px;line-height:1.5">'
        f'{"".join(blocks)}'
        f'<p style="color:#888;font-size:12px">Apple Korea 새 인턴 공고 {len(new_jobs)}건 · 전체 한국 공고: '
        f'<a href="https://jobs.apple.com/{LOCALE}/search?location={LOCATION}">jobs.apple.com</a></p></div>'
    )
    return subject, body


def send_email(subject, html):
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    to = os.environ.get("MAIL_TO") or user
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"], msg["To"], msg["Date"] = subject, to, formatdate(localtime=True)
    msg["From"] = formataddr((str(Header(SENDER_NAME, "utf-8")), user))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


def main():
    dry = "--dry-run" in sys.argv
    alert_all = "--all" in sys.argv

    jobs = fetch_all_jobs()
    targets = jobs if alert_all else [j for j in jobs if is_internship(j)]
    seen = load_seen()
    new = [j for j in targets if j["positionId"] not in seen]
    print(f"한국 공고 {len(jobs)}건, 대상 {len(targets)}건, 신규 {len(new)}건")
    for j in new:
        print(f"  NEW {j['positionId']} {j['postingTitle']} ({j.get('postingDate')}) {job_url(j)}")

    if not new:
        return
    subject, html = build_email(new)
    if dry:
        print("[dry-run] 메일 제목:", subject)
        return
    send_email(subject, html)
    print("메일 발송 완료")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for j in new:
        seen[j["positionId"]] = {"title": j["postingTitle"], "posted": j.get("postingDate"), "notified": now}
    save_seen(seen)


if __name__ == "__main__":
    main()
