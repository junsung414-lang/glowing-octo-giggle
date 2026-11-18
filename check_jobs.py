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
from email.mime.text import MIMEText
from email.utils import formatdate
from html import escape
from pathlib import Path

LOCALE = "ko-kr"
LOCATION = "korea-republic-of-KOR"
SEARCH_URL = "https://jobs.apple.com/{locale}/search?location={loc}&page={page}"
DETAIL_URL = "https://jobs.apple.com/{locale}/details/{id}/{slug}"
SEEN_FILE = Path(__file__).with_name("seen.json")

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


def build_email(new_jobs):
    subject = f"[Apple Korea] 새 인턴 공고 {len(new_jobs)}건: {new_jobs[0]['postingTitle']}"
    rows = []
    for j in new_jobs:
        loc = ", ".join(l.get("name", "") for l in j.get("locations", []))
        team = (j.get("team") or {}).get("teamName", "")
        summary = (j.get("jobSummary") or "").strip().replace("\n", " ")
        if len(summary) > 300:
            summary = summary[:300] + "…"
        rows.append(
            f'<div style="margin:0 0 20px">'
            f'<a href="{escape(job_url(j))}" style="font-size:17px;font-weight:600">{escape(j["postingTitle"])}</a><br>'
            f'<span style="color:#555">{escape(team)} · {escape(loc)} · 게시일 {escape(j.get("postingDate", ""))}</span>'
            f'<p style="color:#333;margin:6px 0">{escape(summary)}</p>'
            f'<a href="{escape(job_url(j))}">→ 바로 지원하러 가기</a></div>'
        )
    body = (
        '<div style="font-family:-apple-system,sans-serif;max-width:640px">'
        f'<h2>Apple Korea 새 인턴 공고 {len(new_jobs)}건</h2>{"".join(rows)}'
        f'<p style="color:#888;font-size:12px">전체 한국 공고: '
        f'<a href="https://jobs.apple.com/{LOCALE}/search?location={LOCATION}">jobs.apple.com</a></p></div>'
    )
    return subject, body


def send_email(subject, html):
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    to = os.environ.get("MAIL_TO") or user
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"], msg["Date"] = subject, user, to, formatdate(localtime=True)
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
