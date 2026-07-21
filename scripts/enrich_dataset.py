"""
final_training_dataset.csv에 빠진 릴리프 set압/목표압 컬럼을 원본 소스(scenarios.csv)에서 되살려 붙인다.

배경: 설계문서 7.1절이 "릴리프 set압", "목표압"을 지령 피처(모델 입력 가능)로 명시하지만
final_training_dataset.csv의 45열 스키마에는 빠져 있었음. dataset/gen_output.csv(솔버 원출력)와
C:\\press_dataes\\scenarios.csv(시나리오 생성 메타데이터)가 row_id("{cycle_id}_{cycle_second}") 기준으로
final_training_dataset.csv와 완전히 동일한 행 집합임을 검증 완료(유량 컬럼 교차검증, 최대오차 0.0005).

v_relief_set 값은 클래스별로 거의 결정적: 대부분 250.0(비활성), relief_early는 166.6~228.0(낮춰짐),
relief_stuck은 999.0(사실상 무효화 센티널). 사용자 확인 결과 컨트롤러가 실시간으로 아는 지령값(측정 불가능한
gt_ 정답이 아님) — 안전하게 피처로 사용 가능.

출력: dataset/final_training_dataset_v2.csv (원본은 보존)
"""
import pandas as pd

FINAL_PATH = r"C:\pipes_press\dataset\final_training_dataset.csv"
SCENARIOS_PATH = r"C:\press_dataes\scenarios.csv"
OUT_PATH = r"C:\pipes_press\dataset\final_training_dataset_v2.csv"


def main():
    print("loading final_training_dataset.csv...")
    final = pd.read_csv(FINAL_PATH, low_memory=False)
    final["row_id"] = final["cycle_id"].astype(str) + "_" + final["cycle_second"].astype(str)

    print("loading scenarios.csv (relief_set_pressure_bar, target_pressure_bar만)...")
    sc = pd.read_csv(SCENARIOS_PATH, usecols=["row_id", "v_relief_set", "target_pressure"])
    sc = sc.rename(columns={"v_relief_set": "relief_set_pressure_bar", "target_pressure": "target_pressure_bar"})

    merged = final.merge(sc, on="row_id", how="left", validate="one_to_one")
    n_missing = merged["relief_set_pressure_bar"].isna().sum()
    if n_missing:
        raise SystemExit(f"병합 실패: {n_missing}행이 scenarios.csv와 매칭 안 됨")
    if len(merged) != len(final):
        raise SystemExit(f"행 수 불일치: final={len(final)} merged={len(merged)}")

    print("클래스별 relief_set_pressure_bar 분포 (검증):")
    print(merged.groupby("fault_class")["relief_set_pressure_bar"].agg(["min", "max", "mean"]))

    merged = merged.drop(columns=["row_id"])
    merged.to_csv(OUT_PATH, index=False)
    print(f"\n저장 완료 -> {OUT_PATH} ({len(merged):,}행, {len(merged.columns)}열)")


if __name__ == "__main__":
    main()
