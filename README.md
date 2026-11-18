# Apple Korea 인턴 공고 알림

GitHub Actions가 10분마다 [jobs.apple.com 한국 공고](https://jobs.apple.com/ko-kr/search?location=korea-republic-of-KOR)를 확인하고,
처음 보는 인턴 공고(학생 팀 / 제목에 intern·인턴)가 있으면 Gmail로 알림을 보냅니다. 컴퓨터가 꺼져 있어도 동작합니다.

## 설정 (한 번만, 약 10분)

### 1. Gmail 앱 비밀번호 만들기
1. Google 계정에 **2단계 인증**이 켜져 있어야 합니다.
2. <https://myaccount.google.com/apppasswords> 에서 이름(예: `apple-alert`)을 입력하고 만들기
3. 나오는 16자리 비밀번호를 복사해 둡니다.

### 2. GitHub 저장소 만들기
1. <https://github.com/new> 에서 새 저장소 생성 (예: `apple-korea-intern-alert`)
   - **Public 권장**: Actions 사용 시간이 무제한 무료입니다. 비밀번호는 Secrets에 저장되어 공개되지 않습니다.
   - Private으로 하면 월 2,000분 무료 한도 때문에 `check.yml`의 cron을 `*/30 * * * *`(30분마다)로 바꾸세요.
2. 이 폴더의 파일을 올립니다 (`.github/workflows/check.yml` 포함):
   ```bash
   git remote add origin https://github.com/<내아이디>/apple-korea-intern-alert.git
   git push -u origin main
   ```

### 3. Secrets 등록
저장소 → **Settings → Secrets and variables → Actions → New repository secret**
| 이름 | 값 |
|---|---|
| `GMAIL_USER` | 보내는 Gmail 주소 |
| `GMAIL_APP_PASSWORD` | 1단계의 16자리 앱 비밀번호 |
| `MAIL_TO` | (선택) 받을 주소. 비우면 `GMAIL_USER`로 보냄. 여러 개는 쉼표로 |

### 4. 첫 실행
**Actions 탭 → Apple Korea intern check → Run workflow**.
현재 열려 있는 인턴 공고가 첫 메일로 오면 설정 완료입니다. 이후에는 새 공고가 생길 때만 메일이 옵니다.

## 참고
- Apple 사이트 구조가 바뀌어 체크가 실패하면 GitHub이 "workflow run failed" 메일을 보내줍니다. 그 메일이 오면 스크립트 수정이 필요하다는 뜻입니다.
- 인턴 외 한국 신규 공고 전체를 받고 싶으면 `check.yml`의 실행 줄을 `python3 check_jobs.py --all`로 바꾸세요.
- 로컬 테스트: `python3 check_jobs.py --dry-run`
