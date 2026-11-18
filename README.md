# JOB ALERT — 한국 채용 공고 알림

GitHub Actions가 10분마다 아래 회사의 **한국 공고**를 확인하고, 처음 보는 대상 공고가 있으면 공고마다 Gmail 알림을 1통씩 보냅니다.
컴퓨터가 꺼져 있어도 동작합니다.

| 회사 | 출처 | 인턴 판별 |
|---|---|---|
| Apple | jobs.apple.com (한국) | '학생' 팀 또는 제목에 intern |
| Amazon / AWS | amazon.jobs (한국) | `is_intern` 필드 또는 제목 |
| Microsoft | apply.careers.microsoft.com (Korea) | 고용 형태 Internship 필터 또는 제목 |
| Google | google.com/about/careers (South Korea) | 고용 형태 INTERN 필터 또는 제목 |

- 보내는 사람: `JOB ALERT`
- 제목: `[인턴|정규직|계약직] 포지션명 (M.D 마감)` (마감일이 없으면 `마감일 미기재`)
- 본문: 마감 D-day 배지 → 핵심 정보 → 지원 버튼 → 전형 일정 → 자격(원문) → 업무(원문)

## 알림 대상 바꾸기
`.github/workflows/check.yml`의 실행 줄을 바꿉니다.

| 원하는 것 | 실행 줄 |
|---|---|
| 인턴만 (기본) | `python3 check_jobs.py` |
| 인턴 + 정규직 | `python3 check_jobs.py --kinds=인턴,정규직` |
| 전부 | `python3 check_jobs.py --all` |
| 일부 회사만 | `python3 check_jobs.py --only=apple,google` |

처음 정규직을 켜면 그 시점에 열려 있는 정규직 공고가 한꺼번에 옵니다(현재 100건 이상).

## 설정
1. Gmail 앱 비밀번호: 보내는 계정에서 2단계 인증을 켜고 <https://myaccount.google.com/apppasswords> 에서 생성
2. 저장소 **Settings → Secrets and variables → Actions**

| 이름 | 값 |
|---|---|
| `GMAIL_USER` | 보내는 Gmail 주소 (알림 전용 계정 권장, 프로필 사진이 발신자 사진으로 보임) |
| `GMAIL_APP_PASSWORD` | 그 계정의 16자리 앱 비밀번호 |
| `MAIL_TO` | 받을 주소 (여러 개는 쉼표) |

3. **Actions → JOB ALERT check → Run workflow** 로 수동 실행 가능

## 장애 대응
- 한 회사 수집이 실패해도 나머지 회사는 계속 확인합니다.
- 같은 회사가 약 1시간 연속 실패하면 `[JOB ALERT] <회사> 공고 수집 실패 중` 메일이 1회 오고, 복구되면 `복구됨` 메일이 옵니다. 실패 메일이 오면 그 회사 사이트 구조가 바뀐 것이라 `sources.py` 수정이 필요합니다.
- 상태 파일: `seen.json`(알림 보낸 공고), `status.json`(회사별 연속 실패 횟수)

## 회사 추가하기
`sources.py`에 `Job` 리스트를 돌려주는 함수를 만들고 맨 아래 `SOURCES`에 등록하면 됩니다.

## 로컬 테스트
```bash
python3 check_jobs.py --dry-run
```
