# NIKKE Auto-QA System Design

## 시스템 목표 (Header)

이 시스템은 NIKKE 빌드 변경 시 자동으로 QA를 수행하고, 결과를 검토 가능한 형태로 정리해 리포트 초안까지 만든다. 8 단계 파이프라인:

1. **Cloud 서버측은 Perforce depot과 연결되어 새 빌드가 올라옴을 감지**
2. **Perforce의 NIKKE 서버 setting JSON을 읽어 server 측 정보 확인**
3. **Perforce의 NIKKE 클라이언트 setting JSON을 읽어 빌드 스펙을 비롯한 테스트 환경 준비**
4-1. **운영 서버와 연결된 Client의 mobile device 상태 확인. 없으면 에뮬레이터(샌드박스) 준비**
4-2. **QA Sheet 검토, 이번 changelist에서 추가해야 할 Test 항목 누락 여부 확인**
4-3. **VLM 서버측 상태 확인, 추가 가용량(이번 테스트 실행분)에 대한 펜딩 실시**
5. **모바일 디바이스 혹은 에뮬레이터에 TestCase 배정**
6. **TC 실시. 클라측 녹화 진행. 병행해서 캡쳐 및 로그를 서버측으로 업로드**
7. **웹 대시보드에서 TC 상세 항목 확인 및 LLM 간 상태 디버깅**
8. **완료된 TC에 한해 pending된 에러 리포트 드래프트 검토**

---

## 1. 전체 아키텍처

```
┌────────────────────────────────────────────────────────────────────────────┐
│                           Cloud Control Plane                              │
│                                                                            │
│  ┌─────────────────┐   ┌──────────────────┐   ┌──────────────────────┐  │
│  │ Perforce Watcher │──▶│  Build Manager   │──▶│   TC Scheduler        │  │
│  │  (CL detection)  │   │ (server+client    │   │  (job queue / load   │  │
│  │                  │   │   config parser) │   │    balancing)         │  │
│  └─────────────────┘   └──────────────────┘   └──────────┬───────────┘  │
│           │                     │                          │              │
│           ▼                     ▼                          │              │
│  ┌─────────────────┐   ┌──────────────────┐              │              │
│  │  QA Sheet DB    │   │  VLM Capacity    │              │              │
│  │  (CL→TC mapping) │   │   Reservation    │              │              │
│  └─────────────────┘   └──────────────────┘              │              │
│                                                            │              │
│                  ┌─────────────────────────────────────────┴───────┐     │
│                  ▼                                                  ▼     │
│       ┌──────────────────────┐                 ┌──────────────────────┐ │
│       │   Object Storage     │                 │  Result/Run DB        │ │
│       │  (mp4/png/log/json) │                 │  (PostgreSQL)         │ │
│       └──────────────────────┘                 └──────────────────────┘ │
│                  │                                          │            │
└──────────────────┼──────────────────────────────────────────┼────────────┘
                   │                                          │
                   ▼                                          ▼
┌──────────────────────────────────────┐     ┌────────────────────────────┐
│          Edge / Client Side          │     │      Web Dashboard         │
│                                      │     │                            │
│  ┌────────────┐    ┌──────────────┐ │     │  - Live TC progress         │
│  │ Device Pool│    │ Emulator     │ │     │  - Per-iter VLM debug       │
│  │ Manager    │    │ Sandbox      │ │     │  - Recording playback        │
│  │ (USB ADB)  │    │ (k8s pods)   │ │     │  - Error report draft       │
│  └─────┬──────┘    └──────┬───────┘ │     │  - LLM call introspection   │
│        │                   │         │     └────────────────────────────┘
│        ▼                   ▼         │                  ▲
│  ┌─────────────────────────────┐    │                  │
│  │       TC Runner Worker      │────┼──────────────────┘
│  │  - orchestrator + recorder  │    │   (WebSocket)
│  │  - capture/log uploader     │    │
│  └─────────────────────────────┘    │
└──────────────────────────────────────┘
                   │
                   ▼
        ┌────────────────────┐
        │   VLM Inference    │
        │   (vLLM cluster)   │
        │  Qwen3.5-27B / +   │
        └────────────────────┘
```

---

## 2. 컴포넌트 명세

### 2.1 Perforce Watcher (단계 1)

**역할**: Perforce depot의 특정 path를 polling/webhook으로 감시. 새 CL이 올라오면 Build Manager 트리거.

**구현**:
- P4 Triggers (서버측 webhook) 또는 `p4 changes` polling (5분 주기)
- 감시 대상 path:
  - `//depot/nikke/server-config/...` (서버 setting json)
  - `//depot/nikke/client-config/...` (클라이언트 setting json)
  - `//depot/nikke/builds/.../*.apk` (APK 빌드)
- 새 CL 감지 시 → JSON payload 생성 → Build Manager API 호출
  ```json
  {
    "cl": 123456,
    "author": "...",
    "files_changed": [...],
    "timestamp": "2026-04-28T...",
    "depot_paths": {"server_config":"...", "client_config":"...", "apk":"..."}
  }
  ```

### 2.2 Build Manager (단계 2-3)

**역할**: 서버 + 클라이언트 config JSON을 읽어 환경 spec을 정규화.

**서버 config 파싱** (단계 2):
- 파싱 항목: 서버 주소 (dev/staging/prod), 활성 server flag, glossary, balance config 등
- 결과: `server_env_spec.json` 저장

**클라이언트 config 파싱** (단계 3):
- 파싱 항목: APK 빌드 번호, 디바이스 호환성 (Android API level), 화면 해상도 권장값, debug flag
- 결과: `client_env_spec.json` 저장
- APK는 object storage로 다운로드 + sha256 캐시

### 2.3 QA Sheet DB / Coverage Manager (단계 4-2)

**역할**: 각 CL과 TC를 매핑. 누락 검출.

**스키마**:
```sql
test_cases (
  tc_no INT PRIMARY KEY,
  category TEXT,
  title TEXT,
  plan_path TEXT,        -- nikke_bvt/plans/tc_NN.json
  required_server TEXT,  -- 'dev' | 'staging' | 'prod' | 'any'
  glossary_tags TEXT[],
  last_pass_cl INT,
  last_run_at TIMESTAMP
)

cl_tc_coverage (
  cl INT,
  tc_no INT,
  reason TEXT,           -- 'auto_match' | 'manual_add' | 'regression_only'
  PRIMARY KEY (cl, tc_no)
)

cl_files (
  cl INT,
  file_path TEXT,
  -- mapping rules: 'arena/*' → tc 62, 63 / 'union/*' → tc 27-30 ...
  mapped_tcs INT[]
)
```

**파일 → TC 매핑 룰** (yaml/json으로 관리):
```yaml
mappings:
  - path_glob: "**/arena/**"
    tcs: [62, 63]
  - path_glob: "**/union/**"
    tcs: [27, 28, 29, 30]
  - path_glob: "**/lobby/**"
    tcs: [12, 13, 14, 15, 16, 17]
  - path_glob: "**/campaign/**"
    tcs: [64, 65, 66, 67]
```

**누락 감지**: 새 CL의 변경 파일이 기존 mapping에 매칭되지 않으면 → "이 CL에 대해 신규 TC 필요" 알림 발행 (Slack / 대시보드 빨간 뱃지).

### 2.4 Device Pool Manager (단계 4-1)

**역할**: 물리 디바이스 + 에뮬레이터 풀 관리.

**Device 등록** (정적 yaml 또는 동적 discovery):
```yaml
devices:
  - id: shiftup-galaxy-s23-01
    type: physical
    serial: R3GYB09D0WY
    host: workstation-A
    server_target: dev      # 어느 서버에 연결됐는지
    status: idle             # idle | running | offline | maintenance
  - id: emulator-pool-1
    type: emulator
    pool: k8s
    capacity: 4              # 동시 실행 가능 4대
```

**상태 폴링**:
- `adb devices` 5초 주기 poll → status 갱신
- physical device offline 시 → Slack alert + 동일 server target의 emulator로 fallback

**에뮬레이터 sandbox** (k8s 기반):
- Genymotion Cloud / Android Studio Emulator container 이미지
- 각 pod = 1 device. ADB over network exposed.
- TC 1개당 1 pod alloc → 종료 시 destroy
- 단점: 게임 로딩 + reinstall 시간 ↑. 장점: 무한 확장.

### 2.5 VLM Capacity Manager (단계 4-3)

**역할**: vLLM 서버 가용 슬롯 추적 + reservation.

**구현**:
- vLLM 서빙 노드별 max_concurrent_requests 설정
- Redis에 `vlm:slots:{node}` 카운터
- TC 실행 직전 `INCR` (capacity check) → 종료 시 `DECR`
- 추가 노드 필요 시 k8s autoscale (HPA based on slot utilization)

**중요**: TC 1개당 평균 VLM 호출 수 기반 capacity 산정. 측정값 (TC#50: 14 iter × 1-2 call) 기준 동시 N개 TC면 ~30 active VLM call.

### 2.6 TC Scheduler (단계 5)

**역할**: 가용 device + VLM capacity + TC 우선순위 매칭.

**알고리즘**:
1. 후보 TC 목록 가져옴 (CL coverage 기반)
2. priority = (regression_critical ? 100 : 10) + (last_pass_cl < CL-N ? 20 : 0)
3. 각 TC에 대해:
   - required_server 매칭 device 검색
   - VLM capacity 확보 가능?
   - 둘 다 OK → assign + emit job
4. job → Redis Stream → TC Runner Worker가 consume

**Job payload**:
```json
{
  "job_id": "uuid",
  "cl": 123456,
  "tc_no": 60,
  "device_id": "shiftup-galaxy-s23-01",
  "vlm_endpoint": "http://vlm-pool-3:30010/v1",
  "priority": 50,
  "deadline": "2026-04-28T...",
  "context": {"server_env": "...", "apk_url": "..."}
}
```

### 2.7 TC Runner Worker (단계 6)

**역할**: 디바이스에 붙어 TC 실행. 현재 우리 `run_tc_with_sample.sh`의 클라우드 버전.

**구성**:
- `worker.py`: job consume → APK install (필요 시) → orchestrator 실행 → recorder 병행
- Recording: chunked screenrecord (현재와 동일) → 각 chunk 즉시 S3 업로드
- Capture: 매 iter capture → S3 즉시 업로드 (백그라운드 thread, TC progress 가리지 않음)
- Log: line-buffered stdout → Loki / OpenSearch에 stream
- 완료 시: results.json 업로드 + Job 완료 emit

**상태 보고** (WebSocket → Dashboard):
- 매 iter마다 `{tc_no, st_num, iter_no, action, image_url, reason}` push
- live progress + 미니 video player

### 2.8 Web Dashboard (단계 7)

**페이지 구성**:

**a) Overview**
- 진행 중 TC 카드 (live)
- 최근 N CL의 PASS/FAIL 매트릭스 (TC×CL)

**b) TC Detail**
- 녹화 영상 player (S3 링크)
- 각 iter timeline:
  - 캡처 썸네일
  - VLM의 reason
  - tap 좌표를 capture 위에 overlay
- 실패 iter: VLM 자기성찰 (debug_ask_40b 결과) inline

**c) LLM Debugging**
- 한 iter 클릭 → 그 iter의 VLM input/output JSON 다운로드
- "다시 물어보기" 버튼: 같은 iter를 새 prompt로 replay → 비교

**d) Error Report Draft (단계 8)**
- FAIL TC마다 자동 생성된 bug report draft 표시
- 사용자가 review/edit → JIRA / Linear 발행

**Tech**: React + Tailwind + react-query + WebSocket. 백엔드 FastAPI.

### 2.9 Error Report Drafter (단계 8)

**역할**: FAIL TC의 로그 + 캡쳐 + VLM 자기성찰을 입력으로 받아 사람이 읽을 만한 bug report draft 생성.

**입력**:
- TC plan (verify_question, expected behavior)
- iterations_record (action history)
- VLM 자기성찰 (`debug_ask_failure.py` JSON)
- 시작/종료 캡처 + cycle 진입 캡처

**출력 (자동 draft)**:
```markdown
## 제목
[TC #60][regression] Tribe Tower Subtask 9: Corp Tower 일일 도전권 0/3 → 진입 불가

## 환경
- CL: 123456
- 서버: dev daily
- APK: nk_android_debug_146.1.51
- 디바이스: shiftup-galaxy-s23-01

## 재현 단계
1. 방주 → 트라이브 타워 진입
2. ... (TC plan에서 자동 추출)

## 기대 결과
Tetra Tower stage selection 진입 + 전투 진입 버튼 활성화

## 실제 결과
전투 진입 버튼 잠김 (남은 횟수 0/3 표시)

## 첨부
- 녹화: [link]
- 핵심 캡처: iter#9 [thumbnail]
- VLM 자기성찰 (한국어): "[일일 도전권 소진 인지... 다른 타워로 전환 필요]"

## 분류 (자동)
- 범주: System Limit (게임 자체 동작)
- 우선순위: 검토 필요
- 재현률: 100% (반복 실행 시 동일)
```

LLM 한 번 호출 (작은 prompt-engineering call)이면 충분.

---

## 3. 데이터 흐름 (Sequence Diagram)

```
Perforce       Watcher        Build Mgr   Coverage   Scheduler   Runner    Device   VLM      Dashboard
   │              │               │           │          │          │         │       │           │
   │── new CL ───▶│               │           │          │          │         │       │           │
   │              │── parse ─────▶│           │          │          │         │       │           │
   │              │               │── tc list ▶│          │          │         │       │           │
   │              │               │           │── job ──▶│          │         │       │           │
   │              │               │           │          │── alloc ▶│         │       │           │
   │              │               │           │          │          │── adb ─▶│       │           │
   │              │               │           │          │          │── VLM ─────────▶│           │
   │              │               │           │          │          │         │   ◀───│           │
   │              │               │           │          │          │── push live ──────────────▶│
   │              │               │           │          │          │── upload(s3)                │
   │              │               │           │          │          │── done ───────────────────▶│
   │              │               │           │          │          │── draft report                │
```

---

## 4. 기술 스택 (제안)

| 컴포넌트 | 기술 |
|---|---|
| Perforce Watcher | Python + p4python lib (또는 P4 Triggers shell) |
| Build/Coverage/Scheduler API | Python + FastAPI + asyncpg |
| Job Queue | Redis Streams (간단) 또는 NATS JetStream (heavy) |
| Result DB | PostgreSQL 16 |
| Object Storage | S3 (AWS) 또는 MinIO (self-host) |
| Device Pool | Python + adb-shell + scrcpy network bridge |
| Emulator | Genymotion Cloud / k8s + Android SDK emulator (`docker-android`) |
| VLM Inference | vLLM (이미 사용 중) on k8s + Triton (옵션) |
| Worker | Python + 기존 orchestrator 재사용 |
| Dashboard FE | React + Tailwind + Recharts + react-query |
| Realtime | WebSocket + Server-Sent Events |
| Logging | Loki + Grafana (또는 OpenSearch) |
| Deploy | Docker Compose (PoC) → k8s (prod) |

---

## 5. 점진적 구축 로드맵

**Phase 0 — 현재 상태**
- 단일 워크스테이션, 수동 실행, sample 폴더 결과 저장

**Phase 1 (2-4주, MVP)**
- (1) Perforce Watcher 단순 polling
- (2-3) Build Manager 단일 머신 로컬 파싱
- (5-6) 기존 `run_tc_with_sample.sh`를 worker.py로 wrap, job queue 연결
- (7) 정적 HTML 대시보드 (TC 결과 표 + 영상 링크)
- 단일 device, 단일 VLM

**Phase 2 (4-8주, 확장)**
- (4-1) Device Pool Manager + 에뮬레이터 1-2대 sandbox
- (4-2) QA Sheet DB + path→TC mapping rules
- (4-3) VLM Capacity Reservation + 노드 추가
- (7) 라이브 대시보드 (WebSocket)

**Phase 3 (확장 / 안정화)**
- (8) Error Report Drafter 자동화
- 에뮬레이터 k8s autoscale
- 에러 카테고리 분류 (System Limit / VLM 오류 / 게임 버그)
- Slack/Linear 통합

---

## 6. 위험요소 & Open Questions

**위험요소:**
1. **Perforce 권한 모델**: 빌드 봇 계정 P4 권한 + ticket 갱신 자동화 필요. 보안 레이어.
2. **물리 디바이스 안정성**: USB 연결 끊김 빈도 (이번 세션에 새벽 4시경 발생). 자동 reconnect + 재부팅 절차.
3. **에뮬레이터 게임 호환성**: ARM 전용 라이브러리는 x86 에뮬레이터에서 안 도는 경우. ARM 에뮬 (GameLoop / Genymotion ARM) 검증 필요.
4. **VLM 비용**: 동시 N TC × 평균 14 iter × 2 call/iter = 큰 throughput. on-prem GPU vs 클라우드 inference 비용 비교.
5. **녹화 저장 비용**: TC 1개당 ~150MB. 1일 100 TC = 15GB. 보관 정책 (30일 hot, 90일 cold).
6. **False PASS 누적**: 8단계 자동화에서 false PASS가 자동 release approve로 이어지지 않도록. zoom-consistency 같은 verify gate 필수.

**Open Questions:**
- Perforce 외 빌드 시스템 (Jenkins / GitHub Actions) 통합 여부?
- 에뮬레이터 vs 물리 디바이스 결과 동등성 검증 — 동일 TC 결과가 다르면 어느 쪽이 정답?
- 다국어 (KR/JP/EN/CN 클라이언트) 지원 시 glossary 분기 → plan json 다국어화?
- 야간 자동 실행 vs business hour만? device/사람 간섭 충돌 방지.
- 어떤 단계(어느 TC fail)에서 release를 block할지의 정책 — automation 결과를 release gate로 쓰면 false FAIL이 release 막음.

---

## 7. 부록 — 디렉토리 구조 (제안)

```
/cloud-server/
├── perforce_watcher/
│   ├── watcher.py
│   └── triggers.cfg
├── build_manager/
│   ├── parser_server.py
│   ├── parser_client.py
│   └── apk_fetcher.py
├── coverage/
│   ├── mapping_rules.yaml
│   ├── api.py            # FastAPI: /coverage/{cl}
│   └── db.py
├── scheduler/
│   ├── scheduler.py
│   ├── priority.py
│   └── job_queue.py
├── vlm_capacity/
│   ├── reserver.py
│   └── slots.lua          # Redis Lua script
├── dashboard/
│   ├── frontend/          # React app
│   └── api/               # FastAPI websocket + REST
├── error_drafter/
│   ├── prompt_template.py
│   └── post_to_jira.py
└── shared/
    ├── models.py          # Pydantic schemas
    └── storage.py         # S3 client wrapper

/edge-runner/
├── worker.py              # job consumer
├── device_manager.py
├── recorder.py            # 기존 record_tc.sh 재구현
└── orchestrator/          # 현재 nikke_bvt/ 가 여기로 이동
```
