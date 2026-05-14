# HTTP/HTTPS 및 CVAT 사내 배포 정리

## 1. HTTP vs HTTPS

### 핵심 차이

|항목 |HTTP |HTTPS         |
|---|-----|--------------|
|포트 |80   |443           |
|암호화|평문 전송|TLS/SSL 암호화   |
|인증서|불필요  |SSL/TLS 인증서 필요|
|SEO|불리   |유리            |

### 장단점 요약


**HTTP**: 구현 단순, 인증서 비용 없음. 단점은 패킷 스니핑·MITM 공격에 무방비, 최신 Web API 사용 불가.
**HTTPS**: 데이터 기밀성·무결성 보장, 최신 보안 기능 사용 가능. 단점은 인증서 관리 필요(자동화로 거의 해소됨).


### HTTPS를 쓰기 어려운 경우


localhost·내부망 등 공인 CA가 인증서를 발급하지 않는 환경
레거시 클라이언트(구형 IoT, 오래된 안드로이드)
폐쇄망(OCSP/CRL 검증 불가)
CDN 뒤 오리진 서버 내부 통신
특수 프로토콜의 격리된 구간


### localhost가 HTTPS를 쓰기 어려운 이유 (구체)


공인 CA는 `localhost`나 `127.0.0.1`에 인증서 발급 불가 (전 세계 모든 컴퓨터가 자기 자신을 가리키므로 소유권 개념 성립 안 함)
자체 서명 인증서는 브라우저가 `NET::ERR_CERT_AUTHORITY_INVALID` 경고로 거부
OAuth/결제 콜백 테스트에서 막힘 (HTTPS 콜백 URL 요구)
회사 보안 정책으로 OS 신뢰 저장소 수정 불가한 경우 `mkcert` 우회도 불가
모바일 기기에서 개발 서버 접근 시 인증서 별도 설치 필요


-----

## 2. 도메인이 있으면 HTTPS는 거의 자동

### 필요한 것들


도메인
공인 IP를 가진 서버 또는 호스팅
DNS A 레코드 설정
TLS 인증서 발급 (Let’s Encrypt 무료)
웹서버 설정


### 사실상 자동화된 경우

Vercel, Netlify, Cloudflare Pages, GitHub Pages, AWS Amplify, Firebase Hosting, Caddy 등은 도메인 연결만으로 HTTPS 자동.

### Let’s Encrypt 갱신 주기 (2026년 5월 기준)


**기본**: 90일 유효, 60일마다 갱신 권장
**변경 진행 중** (CA/Browser Forum 업계 표준):
2026년 5월 13일: 45일 인증서 opt-in 가능 (tlsserver 프로파일)
2027년 2월 10일: 기본 프로파일이 64일로 전환
2028년 2월 16일: 45일 + 7시간 인증 재사용 기간으로 완전 전환

**단기 인증서**: 160시간(약 6일) 유효, IP 주소 인증서도 정식 출시됨


### 비용

연 1~2만원(도메인) + 인증서 무료 + 무료 호스팅 티어 활용 시 사실상 무료.

-----

## 3. CVAT 사내 배포 (회사 사람만 사용)

### 배포 방식 비교

|방식            |보안   |비용|외부 접근      |
|--------------|-----|--|-----------|
|사내망 전용        |매우 높음|무료|불가 (VPN 필요)|
|VPN/Zero Trust|매우 높음|무료|가능         |
|공개 인터넷 + 인증   |중간   |무료|가능         |

### 최종 선택: 사내 서버 + Cloudflare Tunnel + Cloudflare Access

**조건 충족**:


사내 서버 이미 보유
핸드폰·재택 외부 접속 필요
무료
Cloudflare Zero Trust 무료 플랜으로 50명까지 지원


### 구성 흐름

```
[직원 폰/재택 노트북]
        ↓ HTTPS
[Cloudflare Edge] ← Access 인증 (회사 이메일)
        ↓ 암호화 터널 (사무실 → CF로 아웃바운드)
[사내 서버: cloudflared + CVAT Docker]
```

### 장점


사무실 공유기 포트포워딩 불필요
공인 IP / 고정 IP 불필요
서버에 인바운드 포트 0개 노출
HTTPS 자동
모바일에서 앱 설치 없이 브라우저로 접속
회사 구글 워크스페이스 계정으로 인증 가능


### 설치 단계 요약


사내 서버에 Docker + CVAT 설치
   
   ```bash
   git clone https://github.com/cvat-ai/cvat
   cd cvat
   docker compose up -d
   ```

도메인을 Cloudflare로 이관 (네임서버 변경)
Cloudflare Zero Trust 팀 생성 (무료 플랜)
Tunnel 생성 → 서버에 cloudflared 설치
Public Hostname 설정: `cvat.yourcompany.com` → `localhost:8080`
Access Application 생성 → 회사 이메일 도메인만 허용
CVAT_HOST 환경변수 설정 후 재시작


### 서버 사양 권장


최소: 4 vCPU, 8GB RAM, 100GB 디스크
권장: 8 vCPU, 16GB RAM, SSD 500GB
OS: Ubuntu 22.04 LTS


-----

## 4. 사내 서버 OS/Docker 업데이트가 필요한 이유

외부 노출이 없어도 업데이트는 필요. **기능 업데이트가 아닌 보안 패치**가 핵심.

### 위협 시나리오


내부 PC가 악성코드 감염 → 사내망 안에서 서버 공격
CVAT에 업로드되는 이미지/비디오 파일이 악성 (ffmpeg, Pillow 등 파싱 라이브러리 취약점)
컨테이너 탈출 → Docker 엔진 취약점으로 호스트 OS까지 침투
공급망 공격 (악성 Docker 이미지나 의존성)
Cloudflare Access 우회 (직원 이메일 계정 해킹)


### 업데이트 주기


**Ubuntu 보안 패치**: `unattended-upgrades`로 자동화
**Docker 엔진**: 분기 1회 정도
**CVAT**: 분기~반기 1회 (백업 후)
**OS 재부팅**: 분기 1회 (커널 업데이트 적용)


### 최소 필수


`unattended-upgrades` 자동 설정
CVAT 연 1-2회 업데이트 (데이터 백업 후)


-----

## 5. CVAT 코드 수정과 업데이트 충돌 관리

### 관리 전략

|방식                                     |적합 케이스      |머지 부담|
|---------------------------------------|------------|-----|
|Fork + merge/rebase                    |백엔드 로직 수정   |높음   |
|Patch 파일 관리                            |작고 산발적 수정   |중간   |
|Override (docker-compose, 환경변수, 볼륨 마운트)|설정/UI 리소스 수정|없음   |
|커스텀 Docker 이미지                         |가벼운 파일 교체   |낮음   |

### 충돌 최소화 팁


수정한 로직을 별도 함수/파일로 분리, 원본에선 한 줄만 분기 호출
수정 사유를 커밋 메시지에 자세히 기록
수정 파일 목록을 README에 명시
메이저 버전 업그레이드는 별도 브랜치에서 테스트 후 머지
업데이트 전 DB·데이터 볼륨 백업 필수


### 현재 수정 사항별 권장 방식

#### (1) Manual-frame annotation job 생성/삭제 허용

- **목적**: stripe/anchor 워크플로우를 위해 ANNOTATION 타입 job을 API로 manual frame 리스트로 만들고, 필요 시 지울 수 있도록 함. `customization/scripts/cvat_interleaved_jobs.py` 등 자동화의 전제.
- **변경 파일** (총 ~30줄):
  - `cvat/apps/engine/serializers.py` (`JobWriteSerializer.create`): GROUND_TRUTH뿐 아니라 ANNOTATION도 허용, GT 전용 검증/`validation_layout` 갱신은 GT일 때만 수행.
  - `cvat/apps/engine/views.py` (`JobViewSet.perform_destroy`): GROUND_TRUTH뿐 아니라 ANNOTATION도 삭제 허용.
- **Fork 불가피한 정도는 아님.** 변경 표면이 작고 GT 동작은 그대로 유지됨.
- **관리 방식**: 패치 파일로 보관. 현재 구조:
  - `customization/patches/0001-allow-manual-annotation-jobs.patch`
  - `customization/patches/apply.sh` (apply/revert/check)
  - `customization/patches/README.md`
  - 일상 사용:
    - `customization/patches/apply.sh check`: 패치가 현재 트리에 깔려 있거나 깨끗하게 적용 가능한 상태인지 확인.
    - `customization/patches/apply.sh apply`: 모든 패치 적용.
    - `customization/patches/apply.sh revert`: 모든 패치 되돌리기.
  - 업그레이드 절차: `customization/patches/apply.sh revert` → `git fetch/merge upstream` → `customization/patches/apply.sh apply`. 충돌이 나면 그 부분만 수동 수정 후 `git diff` 으로 패치 재생성.
- **선택지**:
  - patch 유지 (가장 가벼움, 추천).
  - 작은 fork + 주기 rebase.
  - upstream에 PR 시도 (GT 동작이 유지되므로 작은 PR로 제안 가능).

#### (2) Admin 외 Task 생성 제한


**코드 수정 불필요**. CVAT 내장 권한 시스템으로 해결.
CVAT의 Global Role: Admin, Business, User, Worker
**Worker 역할**은 Task 생성 권한 없음, 할당된 job만 작업 가능


#### 방법별 정리


**Django admin에서 사용자 그룹 변경**

`/admin/` 접속 → Users → 사용자 선택 → Groups에서 `user` 제거하고 `worker`만 남김

**신규 가입자 기본 그룹을 worker로**
**자체 가입 막고 admin이 계정 생성** (Cloudflare Access가 외부 차단하므로 사실상 충분)
**Rego 정책 수정** (`cvat/apps/*/rules/*.rego`) — 세밀한 제어 필요 시


-----

## 핵심 요약


**HTTPS는 무조건 이득**, 끄는 게 위험. 도메인만 있으면 사실상 무료로 가능.
**CVAT 사내 배포는 Cloudflare Tunnel + Access 조합이 최적**: 무료, 외부 접근 가능, 포트 노출 0.
**사내 서버라도 보안 업데이트는 필요**, 최소한 `unattended-upgrades`는 설정.
**현재 CVAT 코드 수정은 약 30줄 (serializers.py, views.py) 수준이라 Fork 불가피하지 않음.** `customization/patches/`에 patch 파일로 보관해 업그레이드 시 `customization/patches/apply.sh`로 관리. Task 생성 제한은 코드 수정 없이 권한 설정(Worker role)으로 해결.

## 결정 사항 (3-4줄 요약)

- 통신 방식은 **HTTPS로 운영**하기로 결정. 사내 서버 + 도메인 + Let's Encrypt(또는 Cloudflare Tunnel) 기반으로 평문 노출 없이 접근.
- **OS / Docker / CVAT은 주기적으로 업데이트**하기로 결정. Ubuntu 보안 패치는 `unattended-upgrades`로 자동화, Docker 엔진은 분기 1회, CVAT은 분기~반기 1회 데이터 백업 후 갱신.
- 자체 수정은 `serializers.py` / `views.py`의 manual-frame annotation job 허용 패치(~30줄)뿐이므로 **patch 파일로 관리**, Admin 외 Task 생성 제한은 **CVAT 권한 설정(Worker role)**으로 해결.