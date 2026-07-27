"""
`realtime_input_1000cycles_normal_relief_mixed.csv`의 **정상 사이클만** 템플릿으로 삼아
같은 30열 포맷의 정상 전용 input CSV를 임의 사이클 수로 생성한다.

설계 결정 (2026-07-27 사용자 확정):
- 릴리프 개도 시나리오 전면 제거. `cycle_type=normal` / `relief_event=no` /
  `field_data_note=normal_operation` / `Cell.V_RELIEF.opening_percent=0` 고정이고,
  `System.active_mode`는 downstroke/pressure_hold/upstroke 3종만 나온다.
  라벨만 지우는 게 아니라 **솔버 수준에서 릴리프 분기가 비활성**이다 — 목표압 최대 227.09bar에
  jitter 상한 1.03을 곱해도 233.90bar로, 릴리프 설정압 하한 248.80bar까지 14.90bar 여유가 있다.
- jitter 폭은 `ab_solver/gen_scenarios.py`와 동일(사이클당 1회 추첨):
    목표압 x U(0.97,1.03) / 온도 + N(0,1.0) / 펌프rpm x U(0.98,1.02)
- `Node.N_CYL_CAP/ROD.pressure_bar_g`(센서 계측값)는 목표압과 **같은 비율 tj로 스케일**한다.
  원본에서 이 두 값은 솔버 재계산값과 평균 3~4bar 차이(상관 0.999)가 나는데, 같은 비율로 움직여야
  그 오프셋 구조와 상관관계가 함께 보존된다. 이 값들은 솔버 초기압으로도 쓰이므로
  pressure_hold 행에서는 N_CYL_ROD가 solver_output으로 그대로 전달된다
  (`scripts/run_origin_solver.py` 참고).
- 탱크 3노드 압력·밸브 개도·사이클 타이밍은 템플릿 값을 그대로 쓴다(gen_scenarios.py와 동일하게
  jitter 대상 아님).

실행:
  python scripts/gen_normal_realtime.py --cycles 3000 \
      --output dataset/realtime_input_3000cycles_normal_only.csv
  python scripts/gen_normal_realtime.py --selftest    # 포맷 왕복 검증(템플릿 무변형 재출력)
"""
import argparse
import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(r"C:\pipes_press")
SRC = ROOT / "dataset" / "realtime_input_1000cycles_normal_relief_mixed.csv"
SEED = 42
START_TS = datetime(2026, 7, 13, 8, 0, 0)

# 템플릿 풀 분리 (2026-07-27): 원본 895개 정상 파형을 학습용/테스트용으로 쪼갠다.
# 분리 전에는 학습셋과 테스트셋이 같은 파형 풀에서 나와 오탐률이 낙관적으로 측정됐다
# (`docs/정상학습_오토인코더_이상탐지_설계.md` §9). 이 분할은 pool_seed로만 결정되며
# 두 생성기(gen_normal_realtime / gen_anomaly_realtime)가 같은 함수를 써서 어긋날 수 없다.
POOL_SEED = 7
POOL_TRAIN_N = 700

# 컬럼별 반올림 자릿수(원본 관측치). 값이 정확히 0이면 "0"으로 쓴다(원본과 동일).
DEC = {
    "Press.target_pressure_bar_g": 3, "Fluid.temperature_c": 3,
    "Node.N_CYL_CAP.pressure_bar_g": 3, "Node.N_CYL_ROD.pressure_bar_g": 3,
    "Node.N_TANK_SUCTION.pressure_bar_g": 4, "Node.N_TANK_RETURN.pressure_bar_g": 4,
    "Node.N_TANK_RELIEF.pressure_bar_g": 4,
    "Cell.V_DIR_PA.opening_percent": 3, "Cell.V_DIR_PB.opening_percent": 3,
    "Cell.V_DIR_AT.opening_percent": 3, "Cell.V_DIR_BT.opening_percent": 3,
    "Cell.V_RELIEF.opening_percent": 3, "Cell.V_RELIEF.set_pressure_bar_g": 3,
}
# 사이클 단위 1회 추첨 jitter (gen_scenarios.py와 동일)
JIT_TARGET = (0.97, 1.03)
JIT_TEMP_SD = 1.0
JIT_RPM = (0.98, 1.02)


def fmt(v, dec):
    """원본 표기 규칙: 반올림 후 뒤따르는 0 제거, 정확히 0이면 '0'."""
    r = round(float(v), dec)
    if r == 0:
        return "0"
    s = f"{r:.{dec}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def load_templates(pool="all", pool_seed=POOL_SEED, n_train=POOL_TRAIN_N):
    """원본 CSV의 순수 정상 사이클(895개)을 템플릿으로 반환.

    pool='train'/'test'면 pool_seed로 섞어 앞 n_train개를 학습용, 나머지를 테스트용으로 준다.
    두 집합은 **파형이 완전히 겹치지 않는다** — 이래야 오탐률이 정직하게 측정된다.
    """
    rows = list(csv.DictReader(open(SRC, encoding="utf-8-sig")))
    cols = list(rows[0].keys())
    types, byc = {}, {}
    for r in rows:
        byc.setdefault(r["cycle_id"], []).append(r)
        types.setdefault(r["cycle_id"], set()).add(r["cycle_type"])
    tmpl = [byc[c] for c in byc if types[c] == {"normal"}]
    if pool == "all":
        return cols, tmpl
    idx = list(range(len(tmpl)))
    random.Random(pool_seed).shuffle(idx)
    keep = idx[:n_train] if pool == "train" else idx[n_train:]
    return cols, [tmpl[i] for i in sorted(keep)]


def make_cycle(tmpl, cid, ts, sidx, rng):
    """템플릿 사이클 1개 -> jitter 적용된 새 사이클 행들."""
    tj = rng.uniform(*JIT_TARGET)
    tempj = rng.gauss(0.0, JIT_TEMP_SD)
    rpmj = rng.uniform(*JIT_RPM)
    out = []
    for si, r in enumerate(tmpl):
        n = dict(r)
        n["timestamp"] = (ts + timedelta(seconds=si)).strftime("%Y-%m-%d %H:%M:%S")
        n["sample_index"] = str(sidx + si)
        n["cycle_id"] = str(cid)
        n["cycle_second"] = str(si)

        n["Press.target_pressure_bar_g"] = fmt(float(r["Press.target_pressure_bar_g"]) * tj, 3)
        # 센서 계측값도 같은 비율로 (사용자 선택 (a)) -- 솔버 초기압으로도 전달됨
        n["Node.N_CYL_CAP.pressure_bar_g"] = fmt(float(r["Node.N_CYL_CAP.pressure_bar_g"]) * tj, 3)
        n["Node.N_CYL_ROD.pressure_bar_g"] = fmt(float(r["Node.N_CYL_ROD.pressure_bar_g"]) * tj, 3)
        n["Fluid.temperature_c"] = fmt(float(r["Fluid.temperature_c"]) + tempj, 3)
        n["Cell.PUMP_01.rpm"] = str(int(float(r["Cell.PUMP_01.rpm"]) * rpmj))

        # 릴리프 전면 제거 + 정상 라벨 고정
        n["cycle_type"] = "normal"
        n["relief_event"] = "no"
        n["field_data_note"] = "normal_operation"
        n["Cell.V_RELIEF.opening_percent"] = "0"
        out.append(n)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=3000)
    ap.add_argument("--output", default=str(ROOT / "dataset" / "realtime_input_3000cycles_normal_only.csv"))
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--pool", choices=["all", "train", "test"], default="train",
                    help="템플릿 풀. train=앞 700개(기본), test=나머지 195개, all=895개 전부")
    ap.add_argument("--selftest", action="store_true",
                    help="템플릿을 무변형 재출력해 포맷 왕복이 원본과 일치하는지 확인")
    a = ap.parse_args()

    cols, tmpl = load_templates(a.pool)
    print(f"정상 템플릿 사이클: {len(tmpl)}개 (pool={a.pool}, seed={POOL_SEED})")

    if a.selftest:
        return selftest(cols, tmpl)

    rng = random.Random(a.seed)
    rows, sidx, ts = [], 1, START_TS
    for cid in range(1, a.cycles + 1):
        t = rng.choice(tmpl)
        cyc = make_cycle(t, cid, ts, sidx, rng)
        rows.extend(cyc)
        sidx += len(cyc)
        ts += timedelta(seconds=len(cyc))

    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    print(f"-> {out}")
    print(f"   {a.cycles}사이클 / {len(rows)}행 / {len(cols)}열 "
          f"| 템플릿 재사용 평균 {a.cycles / len(tmpl):.2f}회")
    tg = [float(r["Press.target_pressure_bar_g"]) for r in rows]
    rs = [float(r["Cell.V_RELIEF.set_pressure_bar_g"]) for r in rows]
    print(f"   목표압 {min(tg):.2f}~{max(tg):.2f} bar | 릴리프 설정압 하한 {min(rs):.2f} bar "
          f"-> 여유 {min(rs) - max(tg):.2f} bar")


def selftest(cols, tmpl):
    """jitter 없이 템플릿을 그대로 다시 써서 원본 문자열과 일치하는지 확인 = 포맷 규칙 검증."""
    bad = 0
    checked = 0
    for cyc in tmpl:
        for r in cyc:
            for c, d in DEC.items():
                checked += 1
                if fmt(r[c], d) != r[c]:
                    if bad < 10:
                        print(f"  불일치 {c}: 원본 {r[c]!r} -> 재출력 {fmt(r[c], d)!r}")
                    bad += 1
    print(f"포맷 왕복 검증: {checked}개 값 중 불일치 {bad}개")


if __name__ == "__main__":
    main()
