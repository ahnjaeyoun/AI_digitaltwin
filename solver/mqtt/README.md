# MQTT Publisher/Solver - Tailscale 구성

Publisher와 Solver는 Tailscale의 암호화된 사설망을 통해 Mosquitto broker에
접속합니다. 공유기 포트포워딩이나 공인 IP의 `1883` 포트 개방은 사용하지 않습니다.

## Visual Studio에서 실행

최초 한 번 `Setup-MqttBroker.ps1`로
`mosquitto-tailscale.generated.conf`를 생성한 뒤 사용할 수 있습니다.

1. Visual Studio에서 `D:\Project1\Project1.slnx`를 엽니다.
2. 구성과 플랫폼을 `Debug | x64`로 선택합니다.
3. 솔루션 탐색기의 `MQTT Visual Studio Runner`를 마우스 오른쪽 버튼으로 클릭합니다.
4. `시작 프로젝트로 설정(Set as Startup Project)`을 선택합니다.
5. `F5` 또는 `Ctrl+F5`를 누릅니다.
6. 사용자명이나 비밀번호 입력 없이 자동으로 송수신과 Solver 계산이 진행됩니다.

Visual Studio 2022 17.11 이상에서 공유 시작 프로필을 사용하는 경우 툴바에서
`MQTT Tailscale Demo (10 rows)` 프로필을 선택해도 됩니다.

기본 Visual Studio 실행은 CSV 10행을 간격 없이 보내는 smoke test입니다. 실행기는
다음 순서를 자동으로 보장합니다.

```text
Mosquitto 연결/실행 → Solver MQTT 구독 완료 → Publisher 10행 송신 → Solver 출력 완료
```

실행 중에는 CSV의 각 행(MQTT 메시지)마다 아래 로그가 바로 출력됩니다.

```text
[MQTT_송신완료] 순번=1/10 사이클ID=... 메시지ID=...
[Solver] [MQTT_수신완료] 수신행=1 순번=1 사이클ID=... 입력파일=...
[Solver] [CXX] [SOLVER_COMPLETED] row=1 cycle_id=... elapsed_ms=... status=ok output=...
[Solver] [UNITY_송신완료] 송신행=1 입력행=1 사이클ID=... 주제='hydraulic-press/unity/state'
```

- `MQTT_송신완료`: QoS 1 메시지를 broker가 확인(PUBACK)한 시점
- `MQTT_수신완료`: Solver가 메시지를 받고 실시간 입력 CSV에 기록한 시점
- `SOLVER_COMPLETED`: C++ Solver 계산 결과를 출력 CSV에 기록한 시점
- `UNITY_송신완료`: 동일 입력값과 Solver 결과값을 하나의 JSON으로 Broker가 확인한 시점

기본값은 모든 행을 출력합니다. 데이터가 많아 로그를 줄이고 싶으면 Publisher와
Solver 실행 인수에 각각 `--log-every 100`처럼 지정할 수 있습니다.

전체 1,000-cycle을 실행하려면 `MQTT Visual Studio Runner`의 속성에서
`디버깅 > 명령 인수(Command Arguments)` 마지막에 `-Full`을 추가합니다.

## 기본 구성

| 항목 | 값 |
|---|---|
| Broker PC | `desktop-9kudbdm` |
| Broker Tailscale IP | `100.116.221.78` |
| MQTT endpoint | `100.116.221.78:1883` |
| Input topic | `hydraulic-press/solver/input` |
| Unity topic | `hydraulic-press/unity/state` |
| MQTT 인증 | 사용 안 함 (`allow_anonymous true`) |

## 1. 모든 PC를 같은 tailnet에 연결

Broker, Publisher, Solver PC에 Tailscale을 설치하고 같은 tailnet에 로그인합니다.
각 PC에서 연결 상태와 주소를 확인합니다.

```powershell
tailscale status
tailscale ip -4
```

Publisher와 Solver PC에서 broker PC에 도달하는지 확인합니다.

```powershell
tailscale ping 100.116.221.78
```

## 2. Broker PC 설정

Mosquitto를 설치한 다음 설정 스크립트를 실행합니다. 이 스크립트는
Mosquitto listener를 broker PC의 Tailscale 주소에만 바인딩하고,
익명 접속을 활성화하며, `mqtt_settings.json`의 broker 주소도 함께 갱신합니다.

```powershell
cd D:\Project1\mqtt
.\Setup-MqttBroker.ps1
```

MQTT 사용자명이나 비밀번호는 만들거나 입력하지 않습니다.

관리자 PowerShell에서 Tailscale 주소 범위에 MQTT 방화벽을 허용합니다.

```powershell
cd D:\Project1\mqtt
.\Enable-MqttFirewall.ps1
```

일반 PowerShell에서 broker를 실행하고 창을 유지합니다.

```powershell
cd D:\Project1\mqtt
.\Start-MqttBroker.ps1
```

생성되는 Mosquitto listener는 다음과 같습니다.

```text
listener 1883 100.116.221.78
listener_allow_anonymous true
```

익명 접속이므로 같은 tailnet에서 TCP 1883 포트에 도달할 수 있는 장치는 모두
송신과 구독을 할 수 있습니다. 이 설정을 공인 IP나 `0.0.0.0`에 바인딩하지 말고,
필요하면 Tailscale 접근 정책으로 접속 가능한 장치를 제한하세요.

## 3. Python 의존성 설치

Publisher와 Solver PC에서 각각 실행합니다.

```powershell
cd D:\Project1
py -m pip install -r .\mqtt\requirements.txt
```

## 4. Solver PC 실행

TimescaleDB에도 저장하려면 `TIMESCALE_DSN`을 함께 지정합니다. 비밀번호가 들어가는
접속 문자열은 `mqtt_settings.json`에 기록하지 않습니다.

현재 Solver PC에는 Docker 컨테이너 `timescaledb`가 다음과 같이 구성되어 있으며,
접속 문자열은 Windows 사용자 환경 변수 `TIMESCALE_DSN`에 저장되어 있습니다.

| 항목 | 현재 값 |
|---|---|
| 컨테이너 | `timescaledb` (`timescale/timescaledb-ha:pg18`) |
| 접속 주소 | `127.0.0.1:6543` |
| 데이터베이스 | `press_data` |
| 사용자 | `press_solver` |
| 재시작 정책 | `unless-stopped` |

```powershell
cd D:\Project1
$env:TIMESCALE_DSN = 'host=127.0.0.1 port=6543 dbname=press_data user=press_solver password=<DB 비밀번호> sslmode=disable'

tailscale ping 100.116.221.78
Test-NetConnection 100.116.221.78 -Port 1883
py .\mqtt\Solver.py
```

결과는 `realtime_solver_project/realtime_solver_output.csv`에 저장됩니다.

`TIMESCALE_DSN`이 있으면 다음 hypertable도 자동으로 생성하고 실시간 저장합니다.

| 테이블 | 저장 내용 |
|---|---|
| `public.press_raw_data` | MQTT 원천 메시지, 수신 시각, 실행ID, 사이클 정보, 전체 JSON |
| `public.press_solver_results` | Solver 결과, 유량, 압력 오차, 유압 동력, 경고, 전체 JSON |

DB 계정에 확장과 테이블 생성 권한이 없다면 관리자가 먼저 다음 스크립트를 실행하고
Solver에는 `--no-timescale-init`을 추가합니다.

```powershell
psql -d press_data -f .\mqtt\timescaledb_schema.sql
py .\mqtt\Solver.py --no-timescale-init
```

Visual Studio에서 실행할 때도 실행 전에 `TIMESCALE_DSN` 환경 변수가 설정되어 있어야
합니다. DB를 사용하지 않을 때는 이 환경 변수를 지정하지 않으면 기존 CSV 처리만
동작합니다. DB 연결이 일시적으로 끊기면 메모리 대기열에 보관하고 재연결하며,
DB 저장 실패 시 전체 실행도 실패하게 하려면 `--timescale-required`를 사용합니다.

저장 결과는 다음처럼 확인할 수 있습니다.

```sql
SELECT event_time, cycle_id, cycle_phase, active_mode
FROM public.press_raw_data
ORDER BY event_time DESC
LIMIT 10;

SELECT event_time, cycle_id, active_mode,
       calculated_flow_rate_l_min, solver_warning, status
FROM public.press_solver_results
ORDER BY event_time DESC
LIMIT 10;
```

## 5. Unity PC 연결

Unity도 Broker PC와 같은 Tailscale tailnet에 연결하고 MQTT 클라이언트에서 다음
주소와 주제를 구독합니다.

| 항목 | 값 |
|---|---|
| Broker | `100.116.221.78` |
| Port | `1883` |
| Topic | `hydraulic-press/unity/state` |
| QoS | `1` |
| 사용자명/비밀번호 | 사용 안 함 |

Solver는 C++ 계산이 완료된 각 행마다 원본 입력값과 결과값을 다음 JSON 구조로 묶어
Unity topic에 송신합니다. 숫자와 참/거짓 값은 Unity에서 바로 사용할 수 있는 JSON
자료형으로 변환됩니다.

```json
{
  "schema": "hydraulic-press.unity.v1",
  "type": "solver_state",
  "run_id": "...",
  "message_id": "...:unity:1",
  "input_row_index": 1,
  "published_at": "2026-07-21T11:00:00.000+09:00",
  "cycle_id": 1,
  "cycle_phase": "하강",
  "active_mode": "downstroke",
  "input": {
    "Press.target_pressure_bar_g": 230.0,
    "Fluid.temperature_c": 40.0
  },
  "result": {
    "calculated_flow_rate_L_min": 15.2,
    "hydraulic_power_kW": 5.8,
    "solver_warning": false,
    "status": "ok"
  }
}
```

Unity를 연결하기 전에 PC에서 Unity 메시지를 확인하려면 다음 명령을 사용할 수 있습니다.

```powershell
& 'C:\Program Files\mosquitto\mosquitto_sub.exe' `
  -h 100.116.221.78 -p 1883 `
  -t 'hydraulic-press/unity/state' -q 1 -v
```

기본값은 Unity 전송 활성화와 `retain=false`입니다. 따라서 Unity 구독을 먼저 시작해야
모든 실시간 메시지를 받을 수 있습니다. 가장 최근 상태를 Broker에 보관하려면
Solver에 `--unity-retain`을 지정하고, Unity 전송을 끄려면 `--no-unity`를 지정합니다.

## 6. Publisher PC 실행

```powershell
cd D:\Project1
tailscale ping 100.116.221.78
Test-NetConnection 100.116.221.78 -Port 1883
py .\mqtt\Publisher.py
```

10행만 빠르게 시험하려면 다음과 같이 실행합니다.

```powershell
py .\mqtt\Publisher.py --interval 0 --limit 10
```

## Tailscale 접근 제어

tailnet이 제한 정책을 사용한다면 Publisher와 Solver가 broker의 `tcp:1883`에
접근할 수 있도록 Grant가 필요합니다. `tailscale-grants.example.hujson`의 규칙을
기존 tailnet 정책에 병합합니다. 기존 정책 파일 전체를 예제 파일로 교체하면 안 됩니다.

## 주소가 변경된 경우

Broker PC에서 새 주소를 확인한 다음 설정을 다시 생성합니다.

```powershell
tailscale ip -4
cd D:\Project1\mqtt
.\Setup-MqttBroker.ps1 -BindAddress <새 Tailscale IP>
```

스크립트가 `mqtt_settings.json`의 `broker.host`와
`mosquitto-tailscale.generated.conf`의 `listener` 주소를 함께 변경합니다. 변경 후
broker를 다시 시작하세요.
