# 유압 프레스 고장 데이터셋 (v1 샘플)

`dataset_hydraulic_faults.csv` — 디지털 트윈 솔버로 물리적으로 생성한 고장 분류 학습용 데이터셋.

## 1. 생성 방식 (물리 기반)

설계 원칙("사후 교란 금지, 솔버 재실행")대로, 사후에 숫자를 지어낸 것이 아니라 **실제 솔버(main.cpp)를 재구성한 배치 드라이버(`gen.cpp`)로 매 초 재실행**하여 만들었습니다.

- `network.json`(13노드·12셀·4모드)을 기준으로, 매 행(초)마다 운전조건(목표압·유온·RPM·밸브개도)과 고장 파라미터를 덮어써 솔버를 다시 풀고 물리량을 계산.
- **정상 재현 검증 통과**: 원본 정상 데이터를 이 드라이버로 재현했을 때 유량·CAP압·펌프토출압 오차 ≈ 0 (downstroke/hold/upstroke 전부 0.00).
- Tier B(누설)는 층류 누설 `q = K·Δp`를 펌프 공급유량에 더하는 방식으로 확장 구현.

## 2. 규모와 구성

- **5,500 사이클 / 79,829 행 (초 단위 시계열)** — 설계 목표(10만 준균형)의 축소 샘플. 파이프라인은 10만으로 그대로 확장 가능(§6).
- 11 클래스: `normal`(1,500) + 물리 고장 10종(각 400). 각 고장은 심각도 3단계(initial/moderate/severe) × 발생방식(abrupt/gradual) × 운전조건 랜덤화.

## 3. 컬럼 사전 (44열)

**식별자**: `cycle_id`, `cycle_second`, `cycle_phase`, `target_chamber`

**실측 피처(모델 입력, 리치 계측 가정)**:
`temperature_c`, `pump_rpm`, `valve_cmd_{pa,pb,at,bt}`(지령), `valve_pos_{pa,pb,at,bt}`(실제 위치 피드백), `flow_rate_L_min`(유량계), `pump_in_pressure_bar`, `pump_out_pressure_bar`, `cyl_cap_pressure_bar`, `cyl_rod_pressure_bar`, `pump_delta_pressure_bar`, `hydraulic_power_kW`, `max_velocity_m_s`, `target_pressure_error_bar`

**Ground-truth · audit (`gt_` 접두어 — 학습 피처에서 제외)**:
`gt_N_VALVE_*_p`(매니폴드 내부압, 미계측), `gt_Kv_*`, `gt_loss_*`(내부 파생량), `gt_leak_mode`, `gt_fault_location`

**라벨**: `fault_class`(11), `severity_level`, `fault_severity`(0~1), `onset_type`, `is_anomaly`

> 학습 시 `gt_`로 시작하는 컬럼은 반드시 피처에서 제외하세요. 누설 위치·유량 같은 정답을 피처로 쓰면 데이터 누수로 실기 전이 시 성능이 붕괴합니다.

## 4. 검증 결과 (설계 8장 필수 절차)

- kNN(7) 11-클래스 CV 정확도: **0.77**, 정상 vs 고장 이진: **0.85** (nearest-centroid=0.49 → 비선형 모델일수록 상승. XGBoost/1D-CNN은 더 높게 나올 것).
- 강한 클래스: `valve_stuck` 1.00, `normal` 0.98, `suction_clog` 0.93, `overheat` 0.91, `relief_stuck` 0.90, `valve_int_leak` 0.83, `pump_wear` 0.76.
- 약한 클래스: `seal_leak` 0.58, `ext_leak` 0.44, `relief_early` 0.05 — **설계 8장에서 예측한 "누설·릴리프 중첩 그룹"**(CAP 강하 + 유량↑ + 목표압오차가 유사). 개선책: 심각도 확대, ROD압·질량수지 파생 피처 강화, 또는 클래스 병합.
- 심각도 구배 정상: 예) `valve_stuck` 유량 20→16→10, `seal_leak` 24→25→27 (단조 반응).

## 5. 파이프라인 파일

- `gen.cpp` — 배치 솔버 드라이버(C++20, leak 셀 확장 포함). 컴파일: `g++ -O2 -std=c++20 gen.cpp -o gen`
- `gen_scenarios.py` — 시나리오·라벨 생성기(클래스·심각도·onset·랜덤화)
- `merge_validate.py` — 물리 결과 + 라벨 병합, 최종 CSV 및 구별성 검증
- `network.json` — 솔버 기준 네트워크

실행: `python3 gen_scenarios.py && ./gen network.json scenarios.csv > physics.csv && python3 merge_validate.py`

## 6. 전체 10만으로 스케일업

`gen_scenarios.py`의 `COUNTS`를 준균형(정상 30,000 + 고장 각 7,000)으로 바꾸고 재실행하면 됩니다. 솔버 속도 ≈ 5,000행/초 → 약 1.4M행(10만 사이클) 생성에 ~5분. (샌드박스 45초 제한 때문에 v1은 5,500 사이클로 제공.)

## 7. 알려진 한계

- `relief_early`/`relief_stuck`은 압력경계-고정 솔버 특성상 릴리프 유량 분기를 근사 모델링했습니다(정확한 릴리프 밸브 동특성은 후속 과제).
- `suction_clog`는 저유량 구간에서 신호가 약합니다(물리적으로 NPSH·입구압에 주로 영향).
- 센서 고장(Tier C)은 설계대로 1차에서 제외.
