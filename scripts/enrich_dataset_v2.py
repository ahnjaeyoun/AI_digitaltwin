"""
gen.cpp 리턴라인 누설 반영 수정(ab_solver) 이후 재생성된 dataset/gen_output_v2.csv에서
return_flow_L_min(리턴라인 유량, C_RETURN 유속을 배관 단면적으로 환산)을 뽑아
final_training_dataset_v2.csv에 붙여 v3을 만든다.

배경: ext_leak <-> valve_int_leak 상호 혼동(v8까지 recall 0.62/0.86)의 원인이 솔버가
리턴라인 유량에 누설 효과를 전혀 반영하지 않던 것으로 확인됨(트러블슈팅 로그 #10).
gen.cpp의 solve()를 수정(leak_to_가 비어있으면 외부누설로 간주해 리턴경로에는 누설분을 뺀
기본유량만 반영)하고 전체 10만 사이클을 재생성 -> ext_leak만 펌프-리턴 유량차가 벌어짐(평균 3.98,
최대 15.1bar), 나머지 10클래스는 변화 없음(~1e-7 수준, 정합성 확인됨).
"""
import pandas as pd
import numpy as np

FINAL_PATH = r"C:\pipes_press\dataset\final_training_dataset_v2.csv"
GEN_OUTPUT_V2_PATH = r"C:\pipes_press\dataset\gen_output_v2.csv"
OUT_PATH = r"C:\pipes_press\dataset\final_training_dataset_v3.csv"

C_RETURN_DIAMETER_M = 0.016  # network.json의 C_RETURN 배관 직경


def main():
    print("loading final_training_dataset_v2.csv...")
    final = pd.read_csv(FINAL_PATH, low_memory=False)
    final["row_id"] = final["cycle_id"].astype(str) + "_" + final["cycle_second"].astype(str)

    print("loading gen_output_v2.csv (vel_C_RETURN만)...")
    go = pd.read_csv(GEN_OUTPUT_V2_PATH, usecols=["row_id", "vel_C_RETURN"])
    area = np.pi * (C_RETURN_DIAMETER_M / 2) ** 2
    go["return_flow_L_min"] = go["vel_C_RETURN"] * area * 60000
    go = go.drop(columns=["vel_C_RETURN"])

    merged = final.merge(go, on="row_id", how="left", validate="one_to_one")
    n_missing = merged["return_flow_L_min"].isna().sum()
    if n_missing:
        raise SystemExit(f"병합 실패: {n_missing}행이 gen_output_v2.csv와 매칭 안 됨")
    if len(merged) != len(final):
        raise SystemExit(f"행 수 불일치: final={len(final)} merged={len(merged)}")

    print("\n클래스별 펌프유량-리턴유량 차이 (검증, ext_leak만 벌어져야 정상):")
    diff = merged["flow_rate_L_min"] - merged["return_flow_L_min"]
    print(diff.groupby(merged["fault_class"]).agg(["mean", "min", "max"]))

    merged = merged.drop(columns=["row_id"])
    merged.to_csv(OUT_PATH, index=False)
    print(f"\n저장 완료 -> {OUT_PATH} ({len(merged):,}행, {len(merged.columns)}열)")


if __name__ == "__main__":
    main()
