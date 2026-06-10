<!-- keywords: codeb, pages, deploy, 배포, publish, 출력, hosting, 호스팅, 정적, static, bagelpages, 게임배포 -->
# codeb pages 배포 가이드

## 목적
완성한 정적 웹 앱(HTML/JS/CSS 게임 등)을 **BagelPages**에 배포해 URL로 공개한다.
codeb CLI는 이 머신 PATH(`~/.codeb/bin`)에 설치되어 있고 로그인되어 있다.

## 중요: 실행 방법
- codeb는 hwharness의 네이티브 툴이 아니다. **`bash` 툴로 셸 명령을 실행**한다 (사용자 승인 필요).
- 배포는 되돌리기 어려운 외부 동작이므로, 실행 전 어떤 `--app` 이름으로 어디를 올리는지 사용자에게 한 줄로 알린다.

## 핵심 명령

```bash
# 디렉터리 배포 (자동 zip 압축 후 업로드) — 가장 일반적
codeb pages deploy <빌드디렉터리> --app <앱이름>

# 예: Game/ 폴더를 my-game 이라는 이름으로 배포
codeb pages deploy ./Game --app my-game

# zip 파일을 직접 배포
codeb pages deploy ./dist.zip --app my-game

# 단일 파일(index.html 하나짜리) 배포
codeb pages deploy ./index.html --app my-game

# 디렉터리를 압축 없이 파일 단위로 업로드
codeb pages deploy ./Game --app my-game --no-zip
```

## 배포 모드 (경로로 자동 판별)
- `.zip` 파일  → zip 모드: 그대로 업로드
- 디렉터리     → zip 모드: 압축 후 업로드 (`--no-zip`이면 파일 단위)
- 단일 파일    → files 모드: 개별 업로드

## 주요 플래그
| 플래그 | 설명 |
|---|---|
| `--app <이름>` | **필수.** 앱 이름. URL과 식별자가 됨 |
| `--no-zip` | 디렉터리를 압축하지 않고 파일 단위 업로드 |
| `--pages-url <url>` | BagelPages 서버 URL 직접 지정 (보통 불필요) |
| `-v, --verbose` | 디버그용 상세 출력 |

## 관리 명령
```bash
codeb pages list                    # 배포된 앱 목록
codeb pages status --app my-game    # 특정 앱 배포 상태 확인
codeb pages delete --app my-game    # 앱 삭제
```

## 배포 전 체크리스트
- [ ] 진입점이 **`index.html`** 인가? (정적 호스팅은 index.html을 루트로 찾음)
- [ ] 빌드 산출물 경로가 맞는가? (소스가 아니라 빌드된 디렉터리/zip)
- [ ] 경로가 **상대/절대 모두 가능**하나, 올릴 폴더만 정확히 가리키는가?
- [ ] `--app` 이름이 정해졌는가? (영문/하이픈 권장, 특수문자·공백 금지)
- [ ] 기존 같은 이름 앱을 덮어쓰는 배포라면 사용자에게 확인했는가?

## 전형적 흐름 (게임 제작 → 배포)
1. `write_file`/`edit_file`로 게임을 디렉터리(예: `Game/`)에 작성, 진입점은 `index.html`.
2. (선택) `bash`로 로컬에서 동작 확인.
3. `bash`로 배포: `codeb pages deploy ./Game --app <이름>`.
4. 출력에 나온 배포 URL을 사용자에게 보고.
5. 필요하면 `codeb pages status --app <이름>`으로 상태 확인.
