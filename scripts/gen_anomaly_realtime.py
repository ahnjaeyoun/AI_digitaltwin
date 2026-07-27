"""
AE 이상탐지 **테스트/검증용** realtime 데이터셋 생성기 — 정상:이상 = 1:1 (2026-07-27 신규).

원본 `realtime_input_1000cycles_normal_relief_mixed.csv`의 정상 파형 중 **테스트 풀**(학습이 한 번도
보지 못한 195개)만 템플릿으로 삼아, 절반은 정상 그대로 두고 절반에는 물리 고장을 주입한다.

**템플릿 풀 분리(2026-07-27)**: 이전에는 학습 코퍼스(3,000사이클 파일)를 템플릿으로 재사용해
학습·테스트가 같은 895개 파형을 공유했고, 그 탓에 오탐률이 낙관적으로 측정됐다. 또 학습 사이클과
글자 그대로 겹치는 것을 피하려 온도·rpm을 한 번 더 추첨했는데, 그 이중 jitter가 오차 꼬리를
학습셋의 1.9배로 부풀려 오경보를 반대로 과대평가하기도 했다(두 편향이 반대 방향이라 상쇄 정도를
알 수 없었다). 풀을 나누면 중복 방지용 재추첨이 불필요해지므로, **테스트 정상을 학습과 완전히
동일한 생성 과정**(목표압·온도·rpm 1회 추첨)으로 만든다. 주입 메커니즘·심각도 수치는
`ab_solver/gen_scenarios.py`의 `MECH`와 동일하다.

고장 5종만 쓰는 이유: `origin_solver/main.cpp`에는 누설(leak) 모델이 없다 — `leak_K`/`leak_from`/
`leak_to` 분기는 `ab_solver/gen.cpp`에만 추가된 확장(v9 리턴라인 수정)이라, seal_leak/ext_leak/
valve_int_leak은 origin_solver로 재현할 수 없다. 릴리프 2종(relief_early/relief_stuck)은 릴리프
시나리오 제거 결정에 따라 제외. 남는 5종이 펌프·배관(흡입/압력)·밸브·유체로 흩어져 계통 커버리지는
확보된다.

주입 경로가 두 갈래다:
  - `valve_stuck`/`overheat`: input CSV의 컬럼(`Cell.V_DIR_*.opening_percent`, `Fluid.temperature_c`)
    자체가 계측값이므로 **여기서 직접 열화된 값을 쓴다**(솔버는 그 값을 그대로 읽는다).
  - `pump_wear`/`suction_clog`/`line_clog`: `network.json` 내부 파라미터라 input 컬럼에 자리가 없다.
    라벨 4열(`fault_class`/`fault_severity` 등)을 보고 `scripts/run_origin_solver.py`가 솔버 호출
    직전에 network를 변형한다.

라벨 4열(`fault_class`, `severity_level`, `onset_type`, `fault_severity`)은 기존 30열 뒤에 붙는다.
**모델 입력이 아니라 채점용 정답지**다 — 이상 여부 평가와 "SHAP이 실제 고장 부위를 짚는가" 검증에 쓴다.

실행:
  python scripts/gen_anomaly_realtime.py --cycles 1000 \
      --output dataset/realtime_input_1000cycles_test_mixed.csv
"""
import argparse
import csv
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
ROOT = Path(r"C:\pipes_press")
SEED = 2026            # 학습셋(seed 42)과 다른 추첨열
START_TS = datetime(2027, 1, 4, 8, 0, 0)

LABEL_COLS = ["fault_class", "severity_level", "onset_type", "fault_severity"]
SEV = ["initial", "moderate", "severe"]

# gen_scenarios.py의 MECH와 동일한 수치. origin_solver가 읽는 필드로 주입 가능한 5종만.
MECH = {
    "pump_wear":    {"scale": [0.97, 0.94, 0.91]},   # PUMP_01.pump_head_shutoff_m 배율
    "suction_clog": {"mult":  [10, 22, 40]},          # C_SUCTION.local_loss_k 배율
    "line_clog":    {"mult":  [7, 16, 30],
                     "rough": [15, 45, 90]},          # C_PRESSURE.local_loss_k + roughness_m 배율
    "valve_stuck":  {"frac":  [0.65, 0.42, 0.15]},    # 활성 방향밸브 개도 배율
    "overheat":     {"add":   [14, 24, 34]},          # Fluid.temperature_c 가산
}
FAULTS = list(MECH)
VALVE_BY_MODE = {"downstroke": "V_DIR_PA", "upstroke": "V_DIR_PB"}

# jitter·표기 규칙·템플릿 로딩은 학습셋 생성기와 **같은 함수를 공유**한다(어긋날 수 없게).
from gen_normal_realtime import (DEC, JIT_RPM, JIT_TARGET, JIT_TEMP_SD,  # noqa: E402
                                 POOL_SEED, fmt, load_templates)


def make_cycle(tmpl, cid, ts, sidx, rng, cls):
    """템플릿 사이클 1개 -> (정상 | 고장 주입) 사이클. cls=None이면 정상.

    정상 부분의 jitter는 학습셋 생성과 완전히 동일하다(목표압 tj / 온도 / rpm 각 1회 추첨,
    `Node.N_CYL_CAP/ROD`는 tj로 스케일). 그 위에만 고장을 얹는다.
    """
    tj = rng.uniform(*JIT_TARGET)
    tempj = rng.gauss(0.0, JIT_TEMP_SD)
    rpmj = rng.uniform(*JIT_RPM)
    n = len(tmpl)
    sev_i = rng.randrange(3) if cls else -1
    onset = rng.choice(["abrupt", "gradual"]) if cls else "normal"
    m = MECH.get(cls, {})

    out = []
    for si, r in enumerate(tmpl):
        row = dict(r)
        row["timestamp"] = (ts + timedelta(seconds=si)).strftime("%Y-%m-%d %H:%M:%S")
        row["sample_index"] = str(sidx + si)
        row["cycle_id"] = str(cid)
        row["cycle_second"] = str(si)

        # 고장 진행도: abrupt는 처음부터 만개, gradual은 사이클 안에서 0.25 -> 1.0 램프
        fp = 1.0 if onset == "abrupt" else (0.25 + 0.75 * si / max(1, n - 1))
        fp = fp if cls else 0.0

        row["Press.target_pressure_bar_g"] = fmt(float(r["Press.target_pressure_bar_g"]) * tj, 3)
        row["Node.N_CYL_CAP.pressure_bar_g"] = fmt(float(r["Node.N_CYL_CAP.pressure_bar_g"]) * tj, 3)
        row["Node.N_CYL_ROD.pressure_bar_g"] = fmt(float(r["Node.N_CYL_ROD.pressure_bar_g"]) * tj, 3)
        temp = float(r["Fluid.temperature_c"]) + tempj
        rpm = int(float(r["Cell.PUMP_01.rpm"]) * rpmj)

        if cls == "overheat":
            temp += m["add"][sev_i] * fp
        elif cls == "valve_stuck":
            # 해당 위상에서 실제로 흐름을 만드는 밸브만 덜 열린다(pressure_hold는 대상 없음)
            vid = VALVE_BY_MODE.get(row["System.active_mode"])
            if vid:
                key = f"Cell.{vid}.opening_percent"
                frac = 1 - (1 - m["frac"][sev_i]) * fp
                row[key] = fmt(float(r[key]) * frac, 3)

        row["Fluid.temperature_c"] = fmt(temp, 3)
        row["Cell.PUMP_01.rpm"] = str(rpm)

        row["cycle_type"] = "normal" if not cls else cls
        row["relief_event"] = "no"
        row["field_data_note"] = "normal_operation" if not cls else f"{cls}_{SEV[sev_i]}_{onset}"
        row["Cell.V_RELIEF.opening_percent"] = "0"

        row["fault_class"] = cls or "normal"
        row["severity_level"] = SEV[sev_i] if cls else "none"
        row["onset_type"] = onset
        row["fault_severity"] = fmt(fp, 3) if cls else "0"
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=1000)
    ap.add_argument("--output", default=str(ROOT / "dataset" / "realtime_input_1000cycles_test_mixed.csv"))
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args()

    cols, tmpl = load_templates("test")
    print(f"템플릿 사이클: {len(tmpl)}개 (테스트 풀, pool_seed={POOL_SEED} — 학습이 못 본 파형)")

    n_anom = a.cycles // 2
    per = n_anom // len(FAULTS)
    plan = [None] * (a.cycles - n_anom)
    for f in FAULTS:
        plan += [f] * per
    plan += [FAULTS[i % len(FAULTS)] for i in range(n_anom - per * len(FAULTS))]

    rng = random.Random(a.seed)
    rng.shuffle(plan)   # 정상/고장이 파일 안에서 섞이도록

    rows, sidx, ts = [], 1, START_TS
    for cid, cls in enumerate(plan, start=1):
        t = rng.choice(tmpl)
        cyc = make_cycle(t, cid, ts, sidx, rng, cls)
        rows.extend(cyc)
        sidx += len(cyc)
        ts += timedelta(seconds=len(cyc))

    out = Path(a.output)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols + LABEL_COLS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    cc = Counter(plan)
    print(f"-> {out}")
    print(f"   {a.cycles}사이클 / {len(rows)}행 / {len(cols) + len(LABEL_COLS)}열")
    print(f"   정상 {cc[None]}사이클 | 이상 {a.cycles - cc[None]}사이클")
    for f in FAULTS:
        print(f"     {f:14s} {cc[f]}사이클")
    nr = sum(1 for r in rows if r["fault_class"] == "normal")
    print(f"   행 기준: 정상 {nr} / 이상 {len(rows) - nr} ({(len(rows) - nr) / len(rows) * 100:.1f}%)")


if __name__ == "__main__":
    main()
