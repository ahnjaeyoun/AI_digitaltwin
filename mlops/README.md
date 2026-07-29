# Digital Twin Jenkins MLOps

이 폴더는 유압 프레스 디지털 트윈의 세 Docker 이미지와 통합 Compose
파이프라인을 Jenkins에서 빌드·테스트·배포하기 위한 독립 실행 구조입니다.

## 포함 범위

| 구성 | 이미지/역할 |
|---|---|
| `raw_solver` | CSV Publisher, MQTT Solver 브리지, C++ 실시간 Solver |
| `anomaly` | TensorFlow Autoencoder 이상탐지 |
| `risk` | PyTorch LSTM Autoencoder 위험도 평가 |
| `broker` | Eclipse Mosquitto |
| `timescaledb` | 원천·Solver·이상탐지·위험도 결과 저장 |
| `ci-tester` | 네 개 TimescaleDB 테이블과 Solver 유한값 검사 |

Unity Windows 실행파일은 Docker 이미지에 포함하지 않습니다. Unity 빌드는
Unity가 설치된 Windows Jenkins Agent에서 별도 Pipeline으로 관리합니다.

## 디렉터리

```text
mlops/
├── Jenkinsfile
├── compose.yaml
├── compose.local.yaml
├── compose.ci.yaml
├── compose.production.yaml
├── .env.example
├── ci/
├── deploy/
├── docker/mosquitto/
├── raw_solver/
├── anomaly/
└── risk/
```

## Jenkins Agent 요구사항

- Linux Jenkins Agent label: `docker-linux`
- Git
- Docker Engine
- Docker Compose v2
- Registry 및 배포 서버로 접근 가능한 네트워크

Jenkins Pipeline을 전체 `digital_twin` 저장소에서 실행하면 Script Path를
`mlops/Jenkinsfile`로 지정합니다. `mlops` 자체를 별도 저장소로 만들 경우
Script Path를 `Jenkinsfile`로 지정해도 동작합니다.

현재 `A:\coding\digital_twin`은 Git 저장소가 아니므로, Jenkins의
`checkout scm`을 사용하려면 이 폴더 또는 `mlops` 폴더를 Git 저장소에
커밋하고 원격 저장소에 Push해야 합니다.

## Jenkins Credentials

다음 ID를 Jenkins Credentials에 등록합니다.

| Credentials ID | 형식 | 용도 |
|---|---|---|
| `docker-registry` | Username/Password | Docker 이미지 Push |
| `digital-twin-staging-ssh` | SSH private key | Staging 서버 접속 |
| `digital-twin-staging-env` | Secret file | Staging 배포용 `.env` |

`digital-twin-staging-env` 파일에는 최소한 다음 값이 필요합니다.

```dotenv
TIMESCALE_DB=digital_twin
TIMESCALE_USER=digital_twin
TIMESCALE_PASSWORD=replace-with-a-secret
TIMESCALE_SCHEMA=public
MQTT_BIND_ADDRESS=127.0.0.1
MQTT_HOST_PORT=1883
TIMESCALE_BIND_ADDRESS=127.0.0.1
TIMESCALE_HOST_PORT=6543
```

배포 서버는 Registry에 대해 읽기 전용 `docker login`이 완료돼 있어야 합니다.

## Pipeline 단계

1. SCM Checkout
2. Compose와 Python 구문 검증
3. `raw-solver`, `anomaly`, `risk`, `ci-tester` 이미지 빌드
4. 격리된 Compose 프로젝트 실행
5. 빠른 간격으로 시뮬레이션 CSV 발행
6. 다음 TimescaleDB 테이블 저장 검증
   - `press_raw_data`
   - `press_solver_results`
   - `press_anomaly_results`
   - `press_risk_results`
7. JUnit XML과 Compose 로그 보관
8. `main` 또는 Git tag 빌드일 때 Registry Push
9. `DEPLOY_STAGING=true`일 때 Staging 배포
10. CI 전용 컨테이너와 볼륨 정리

이미지는 Git commit 앞 12자를 태그로 사용하므로 이전 태그로 롤백할 수
있습니다.

## 로컬 Compose 검증

PowerShell:

```powershell
cd A:\coding\digital_twin\mlops
Copy-Item .env.example .env
docker compose -f compose.yaml -f compose.local.yaml config
docker compose -f compose.yaml -f compose.local.yaml build
docker compose -f compose.yaml -f compose.local.yaml up -d
```

시뮬레이션 Publisher:

```powershell
docker compose -f compose.yaml -f compose.local.yaml `
  --profile simulation run --rm raw-publisher
```

종료할 때 DB 데이터를 유지하려면 `-v`를 사용하지 않습니다.

```powershell
docker compose -f compose.yaml -f compose.local.yaml down
```

## 운영 배포 주의사항

- 운영에서는 `TIMESCALE_PASSWORD=0000`을 사용하지 않습니다.
- 운영 TimescaleDB 볼륨에 `docker compose down -v`를 실행하지 않습니다.
- `REGISTRY_PREFIX`에는 마지막 `/`가 필요합니다.
- Unity가 다른 PC에서 MQTT에 접근한다면 방화벽과 MQTT 인증/TLS를 별도로
  구성하고 `MQTT_BIND_ADDRESS`도 운영 네트워크에 맞게 설정합니다.
