"""
realtime_input 형식 CSV를 `origin_solver`로 풀어 realtime_solver_output 형식(45열) CSV를 만든다.

`origin_solver/main.cpp`는 배치 드라이버가 아니라 `network.json` 하나를 읽어 단일 운전점을
풀고 사람이 읽는 텍스트를 stdout으로 뱉는다. 따라서 이 스크립트가 바깥에서
  행 -> network.json 재작성 -> origin.exe 실행 -> 텍스트 파싱 -> 45열 행
을 반복한다(솔버 소스는 건드리지 않는다).

원본 재현 검증(2026-07-27): input의 `Node.N_TANK_{SUCTION,RETURN,RELIEF}.pressure_bar_g`를
**솔버 경계조건으로 반드시 물려줘야** 원본 `realtime_solver_output`과 일치한다. 이걸 빼면
리턴 계통(N_VALVE_B/N_VALVE_T/N_CYL_ROD)이 탱크 리턴압만큼 균일하게 어긋난다.
※ ab_solver의 scenarios.csv 스키마에는 이 경계압 필드가 없어 gen_static.exe로는 재현 불가.

실행:
  python scripts/run_origin_solver.py --input <in.csv> --output <out.csv> [--jobs N]
  python scripts/run_origin_solver.py --validate      # 원본 1000사이클로 재현 검증
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:\pipes_press")
SOLVER_DIR = ROOT / "origin_solver"
BUILD_DIR = Path(tempfile.gettempdir()) / "pipes_press_origin_build"

NODES_OUT = ["N_PUMP_IN", "N_PUMP_OUT", "N_VALVE_P", "N_VALVE_A",
             "N_VALVE_B", "N_VALVE_T", "N_CYL_CAP", "N_CYL_ROD"]
VELS = ["C_SUCTION", "C_PRESSURE", "C_A_LINE", "C_B_LINE", "C_RETURN", "V_RELIEF", "C_RELIEF"]
LOSSES = ["C_SUCTION", "C_PRESSURE", "C_A_LINE", "C_B_LINE", "C_RETURN", "V_RELIEF"]
KVS = ["V_DIR_PA", "V_DIR_PB", "V_DIR_AT", "V_DIR_BT", "V_RELIEF"]
# mode -> 목표압이 걸리는 노드 (network.json modes[*].load_node)
LOAD_NODE = {"downstroke": "N_CYL_CAP", "pressure_hold": "N_CYL_CAP", "upstroke": "N_CYL_ROD"}

OUT_COLS = (["processed_at", "input_row_index", "timestamp", "cycle_id", "cycle_phase", "active_mode",
             "Press_target_pressure_bar_g", "Press_target_chamber", "target_pressure_error_bar",
             "load_pressure_bar_g", "calculated_flow_rate_L_min", "calculated_flow_rate_m3_h",
             "hydraulic_power_kW", "pump_head_m", "pump_delta_pressure_bar"]
            + [f"{n}_pressure_bar_g" for n in NODES_OUT]
            + [f"{v}_velocity_m_s" for v in VELS] + ["max_active_velocity_m_s"]
            + [f"{c}_pressure_loss_bar" for c in LOSSES]
            + [f"{v}_Kv_effective_m3_h" for v in KVS]
            + ["solver_warning", "contains_inf_or_nan", "status"])


def build_exe():
    """origin_solver/main.cpp를 정적 링크로 빌드(이미 있으면 재사용)."""
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    exe = BUILD_DIR / "origin.exe"
    src, hdr = SOLVER_DIR / "main.cpp", SOLVER_DIR / "json.hpp"
    if exe.exists() and exe.stat().st_mtime > src.stat().st_mtime:
        return exe
    shutil.copy2(src, BUILD_DIR)
    shutil.copy2(hdr, BUILD_DIR)
    r = subprocess.run(["g++", "-O2", "-std=c++20", "-static", "main.cpp", "-o", "origin.exe"],
                       cwd=BUILD_DIR, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"origin_solver 빌드 실패:\n{r.stderr[:2000]}")
    return exe


def fnum(row, key, default=0.0):
    try:
        return float(row[key])
    except (KeyError, ValueError, TypeError):
        return default


def make_network(base, row):
    """input 한 행의 운전점 + 탱크 경계압을 network.json 구조에 심는다."""
    d = json.loads(base)
    mode = row["System.active_mode"]
    d["active_mode"] = mode
    d["press"]["target_pressure_bar_g"] = fnum(row, "Press.target_pressure_bar_g")
    d["fluid"]["temperature_c"] = fnum(row, "Fluid.temperature_c")

    # input의 Node.* 5개는 전부 솔버 경계/초기압이다 (원본 재현의 핵심).
    #  - 탱크 3노드: 경계조건. 빼면 리턴 계통이 탱크 리턴압만큼 균일하게 어긋난다.
    #  - 실린더 2노드: 초기값. downstroke/upstroke에서는 솔버가 물리로 덮어쓰지만,
    #    pressure_hold는 supply/return path가 비어 있어 로드 반대편(N_CYL_ROD)을
    #    계산하지 않고 이 값을 그대로 내보낸다.
    bnd = {"N_TANK_SUCTION": fnum(row, "Node.N_TANK_SUCTION.pressure_bar_g"),
           "N_TANK_RETURN": fnum(row, "Node.N_TANK_RETURN.pressure_bar_g"),
           "N_TANK_RELIEF": fnum(row, "Node.N_TANK_RELIEF.pressure_bar_g"),
           "N_CYL_CAP": fnum(row, "Node.N_CYL_CAP.pressure_bar_g"),
           "N_CYL_ROD": fnum(row, "Node.N_CYL_ROD.pressure_bar_g")}
    for n in d["nodes"]:
        if n["id"] in bnd:
            n["pressure_bar_g"] = bnd[n["id"]]

    for c in d["cells"]:
        cid = c["id"]
        if cid == "PUMP_01":
            c["rpm"] = fnum(row, "Cell.PUMP_01.rpm")
            c["rated_rpm"] = fnum(row, "Cell.PUMP_01.rated_rpm", 1800.0)
            c["is_running"] = row.get("Cell.PUMP_01.is_running", "yes") == "yes"
        elif cid in ("V_DIR_PA", "V_DIR_PB", "V_DIR_AT", "V_DIR_BT"):
            c["opening_percent"] = fnum(row, f"Cell.{cid}.opening_percent")
        elif cid == "V_RELIEF":
            c["opening_percent"] = fnum(row, "Cell.V_RELIEF.opening_percent")
            c["set_pressure_bar_g"] = fnum(row, "Cell.V_RELIEF.set_pressure_bar_g", 250.0)

    apply_fault(d, row)
    return d


# network.json 내부 파라미터로만 주입되는 고장(input 컬럼에 자리가 없는 것).
# valve_stuck/overheat는 input의 밸브 개도·온도 컬럼에 이미 열화값이 들어 있어 여기서 다루지 않는다.
# 배율은 ab_solver/gen_scenarios.py의 MECH와 동일, 적용식도 gen.cpp:946-949와 같다.
FAULT_MECH = {
    "pump_wear":    {"scale": [0.97, 0.94, 0.91]},
    "suction_clog": {"cell": "C_SUCTION", "mult": [10, 22, 40]},
    "line_clog":    {"cell": "C_PRESSURE", "mult": [7, 16, 30], "rough": [15, 45, 90]},
}
SEV_IDX = {"initial": 0, "moderate": 1, "severe": 2}


def _mul_cell(d, cid, key, factor):
    for c in d["cells"]:
        if c["id"] == cid and key in c:
            c[key] = c[key] * factor
            return


def apply_fault(d, row):
    """라벨 열(fault_class/severity_level/fault_severity)이 있으면 해당 고장을 network에 주입."""
    cls = row.get("fault_class", "normal")
    if cls in ("normal", "", None) or cls not in FAULT_MECH:
        return
    si = SEV_IDX.get(row.get("severity_level", ""), None)
    if si is None:
        return
    fp = fnum(row, "fault_severity", 1.0)     # 사이클 내 고장 진행도 0~1
    m = FAULT_MECH[cls]
    if cls == "pump_wear":
        _mul_cell(d, "PUMP_01", "pump_head_shutoff_m", 1 - (1 - m["scale"][si]) * fp)
    else:
        _mul_cell(d, m["cell"], "local_loss_k", 1 + (m["mult"][si] - 1) * fp)
        if "rough" in m:
            _mul_cell(d, m["cell"], "roughness_m", 1 + (m["rough"][si] - 1) * fp)


RE_FLOW = re.compile(r"Calculated flow:\s*([-\d.eE+]+)\s*L/min\s*\(([-\d.eE+]+)\s*m3/h\)")
RE_NODE = re.compile(r"^(\S+)\s+\[.*?\]\s+elev=.*?P=([-\d.eE+]+)\s*bar\(g\)")
RE_VEL = re.compile(r"velocity=([-\d.eE+]+)\s*m/s")
RE_LOSS = re.compile(r"pressure_loss=([-\d.eE+]+)\s*bar")
RE_KV = re.compile(r"Kv_effective=([-\d.eE+]+)\s*m3/h")
RE_PUMP = re.compile(r"pump_head=([-\d.eE+]+)\s*m,\s*deltaP=([-\d.eE+]+)\s*bar")


def parse_output(text):
    """origin_solver의 텍스트 출력에서 필요한 물리량을 뽑는다."""
    nodeP, vel, loss, kv = {}, {}, {}, {}
    flow_lpm = flow_m3h = phead = pdelta = 0.0
    for line in text.splitlines():
        m = RE_FLOW.search(line)
        if m:
            flow_lpm, flow_m3h = float(m.group(1)), float(m.group(2))
            continue
        m = RE_NODE.match(line)
        if m:
            nodeP[m.group(1)] = float(m.group(2))
            continue
        # 셀 라인: "<ID> [type/...] active=..., ..."
        if "[" in line and "active=" in line:
            cid = line.split(None, 1)[0]
            if cid == "PUMP_01":
                mp = RE_PUMP.search(line)
                if mp:
                    phead, pdelta = float(mp.group(1)), float(mp.group(2))
            mv, ml, mk = RE_VEL.search(line), RE_LOSS.search(line), RE_KV.search(line)
            if mv:
                vel[cid] = float(mv.group(1))
            if ml:
                loss[cid] = float(ml.group(1))
            if mk:
                kv[cid] = float(mk.group(1))
    warn = "yes" if "--- Solver warning ---" in text or "iteration limit reached" in text else "no"
    return dict(flow_lpm=flow_lpm, flow_m3h=flow_m3h, phead=phead, pdelta=pdelta,
                nodeP=nodeP, vel=vel, loss=loss, kv=kv, warn=warn)


_WORKER = {}


def _init_worker():
    """워커마다 전용 작업 디렉터리(+exe 사본). origin.exe가 CWD의 network.json을 읽기 때문.
    빌드는 풀 생성 전에 부모가 마쳐 두므로 여기서는 재사용만 한다(동시 빌드 시 소스 손상)."""
    exe = BUILD_DIR / "origin.exe"
    wd = Path(tempfile.mkdtemp(prefix="origin_w"))
    shutil.copy2(exe, wd / "origin.exe")
    _WORKER["wd"] = wd
    _WORKER["exe"] = wd / "origin.exe"
    _WORKER["base"] = (SOLVER_DIR / "network.json").read_text(encoding="utf-8")


def solve_row(args):
    i, row = args
    wd, exe, base = _WORKER["wd"], _WORKER["exe"], _WORKER["base"]
    net = make_network(base, row)
    (wd / "network.json").write_text(json.dumps(net, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([str(exe)], cwd=wd, capture_output=True, text=True)
    p = parse_output(r.stdout)

    mode = row["System.active_mode"]
    tgt = fnum(row, "Press.target_pressure_bar_g")
    loadp = p["nodeP"].get(LOAD_NODE.get(mode, "N_CYL_CAP"), 0.0)
    q_m3s = p["flow_lpm"] / 60000.0
    power = p["pdelta"] * 1e5 * q_m3s / 1000.0            # gen.cpp:963
    terr = abs(tgt - loadp)                                # gen.cpp:964
    vmax = max((p["vel"].get(v, 0.0) for v in VELS), default=0.0)

    vals = ([row["timestamp"], row["cycle_id"], row["cycle_phase"], mode,
             f"{tgt:.4f}", row["Press.target_chamber"], f"{terr:.4f}", f"{loadp:.4f}",
             f"{p['flow_lpm']:.4f}", f"{p['flow_m3h']:.4f}", f"{power:.4f}",
             f"{p['phead']:.4f}", f"{p['pdelta']:.4f}"]
            + [f"{p['nodeP'].get(n, 0.0):.4f}" for n in NODES_OUT]
            + [f"{p['vel'].get(v, 0.0):.4f}" for v in VELS] + [f"{vmax:.4f}"]
            + [f"{p['loss'].get(c, 0.0):.4f}" for c in LOSSES]
            + [f"{p['kv'].get(v, 0.0):.4f}" for v in KVS])
    bad = "yes" if any(x in ("nan", "inf", "-nan", "-inf") for x in vals) else "no"
    status = "ok" if r.returncode == 0 and bad == "no" else "error"
    return i, [None, str(i + 1)] + vals + [p["warn"], bad, status]


def run(rows, jobs):
    import multiprocessing as mp
    from datetime import datetime
    build_exe()   # 워커 생성 전에 단일 프로세스로 빌드 완료
    with mp.Pool(jobs, initializer=_init_worker) as pool:
        out = [None] * len(rows)
        done = 0
        for i, rec in pool.imap_unordered(solve_row, enumerate(rows), chunksize=64):
            out[i] = rec
            done += 1
            if done % 2000 == 0 or done == len(rows):
                print(f"  {done}/{len(rows)}", file=sys.stderr, flush=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for rec in out:
        rec[0] = stamp
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input")
    ap.add_argument("--output")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--limit", type=int, default=0, help="앞 N행만 처리(디버그)")
    ap.add_argument("--validate", action="store_true",
                    help="원본 1000사이클 input/solver_output으로 재현 검증")
    a = ap.parse_args()

    if a.validate:
        return validate(a.jobs, a.limit or 300)

    rows = list(csv.DictReader(open(a.input, encoding="utf-8-sig")))
    if a.limit:
        rows = rows[:a.limit]
    print(f"입력 {len(rows)}행 | 워커 {a.jobs}개", file=sys.stderr)
    recs = run(rows, a.jobs)
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(OUT_COLS)
        w.writerows(recs)
    print(f"-> {a.output}  ({len(recs)}행 x {len(OUT_COLS)}열)", file=sys.stderr)


def validate(jobs, n):
    """원본 input을 다시 풀어 원본 solver_output과 열별 최대 오차를 비교."""
    ind = ROOT / "dataset" / "realtime_input_1000cycles_normal_relief_mixed.csv"
    sod = ROOT / "dataset" / "realtime_solver_output_1000cycles_normal_relief_mixed.csv"
    rows = list(csv.DictReader(open(ind, encoding="utf-8-sig")))
    ref = list(csv.DictReader(open(sod, encoding="utf-8-sig")))
    # 정상 행만 (릴리프 개도 행은 생성 대상이 아니므로 제외)
    idx = [i for i, r in enumerate(rows) if r["relief_event"] == "no"][:n]
    sub = [rows[i] for i in idx]
    print(f"검증: 정상 {len(sub)}행 재현", file=sys.stderr)
    recs = run(sub, jobs)

    num_cols = [c for c in OUT_COLS if c not in
                ("processed_at", "input_row_index", "timestamp", "cycle_id", "cycle_phase",
                 "active_mode", "Press_target_chamber", "solver_warning",
                 "contains_inf_or_nan", "status")]
    worst = {}
    for k, rec in enumerate(recs):
        got = dict(zip(OUT_COLS, rec))
        exp = ref[idx[k]]
        for c in num_cols:
            d = abs(float(got[c]) - float(exp[c]))
            if d > worst.get(c, (-1, None))[0]:
                worst[c] = (d, k)
    print("\n=== 열별 최대 절대오차 (원본 대비) ===")
    for c, (d, k) in sorted(worst.items(), key=lambda x: -x[1][0]):
        flag = "OK" if d < 5e-4 else ("~" if d < 5e-3 else "DIFF")
        print("  %-34s %.6f   %s" % (c, d, flag))
    mx = max(d for d, _ in worst.values())
    print(f"\n전체 최대 오차: {mx:.6f}")


if __name__ == "__main__":
    main()
