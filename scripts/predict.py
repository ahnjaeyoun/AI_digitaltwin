"""
학습된 계층형 모델(models/hierarchical_lgbm.joblib)로 추론.

사용 예:
  # final_training_dataset.csv와 같은 스키마의 CSV가 있을 때
  python predict.py --data some_dataset.csv

  # raw 2파일(명령/시나리오 + solver_output)을 직접 넣을 때 (build_dataset.py와 동일 로직 재사용)
  python predict.py --cmd input_timeseries.csv --solver solver_output_timeseries.csv
"""
import argparse
import sys
from pathlib import Path

import joblib
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from build_dataset import (
    add_engineered_features, apply_label_defaults, load_command_source, load_solver_output, merge_sources,
)

MODEL_PATH = r"C:\pipes_press\models\hierarchical_lgbm.joblib"


def load_model(path=MODEL_PATH):
    return joblib.load(path)


def prepare_features(df, categorical_features):
    df = add_engineered_features(df)
    for c in categorical_features:
        df[c] = df[c].astype("category")
    return df


def predict(bundle, df):
    feature_cols = bundle["feature_cols"]
    stage1, stage2 = bundle["stage1"], bundle["stage2"]
    coarse_label = bundle["coarse_label"]

    coarse_pred = stage1.predict(df[feature_cols]).astype(object)
    final_pred = coarse_pred.copy()
    leak_mask = coarse_pred == coarse_label
    if leak_mask.any():
        final_pred[leak_mask] = stage2.predict(df.loc[leak_mask, feature_cols])
    return final_pred


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", help="final_training_dataset.csv와 동일 스키마의 CSV")
    ap.add_argument("--cmd", help="raw 명령/시나리오 파일 (--solver와 함께 사용)")
    ap.add_argument("--solver", help="raw solver_output 파일 (--cmd와 함께 사용)")
    ap.add_argument("--out", help="예측 결과 저장 경로 (미지정 시 콘솔 출력만)")
    args = ap.parse_args()

    bundle = load_model()

    if args.data:
        df = pd.read_csv(args.data, low_memory=False)
    elif args.cmd and args.solver:
        cmd_df = load_command_source(args.cmd)
        solver_df = load_solver_output(args.solver)
        df = merge_sources(cmd_df, solver_df)
        df = apply_label_defaults(df)
    else:
        raise SystemExit("--data 또는 (--cmd, --solver) 조합으로 입력을 지정하세요")

    df = prepare_features(df, bundle["categorical_features"])
    df["predicted_fault_class"] = predict(bundle, df)

    cols = ["cycle_id", "cycle_second", "cycle_phase", "predicted_fault_class"]
    if args.out:
        df[cols + (["fault_class"] if "fault_class" in df.columns else [])].to_csv(args.out, index=False)
        print(f"저장 완료 -> {args.out}")
    else:
        print(df[cols].to_string(index=False))

    if "fault_class" in df.columns:
        acc = (df["predicted_fault_class"] == df["fault_class"]).mean()
        print(f"\n실제 라벨 대비 정확도: {acc:.3f} ({len(df)}행)")


if __name__ == "__main__":
    main()
