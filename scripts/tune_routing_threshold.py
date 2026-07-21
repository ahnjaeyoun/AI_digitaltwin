"""
stage1->stage2 라우팅을 argmax(1등만 통과) 대신 확률 임계값 기반으로 바꿔서 성능이 개선되는지 스윕.
재학습 불필요 — 이미 학습된 models/hierarchical_lgbm.joblib으로 추론만 다시 함(빠름).

가설: relief_early는 stage1에서 "leak_family가 1등"까지는 잘 안 되지만, 확률 자체는 어느 정도 올라가 있을
수 있다. 임계값을 낮춰 "1등이 아니어도 leak_family 확률이 threshold 이상이면 stage2로 보낸다"로 바꾸면
recall이 오를 수 있다. 대신 원래 normal/기타였던 행이 잘못 stage2로 끌려가 오답이 될 위험(정밀도 하락)과의
트레이드오프.
"""
import sys
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import f1_score, precision_recall_fscore_support

sys.path.insert(0, str(Path(__file__).parent))
from build_dataset import add_engineered_features
from train_baseline import DATA_PATH, FEATURE_COLS, CATEGORICAL_FEATURES, TARGET_COL, group_stratified_split

MODEL_PATH = r"C:\pipes_press\models\hierarchical_lgbm.joblib"
THRESHOLDS = [0.5, 0.3, 0.2, 0.15, 0.10, 0.07, 0.05, 0.03]


def load_val():
    print("loading + engineering...")
    t0 = time.time()
    df = pd.read_csv(DATA_PATH, low_memory=False)
    df = add_engineered_features(df)
    for c in CATEGORICAL_FEATURES:
        df[c] = df[c].astype("category")
    _, val_df = group_stratified_split(df)
    print(f"  done in {time.time() - t0:.1f}s, val rows={len(val_df)}")
    return val_df


def predict_cascade_threshold(bundle, df, threshold):
    stage1, stage2 = bundle["stage1"], bundle["stage2"]
    coarse_label = bundle["coarse_label"]
    feature_cols = bundle["feature_cols"]

    proba = stage1.predict_proba(df[feature_cols])
    classes = list(stage1.classes_)
    leak_idx = classes.index(coarse_label)

    hard_pred = np.array(classes)[proba.argmax(axis=1)].astype(object)
    leak_mask = proba[:, leak_idx] >= threshold

    final_pred = hard_pred.copy()
    final_pred[leak_mask] = stage2.predict(df.loc[leak_mask, feature_cols])
    # threshold 미만인데 원래 argmax가 leak_family였던 경우는 hard_pred가 이미 leak_family 문자열이므로
    # stage2로 보정 안 되면 잘못된 라벨이 남는다 -> 그런 행은 stage2로 강제 라우팅
    still_leak = (~leak_mask) & (hard_pred == coarse_label)
    if still_leak.any():
        final_pred[still_leak] = stage2.predict(df.loc[still_leak, feature_cols])
    return final_pred, leak_mask | still_leak


def main():
    bundle = joblib.load(MODEL_PATH)
    val_df = load_val()
    y_val = val_df[TARGET_COL].values

    rows = []
    for th in THRESHOLDS:
        pred, leak_mask = predict_cascade_threshold(bundle, val_df, th)
        macro_f1 = f1_score(y_val, pred, average="macro")
        p, r, f, _ = precision_recall_fscore_support(y_val, pred, labels=["relief_early", "normal"], zero_division=0)
        rows.append({
            "threshold": th,
            "cascade_macro_f1": round(macro_f1, 4),
            "routed_to_stage2_pct": round(leak_mask.mean() * 100, 1),
            "relief_early_precision": round(p[0], 3), "relief_early_recall": round(r[0], 3),
            "normal_precision": round(p[1], 3), "normal_recall": round(r[1], 3),
        })
        print(rows[-1])

    result = pd.DataFrame(rows)
    print("\n=== 임계값 스윕 결과 ===")
    print(result.to_string(index=False))
    best = result.loc[result["cascade_macro_f1"].idxmax()]
    print(f"\n최고 macro F1: threshold={best['threshold']} -> {best['cascade_macro_f1']}")


if __name__ == "__main__":
    main()
