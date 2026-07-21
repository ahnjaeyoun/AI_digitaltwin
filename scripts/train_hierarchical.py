"""
2단계 계층형 분류기.
stage1: 8-way coarse (normal/valve_stuck/overheat/relief_stuck/suction_clog/pump_wear/line_clog
        + 누설계열을 leak_family 하나로 묶음)
stage2: leak_family로 예측된 경우에만 발동하는 4-way fine (ext_leak/seal_leak/valve_int_leak/relief_early)

설계문서 13.4/13.5절이 지적한 중첩 그룹(누설 3종 + relief_early) 대응.
train_baseline.py의 v1/v2 플랫 베이스라인과 동일 피처·동일 split으로 비교 가능하게 구성.
"""
import sys
import time
from pathlib import Path

import joblib
import lightgbm as lgb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, confusion_matrix, f1_score, precision_recall_fscore_support

sys.path.insert(0, str(Path(__file__).parent))
from build_dataset import add_engineered_features
from train_baseline import DATA_PATH, FEATURE_COLS, CATEGORICAL_FEATURES, TARGET_COL, group_stratified_split

MODEL_OUT = r"C:\pipes_press\models\hierarchical_lgbm.joblib"
PLOT_DIR = r"C:\pipes_press\models\eval_plots"

LEAK_FAMILY = ["ext_leak", "seal_leak", "valve_int_leak", "relief_early"]
COARSE_LABEL = "leak_family"

CLASS_ORDER = [
    "normal", "valve_stuck", "overheat", "relief_stuck", "suction_clog", "pump_wear",
    "line_clog", "ext_leak", "seal_leak", "valve_int_leak", "relief_early",
]

matplotlib.rcParams["font.family"] = "Malgun Gothic"
matplotlib.rcParams["axes.unicode_minus"] = False

LGB_PARAMS = dict(objective="multiclass", n_estimators=3000, learning_rate=0.05,
                   num_leaves=63, random_state=42, n_jobs=-1)


def to_coarse(y):
    y = pd.Series(y).reset_index(drop=True)
    return y.where(~y.isin(LEAK_FAMILY), COARSE_LABEL)


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


def train_stage1(train_df, val_df):
    y_train = to_coarse(train_df[TARGET_COL])
    y_val = to_coarse(val_df[TARGET_COL])
    model = lgb.LGBMClassifier(**LGB_PARAMS)
    model.fit(
        train_df[FEATURE_COLS], y_train,
        eval_set=[(val_df[FEATURE_COLS], y_val)],
        eval_metric="multi_logloss",
        categorical_feature=CATEGORICAL_FEATURES,
        callbacks=[lgb.early_stopping(30), lgb.log_evaluation(50)],
    )
    return model


def train_stage2(train_df, val_df):
    tr = train_df[train_df[TARGET_COL].isin(LEAK_FAMILY)]
    va = val_df[val_df[TARGET_COL].isin(LEAK_FAMILY)]
    model = lgb.LGBMClassifier(**LGB_PARAMS)
    model.fit(
        tr[FEATURE_COLS], tr[TARGET_COL],
        eval_set=[(va[FEATURE_COLS], va[TARGET_COL])],
        eval_metric="multi_logloss",
        categorical_feature=CATEGORICAL_FEATURES,
        callbacks=[lgb.early_stopping(30), lgb.log_evaluation(50)],
    )
    return model


def predict_cascade(stage1, stage2, df):
    coarse_pred = stage1.predict(df[FEATURE_COLS]).astype(object)
    final_pred = coarse_pred.copy()
    leak_mask = coarse_pred == COARSE_LABEL
    if leak_mask.any():
        final_pred[leak_mask] = stage2.predict(df.loc[leak_mask, FEATURE_COLS])
    return final_pred


def evaluate(stage1, stage2, val_df):
    y_val = val_df[TARGET_COL].values
    y_pred = predict_cascade(stage1, stage2, val_df)

    macro_f1 = f1_score(y_val, y_pred, average="macro")
    print(f"\n[계층형 cascade] macro F1 (row 단위, 실제 배포 방식대로 stage1 예측에 따라 라우팅): {macro_f1:.3f}")
    print(classification_report(y_val, y_pred, digits=3))

    labels = sorted(pd.unique(y_val))
    cm = pd.DataFrame(confusion_matrix(y_val, y_pred, labels=labels), index=labels, columns=labels)
    print("confusion matrix (행=실제, 열=예측):")
    print(cm)

    coarse_true = to_coarse(y_val)
    coarse_pred = stage1.predict(val_df[FEATURE_COLS])
    print("\n[stage1 단독] coarse(8-way) macro F1:", f1_score(coarse_true, coarse_pred, average="macro"))
    print(classification_report(coarse_true, coarse_pred, digits=3))

    leak_val = val_df[val_df[TARGET_COL].isin(LEAK_FAMILY)]
    if len(leak_val):
        fine_pred = stage2.predict(leak_val[FEATURE_COLS])
        print("\n[stage2 단독, oracle routing 기준] fine(4-way) macro F1:",
              f1_score(leak_val[TARGET_COL], fine_pred, average="macro"))
        print(classification_report(leak_val[TARGET_COL], fine_pred, digits=3))

    return cm, y_val, y_pred


def plot_confusion_matrix(y_val, y_pred, out_path):
    cm = confusion_matrix(y_val, y_pred, labels=CLASS_ORDER)
    cm_df = pd.DataFrame(cm, index=CLASS_ORDER, columns=CLASS_ORDER)
    cm_norm = cm_df.div(cm_df.sum(axis=1), axis=0).fillna(0)

    n = len(CLASS_ORDER)
    fig, ax = plt.subplots(figsize=(0.75 * n + 2.5, 0.75 * n + 2))
    im = ax.imshow(cm_norm.values, cmap="Blues", vmin=0, vmax=1)

    ax.set_xticks(range(n))
    ax.set_xticklabels(CLASS_ORDER, rotation=45, ha="right")
    ax.set_yticks(range(n))
    ax.set_yticklabels(CLASS_ORDER)
    ax.set_xlabel("예측 (predicted)")
    ax.set_ylabel("실제 (actual)")
    ax.set_title("혼동행렬 (행 기준 정규화)")

    for i in range(n):
        for j in range(n):
            frac = cm_norm.values[i, j]
            count = cm_df.values[i, j]
            if count == 0:
                continue
            color = "white" if frac > 0.55 else "black"
            weight = "bold" if i == j else "normal"
            ax.text(j, i, f"{count}\n{frac*100:.0f}%", ha="center", va="center",
                     color=color, fontsize=8, fontweight=weight)

    fig.colorbar(im, ax=ax, label="행(실제) 기준 비율")
    fig.tight_layout()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"confusion matrix plot saved -> {out_path}")


def plot_class_metrics(y_val, y_pred, out_path):
    p, r, f, support = precision_recall_fscore_support(y_val, y_pred, labels=CLASS_ORDER, zero_division=0)
    per_class = pd.DataFrame({"precision": p, "recall": r, "f1": f, "support": support}, index=CLASS_ORDER)
    order = per_class.sort_values("recall").index.tolist()
    per_class = per_class.loc[order]

    x = np.arange(len(order))
    width = 0.26
    fig, ax = plt.subplots(figsize=(max(8, 0.7 * len(order)), 5))
    ax.bar(x - width, per_class["precision"], width, label="정밀도(precision)", color="#2a78d6")
    ax.bar(x, per_class["recall"], width, label="재현율(recall)", color="#008300")
    ax.bar(x + width, per_class["f1"], width, label="F1", color="#e87ba4")

    ax.set_xticks(x)
    ax.set_xticklabels(order, rotation=45, ha="right")
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("score")
    ax.set_title("클래스별 정밀도·재현율·F1 (재현율 오름차순)")
    ax.legend(loc="lower right")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    fig.tight_layout()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"class metrics plot saved -> {out_path}")


def main():
    df = load_and_prepare()
    train_df, val_df = group_stratified_split(df)
    print(f"train cycles={train_df['cycle_id'].nunique()} rows={len(train_df)}")
    print(f"val   cycles={val_df['cycle_id'].nunique()} rows={len(val_df)}")

    print("\n=== stage1 학습: coarse(8-way) ===")
    stage1 = train_stage1(train_df, val_df)

    print("\n=== stage2 학습: fine(leak_family, 4-way) ===")
    stage2 = train_stage2(train_df, val_df)

    _, y_val, y_pred = evaluate(stage1, stage2, val_df)

    plot_confusion_matrix(y_val, y_pred, str(Path(PLOT_DIR) / "confusion_matrix.png"))
    plot_class_metrics(y_val, y_pred, str(Path(PLOT_DIR) / "class_metrics.png"))

    Path(MODEL_OUT).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {"stage1": stage1, "stage2": stage2, "feature_cols": FEATURE_COLS,
         "categorical_features": CATEGORICAL_FEATURES, "leak_family": LEAK_FAMILY, "coarse_label": COARSE_LABEL},
        MODEL_OUT,
    )
    print(f"\nmodel saved -> {MODEL_OUT}")


if __name__ == "__main__":
    main()
