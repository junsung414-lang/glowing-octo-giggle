"""profile.json 과 공고의 '필수 자격'을 비교해 지원이 아예 불가능한 공고를 거른다.

원칙: 확실할 때만 거른다. 애매하면 통과(=알림 발송). 놓친 기회가 불필요한 메일보다 비싸다.
screen() 은 탈락 사유 리스트를 돌려준다. 빈 리스트면 통과.
"""
import json
import re
from pathlib import Path

PROFILE_FILE = Path(__file__).with_name("profile.json")

SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|principal|staff|head|director|vp|vice president|chief|lead|leader|manager of managers)\b", re.I)
TECH_TITLE = re.compile(
    r"\b(engineer|engineering|developer|scientist|technician|architect|sde|programmer|devops|sre)\b", re.I)
YEARS = re.compile(r"(?<!\d)(\d{1,2})\s*\+?\s*(?:or more\s+)?(?:years?|yrs?)\b|(?<!\d)(\d{1,2})\s*년\s*(?:이상|의|간|차)?", re.I)
OR_YEARS = re.compile(r"\bor\s+(?:equivalent\s+)?(\d{1,2})\s*\+?\s*years?", re.I)  # '학위 or N년 경력' 같은 대체 경로
EQUIVALENT = re.compile(r"equivalent (practical |professional |work )?(experience|combination)|or equivalent", re.I)
ADV_DEGREE = re.compile(r"\b(master'?s|ph\.?\s?d|doctora(l|te)|mba)\b", re.I)
BACHELOR = re.compile(r"\b(bachelor'?s?|ba/bs|b\.?s\.?|b\.?a\.?|undergraduate|학사)\b", re.I)
DEGREE_LINE = re.compile(r"\b(degree|bachelor|associate'?s|diploma|학위|전공)\b", re.I)
TECH_FIELD = re.compile(
    r"computer science|computer engineering|software engineering|electrical|electronic|mechanical|"
    r"information technology|information science|networking|engineering|physics|mathematics|stem\b", re.I)
NONTECH_FIELD = re.compile(
    r"business|management|economics|marketing|finance|social|sociology|psychology|communications?\b(?! engineering)|"
    r"humanities|liberal arts|public|policy|any (field|discipline)|related field of study", re.I)
LANGS = ["japanese", "chinese", "mandarin", "cantonese", "french", "german", "spanish", "vietnamese", "thai",
         "indonesian", "hindi", "arabic", "russian", "portuguese", "italian", "malay", "tagalog"]
LANG_RE = re.compile(r"\b(" + "|".join(LANGS) + r")\b", re.I)
LANG_REQ = re.compile(r"fluen|proficien|native|business[- ]level|bilingual|speak|written and verbal|communicat", re.I)
SPECIAL = {
    "disability": re.compile(r"people with disabilit|\bpwd\b|disabled|장애인", re.I),
    "veteran": re.compile(r"veteran|보훈", re.I),
}


def load_profile():
    return json.loads(PROFILE_FILE.read_text(encoding="utf-8")) if PROFILE_FILE.exists() else {}


def _lines(text):
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


def screen(job, detail, profile=None):
    p = profile if profile is not None else load_profile()
    if not p:
        return []
    reasons = []
    title = job.title
    minq = detail.get("minimumQualifications") or ""

    # 1. 직급: Senior/Principal/Head/Lead 등은 다년 경력 전제
    if p.get("exclude_senior_titles") and job.kind != "인턴":
        m = SENIOR_TITLE.search(title)
        if m:
            reasons.append(f"시니어 직급 ({m.group(0)})")

    # 2. 직무: 엔지니어·개발·과학자·기술자 등 기술직
    if p.get("exclude_technical_roles"):
        m = TECH_TITLE.search(title)
        if m:
            reasons.append(f"기술직 ({m.group(0)})")

    # 3. 필수 경력 연수 > 허용치
    #    한 줄 안의 'A AND 3+ years OR B AND 2+ years' 같은 대체 경로는 가장 낮은 값,
    #    서로 다른 필수 조건 줄끼리는 모두 충족해야 하므로 가장 높은 값.
    #    학위 대신 'or 6+ years' 같은 대체 경로는 무시.
    need = 0
    for line in _lines(minq):
        ys = [int(a or b) for a, b in YEARS.findall(OR_YEARS.sub("", line)) if 0 < int(a or b) < 40]
        if ys:
            need = max(need, min(ys))
    if need > p.get("max_required_years", 1):
        reasons.append(f"필수 경력 {need}년 이상")

    # 4. 학위: 석·박사만 허용하는 경우 / 기술 전공만 허용하는 경우 (equivalent 경험 대체 가능하면 통과)
    if p.get("highest_degree", "bachelor") == "bachelor" and minq:
        if ADV_DEGREE.search(minq) and not BACHELOR.search(minq) and not EQUIVALENT.search(minq):
            reasons.append("석사 이상 학위 필수")
    for line in _lines(minq):
        if (DEGREE_LINE.search(line) and TECH_FIELD.search(line) and not NONTECH_FIELD.search(line)
                and not EQUIVALENT.search(line)):
            reasons.append("기술 전공 학위 필수")
            break

    # 5. 언어: 프로필에 없는 언어를 필수로 요구
    known = {l.lower() for l in p.get("languages", [])}
    for text in [title] + _lines(minq):
        for lang in LANG_RE.findall(text):
            if lang.lower() not in known and (text is title or LANG_REQ.search(text)):
                reasons.append(f"{lang.capitalize()} 필수")
                break
        else:
            continue
        break

    # 6. 희망 직무: 제외 키워드는 모두에, 포함 키워드는 인턴 외 공고에 적용 (인턴은 드물어서 제외 키워드만)
    tr = p.get("target_roles") or {}
    hay = f"{title}\n{job.team}"
    ex = next((k for k in tr.get("exclude", []) if re.search(k, hay, re.I)), None)
    if ex:
        reasons.append(f"희망 직무 아님 ({re.search(ex, hay, re.I).group(0)})")
    elif tr.get("include") and job.kind != "인턴" and not any(re.search(k, hay, re.I) for k in tr["include"]):
        reasons.append(f"희망 직무 아님 ({job.team or '분류 없음'})")

    # 7. 대상 제한 전형 (장애인·보훈 등)
    blob = f"{title}\n{minq}"
    for prog, rx in SPECIAL.items():
        if prog not in p.get("special_programs", []) and rx.search(blob):
            reasons.append({"disability": "장애인 대상 전형", "veteran": "보훈 대상 전형"}[prog])
    return reasons
