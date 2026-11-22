"""회사별 한국 채용 공고 수집기.

각 수집기는 Job 리스트를 돌려준다. Job.detail() 은 JD 본문을
{"description", "minimumQualifications", "preferredQualifications", "responsibilities"} (줄바꿈 텍스트) 로 돌려준다.
새 회사를 추가하려면 함수를 하나 만들고 SOURCES 에 등록하면 된다.
"""
import html as htmllib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

INTERN_RE = re.compile(r"\bintern(ship)?s?\b|인턴|co-?op\b", re.I)
CONTRACT_RE = re.compile(r"contract|fixed[- ]term|temporary|계약", re.I)


@dataclass
class Job:
    source: str            # seen.json 키 접두어 (apple, amazon, microsoft, google)
    company: str           # 메일에 표시할 회사명
    id: str
    title: str
    url: str
    posted: Optional[date]
    kind: str              # 인턴 / 정규직 / 계약직
    location: str = ""
    team: str = ""
    hours: Optional[int] = None
    all_url: str = ""      # 회사 전체 한국 공고 페이지
    _detail: Callable[[], Dict[str, str]] = field(default=lambda: {}, repr=False)

    @property
    def key(self):
        return f"{self.source}:{self.id}"

    def detail(self):
        """JD 본문. 가져오지 못하면 None → 호출 측이 이번 회차를 건너뛰고 다음에 재시도."""
        try:
            return self._detail() or {}
        except Exception as e:  # noqa: BLE001
            print(f"  상세 파싱 실패 ({self.key}): {e}", file=sys.stderr)
            return None


def kind_of(title, intern_flag=False):
    if intern_flag or INTERN_RE.search(title):
        return "인턴"
    if CONTRACT_RE.search(title):
        return "계약직"
    return "정규직"


# ───────────────────────── 공통 도우미 ─────────────────────────

def fetch(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ko;q=0.8"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            wait = 5 * (i + 1)
            if getattr(e, "code", None) == 429:  # 요청 제한: Retry-After 존중 (최대 60초)
                try:
                    wait = min(60, max(wait, int(e.headers.get("Retry-After") or 20)))
                except (TypeError, ValueError):
                    wait = 20
            print(f"  fetch 실패 ({e}), {wait}초 후 재시도...", file=sys.stderr)
            time.sleep(wait)


def fetch_json(url):
    return json.loads(fetch(url))


def html_text(s):
    """HTML → 줄 단위 텍스트."""
    if not s:
        return ""
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>|<li[^>]*>|<h\d[^>]*>", "\n", s)
    s = htmllib.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ")
    return "\n".join(l.strip() for l in s.splitlines() if l.strip())


SECTION_HEADINGS = [
    (re.compile(r"^(overview|about the (job|role|team)|job description|description|the role|position overview)$", re.I),
     "description"),
    (re.compile(r"^((key |primary )?responsibilities|what you('ll| will) do|your role)$", re.I), "responsibilities"),
    (re.compile(r"^(preferred|other|additional|desired|nice to have)( qualifications| skills)?$", re.I),
     "preferredQualifications"),
    (re.compile(r"^(qualifications|(required|minimum|basic) qualifications|requirements|what you('ll)? need)$", re.I),
     "minimumQualifications"),
]


def split_sections(text, default="description"):
    """'Responsibilities' 같은 제목 줄을 기준으로 텍스트를 섹션별로 나눈다."""
    out, cur = {}, default
    for line in text.splitlines():
        head = line.strip().rstrip(":：").strip()
        # "While not required, we also look for the following Preferred Qualifications:" 같은 문장형 제목
        if line.strip().endswith(":") and len(head) <= 120 and re.search(r"preferred qualifications", head, re.I):
            cur = "preferredQualifications"
            continue
        for rx, name in SECTION_HEADINGS:
            if len(head) <= 40 and rx.match(head):
                cur = name
                break
        else:
            out.setdefault(cur, []).append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def ts_date(ts):
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ───────────────────────── Apple ─────────────────────────

APPLE_LOCALE, APPLE_LOC = "ko-kr", "korea-republic-of-KOR"
APPLE_HYDRATION = re.compile(r"__staticRouterHydrationData = JSON\.parse\((\".*?\")\);", re.S)


def _apple_hydration(page_html):
    m = APPLE_HYDRATION.search(page_html)
    if not m:
        raise RuntimeError("Apple 페이지 구조가 바뀐 것 같음: hydration 데이터 없음")
    return json.loads(json.loads(m.group(1)))["loaderData"]


def apple():
    base = f"https://jobs.apple.com/{APPLE_LOCALE}"
    all_url = f"{base}/search?location={APPLE_LOC}"
    raw, page, total = {}, 1, None
    while True:
        s = _apple_hydration(fetch(f"{all_url}&page={page}"))["search"]
        total = total if total is not None else int(s.get("totalRecords") or 0)
        if not s["searchResults"]:
            break
        for r in s["searchResults"]:
            raw[r["positionId"]] = r
        if len(raw) >= total or page >= 20:
            break
        page += 1
    if total and not raw:
        raise RuntimeError(f"Apple: {total}건이라는데 하나도 파싱 못함")

    jobs = []
    for r in raw.values():
        url = f"{base}/details/{r['id']}/{r.get('transformedPostingTitle', '')}"
        team = r.get("team") or {}

        def detail(url=url):
            d = _apple_hydration(fetch(url))["jobDetails"]["jobsData"]
            return {k: d.get(k) or "" for k in
                    ("description", "minimumQualifications", "preferredQualifications", "responsibilities")}

        jobs.append(Job(
            source="apple", company="Apple Korea", id=r["positionId"], title=r["postingTitle"], url=url,
            posted=_iso_date(r.get("postDateInGMT")),
            kind=kind_of(r["postingTitle"], team.get("teamCode") == "STDNT"),
            location=", ".join(l.get("name", "") for l in r.get("locations", [])),
            team=team.get("teamName", ""), hours=r.get("standardWeeklyHours"), all_url=all_url, _detail=detail,
        ))
    return jobs


def _iso_date(s):
    try:
        return datetime.fromisoformat(s[:10]).date()
    except (TypeError, ValueError):
        return None


# ───────────────────────── Amazon / AWS ─────────────────────────

def amazon():
    api = "https://www.amazon.jobs/en/search.json?normalized_country_code%5B%5D=KOR&result_limit=100&sort=recent&offset={}"
    all_url = "https://www.amazon.jobs/en/search?country%5B%5D=KOR&sort=recent"
    raw, offset = [], 0
    while True:
        d = fetch_json(api.format(offset))
        raw += d.get("jobs") or []
        offset += 100
        if offset >= int(d.get("hits") or 0) or not d.get("jobs") or offset >= 1000:
            break

    jobs = []
    for r in raw:
        biz = r.get("business_category") or ""
        company = "AWS" if biz == "aws" or "aws" in r["title"].lower() else "Amazon"
        try:
            posted = datetime.strptime(re.sub(r"\s+", " ", r.get("posted_date", "")), "%B %d, %Y").date()
        except ValueError:
            posted = None
        detail = {
            "description": html_text(r.get("description")),
            "minimumQualifications": html_text(r.get("basic_qualifications")),
            "preferredQualifications": html_text(r.get("preferred_qualifications")),
        }
        jobs.append(Job(
            source="amazon", company=f"{company} Korea", id=str(r["id_icims"]), title=r["title"].strip(),
            url="https://www.amazon.jobs" + r["job_path"], posted=posted,
            kind=kind_of(r["title"], bool(r.get("is_intern"))),
            location=r.get("normalized_location") or r.get("location") or "",
            team=r.get("job_category") or "", all_url=all_url, _detail=lambda d=detail: d,
        ))
    return jobs


# ───────────────────────── Microsoft ─────────────────────────

MS_API = "https://apply.careers.microsoft.com/api/pcsx"


def microsoft():
    search = MS_API + "/search?domain=microsoft.com&query=&location=Korea&sort_by=timestamp&num=10&start={}{}"
    all_url = "https://apply.careers.microsoft.com/careers?location=Korea&sort_by=timestamp"

    def collect(extra=""):
        out, start = {}, 0
        while True:
            d = fetch_json(search.format(start, extra))["data"]
            for p in d.get("positions") or []:
                out[str(p["id"])] = p
            start += 10
            if start >= int(d.get("count") or 0) or not d.get("positions") or start >= 500:
                break
            time.sleep(1)  # 요청 제한(429) 예방
        return out

    raw = collect()
    interns = set(collect("&filter_employment_type=internship"))

    jobs = []
    for pid, p in raw.items():
        def detail(pid=pid):
            time.sleep(1.5)  # 상세 요청 연속 호출 시 429 예방
            d = fetch_json(f"{MS_API}/position_details?position_id={pid}&domain=microsoft.com&hl=en")["data"]
            return split_sections(html_text(d.get("jobDescription")))

        jobs.append(Job(
            source="microsoft", company="Microsoft Korea", id=pid, title=p["name"].strip(),
            url=f"https://apply.careers.microsoft.com/careers/job/{pid}", posted=ts_date(p.get("postedTs")),
            kind=kind_of(p["name"], pid in interns), location=" / ".join(p.get("locations") or []),
            team=p.get("department") or "", all_url=all_url, _detail=detail,
        ))
    return jobs


# ───────────────────────── Google ─────────────────────────

GOOGLE_BASE = "https://www.google.com/about/careers/applications/jobs/results"
GOOGLE_DS = re.compile(r"AF_initDataCallback\(\{key: 'ds:1', hash: '\d+', data:(.*?), sideChannel: \{\}\}\);</script>",
                       re.S)


def google():
    all_url = f"{GOOGLE_BASE}?location=South%20Korea&sort_by=date"

    def collect(extra=""):
        out, page = {}, 1
        while True:
            m = GOOGLE_DS.search(fetch(f"{all_url}{extra}&page={page}"))
            if not m:
                raise RuntimeError("Google 페이지 구조가 바뀐 것 같음: ds:1 데이터 없음")
            d = json.loads(m.group(1))
            rows, total, size = d[0] or [], int(d[2] or 0), int(d[3] or 20)  # d = [rows, None, total, page_size]
            for row in rows:
                out[str(row[0])] = row
            if not rows or page * size >= total or page >= 25:
                break
            page += 1
        return out

    raw = collect()
    interns = set(collect("&employment_type=INTERN"))

    def h(row, i):
        try:
            return html_text(row[i][1])
        except (IndexError, TypeError):
            return ""

    jobs = []
    for jid, row in raw.items():
        quals = split_sections(h(row, 4), default="minimumQualifications")
        detail = {
            "description": h(row, 10),
            "responsibilities": h(row, 3),
            "minimumQualifications": quals.get("minimumQualifications", ""),
            "preferredQualifications": quals.get("preferredQualifications", ""),
        }
        locs = "; ".join(l[0] for l in (row[9] or []) if l)
        jobs.append(Job(
            source="google", company="Google Korea", id=jid, title=row[1].strip(),
            url=f"{GOOGLE_BASE}/{jid}-{slugify(row[1])}", posted=ts_date((row[12] or [None])[0]),
            kind=kind_of(row[1], jid in interns), location=locs,
            team=row[7] if row[7] and row[7] != "Google" else "",
            all_url=all_url, _detail=lambda d=detail: d,
        ))
    return jobs


SOURCES = {"apple": apple, "amazon": amazon, "microsoft": microsoft, "google": google}
