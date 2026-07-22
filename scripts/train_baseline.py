"""
final_training_dataset.csv 기반 원인 분류 베이스라인 (LightGBM, 초 단위 causal 피처).
build_dataset.py의 add_engineered_features()를 그대로 재사용 -> 실시간 서빙과 동일한 피처 계산 경로.
"""
import sys
import time
from pathlib import Path

import joblib
import lightgbm as lgb
import pandas as pd
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from sklearn.model_selection import train_test_split

sys.path.insert(0, str(Path(__file__).parent))
from build_dataset import add_engineered_features

DATA_PATH = r"C:\pipes_press\dataset\final_training_dataset_v4.csv"
MODEL_OUT = r"C:\pipes_press\models\baseline_lgbm.joblib"

NUMERIC_FEATURES = [
    "cycle_second", "temperature_c", "pump_rpm",
    "valve_cmd_pa", "valve_pos_pa", "valve_cmd_pb", "valve_pos_pb",
    "valve_cmd_at", "valve_pos_at", "valve_cmd_bt", "valve_pos_bt",
    "flow_rate_L_min", "return_flow_L_min", "pump_in_pressure_bar", "pump_out_pressure_bar",
    "cyl_cap_pressure_bar", "cyl_rod_pressure_bar", "pump_delta_pressure_bar",
    "pump_head_m", "hydraulic_power_kW", "max_velocity_m_s", "target_pressure_error_bar",
    "relief_set_pressure_bar", "target_pressure_bar",
    "feat_valve_dev_pa", "feat_valve_dev_pb", "feat_valve_dev_at", "feat_valve_dev_bt",
    "feat_valve_dev_max_pa", "feat_valve_dev_max_pb", "feat_valve_dev_max_at", "feat_valve_dev_max_bt",
    "feat_flow_per_rpm", "feat_cap_rod_corr",
    "feat_hold_pressure_decay", "feat_hold_pressure_decay_ffill", "feat_mass_balance_resid",
    "feat_target_pressure_error_max", "feat_downstroke_error_max", "feat_upstroke_error_max",
    "feat_up_down_error_diff", "feat_flow_return_diff",
]
CATEGORICAL_FEATURES = ["cycle_phase", "target_chamber"]
FEATURE_COLS = NUMERIC_FEATURES + CATEGORICAL_FEATURES
TARGET_COL = "fault_class"


def load_and_prepare():
    print("loading", DATA_PATH)
    t0 = time.time()
    df = pd.read_csv(DATA_PATH, low_memory=False)
    print(f"  {df.shape} in {time.time() - t0:.1f}s")

    print("engineering features...")
    t0 = time.time()
    df = add_engineered_features(df)
    print(f"  done in {time.time() - t0:.1f}s")

    for c in CATEGORICAL_FEATURES:
        df[c] = df[c].astype("category")

    return df


def group_stratified_split(df, test_size=0.2, seed=42):
    """cycle_id 단위로 통째로 train/val에 배정 + fault_class 층화 (같은 사이클이 양쪽에 걸치지 않음)."""
    cycle_labels = df.groupby("cycle_id")[TARGET_COL].first()
    train_cycles, val_cycles = train_test_split(
        cycle_labels.index, test_size=test_size, stratify=cycle_labels.values, random_state=seed
    )
    train_df = df[df["cycle_id"].isin(train_cycles)]
    val_df = df[df["cycle_id"].isin(val_cycles)]
    return train_df, val_df


def train(train_df, val_df):
    X_train, y_train = train_df[FEATURE_COLS], train_df[TARGET_COL]
    X_val, y_val = val_df[FEATURE_COLS], val_df[TARGET_COL]

    model = lgb.LGBMClassifier(
        objective="multiclass",
        n_estimators=500,
        learning_rate=0.05,
        num_leaves=63,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        eval_metric="multi_logloss",
        categorical_feature=CATEGORICAL_FEATURES,
        callbacks=[lgb.early_stopping(30), lgb.log_evaluation(50)],
    )
    return model


def evaluate(model, val_df):
    X_val, y_val = val_df[FEATURE_COLS], val_df[TARGET_COL]
    y_pred = model.predict(X_val)

    macro_f1 = f1_score(y_val, y_pred, average="macro")
    print(f"\nmacro F1 (row 단위, 전체 경과시간 통합): {macro_f1:.3f}")
    print("(참고: 설계문서 13.4절 kNN(7) 사이클 단위 베이스라인 11-class accuracy = 0.72)\n")

    print(classification_report(y_val, y_pred, digits=3))

    labels = sorted(y_val.unique())
    cm = pd.DataFrame(confusion_matrix(y_val, y_pred, labels=labels), index=labels, columns=labels)
    print("confusion matrix (행=실제, 열=예측):")
    print(cm)

    val_df = val_df.copy()
    val_df["_correct"] = (y_pred == val_df[TARGET_COL].values).astype(int)
    by_second = val_df.groupby("cycle_second")["_correct"].mean()
    print("\ncycle_second별 정확도 (탐지 지연 곡선):")
    print(by_second)

    return cm, by_second


def main():
    df = load_and_prepare()
    train_df, val_df = group_stratified_split(df)
    print(f"train cycles={train_df['cycle_id'].nunique()} rows={len(train_df)}")
    print(f"val   cycles={val_df['cycle_id'].nunique()} rows={len(val_df)}")

    model = train(train_df, val_df)
    evaluate(model, val_df)

    Path(MODEL_OUT).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "feature_cols": FEATURE_COLS,
                 "categorical_features": CATEGORICAL_FEATURES}, MODEL_OUT)
    print(f"\nmodel saved -> {MODEL_OUT}")


if __name__ == "__main__":
    main()
