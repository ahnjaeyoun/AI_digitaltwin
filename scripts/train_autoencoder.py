"""
realtime 2파일(input + solver_output, 1000사이클 normal/relief_mixed) 기반
오토인코더 이상탐지 + SHAP 설명 파이프라인.

- 학습: relief_event=no 행 전부(train 사이클)로 Keras 대칭 MLP AE 학습 (정상만 학습)
- 탐지: 재구성오차(MSE) > 임계값(val 정상 99.5퍼센타일) -> 이상
- 평가: 행 단위(relief_event=yes 250행이 양성) ROC-AUC/PR-AUC/F1 + 사이클 단위 탐지율
- SHAP: 재구성오차 스칼라를 출력하는 래퍼 모델에 GradientExplainer 적용
- 라벨 유출 방지: cycle_type/relief_event/field_data_note/System.active_mode(active_mode)는
  'relief' 값이 라벨과 100% 일치하므로 피처에서 제외 (검증 완료)

실행: C:\anaconda3\envs\aj\python.exe scripts/train_autoencoder.py
        [--mode full|hard] [--shap-target detected|label|both]
  --mode hard: 릴리프 계통 신호 7컬럼(RELIEF_SIGNAL_COLS) 제외, 간접 신호만으로 탐지.
  산출물은 *_hard 접미사로 분리 저장(full 모델 보존).
  --shap-target: SHAP 설명 대상 행의 선택 기준.
    detected(기본) = 재구성오차 > 임계값으로 '모델이 이상이라 판정한' 행. 라벨을 쓰지 않으므로
      실제 운영에서 그대로 재현 가능한 산출물이며, 오탐 행도 포함돼 '왜 헛경보를 냈는가'가 함께 나온다
      (field_data_note='normal_operation' 열이 shap_by_fault_type에 추가로 생김).
    label = 실제 relief_event=yes 행. 라벨이 있어야만 만들 수 있는 사후 검증용.
    both = 둘 다 산출해 대조. 파일명은 항상 _{target} 접미사로 분리된다.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import joblib
import numpy as np
import pandas as pd

INPUT_CSV = r"C:\pipes_press\dataset\realtime_input_1000cycles_normal_relief_mixed.csv"
SOLVER_CSV = r"C:\pipes_press\dataset\realtime_solver_output_1000cycles_normal_relief_mixed.csv"
MODEL_DIR = Path(r"C:\pipes_press\models")

# --mode hard: 릴리프 계통 신호(사실상 "릴리프가 열렸다"를 직접 알려주는 컬럼)를 제외하고
# 압력·유량 등 간접 신호만으로 탐지하는 어려운 버전. 실제 설비에 릴리프 밸브 센서가 없는
# (단순 기계식 밸브) 상황을 가정한 문제 설정.
RELIEF_SIGNAL_COLS = [
    "Cell.V_RELIEF.opening_percent", "Cell.V_RELIEF.set_pressure_bar_g",
    "Node.N_TANK_RELIEF.pressure_bar_g",
    "V_RELIEF_velocity_m_s", "C_RELIEF_velocity_m_s",
    "V_RELIEF_pressure_loss_bar", "V_RELIEF_Kv_effective_m3_h",
]

SEED = 42
LATENT_DIM = 8
THRESHOLD_PERCENTILE = 99.5

# ---------------------------------------------------------------------------
# 1. 데이터 로드 / 조인 / 피처 구성
# ---------------------------------------------------------------------------

# input CSV에서 쓸 수치 피처 (라벨·식별자·상수 제외)
INPUT_NUMERIC = [
    "cycle_second", "total_cycle_time_s", "down_time_s", "pressure_hold_time_s", "rise_time_s",
    "Press.target_pressure_bar_g", "Fluid.temperature_c",
    "Node.N_CYL_CAP.pressure_bar_g", "Node.N_CYL_ROD.pressure_bar_g",
    "Node.N_TANK_RETURN.pressure_bar_g", "Node.N_TANK_RELIEF.pressure_bar_g",
    "Cell.PUMP_01.rpm",
    "Cell.V_DIR_PA.opening_percent", "Cell.V_DIR_PB.opening_percent",
    "Cell.V_DIR_AT.opening_percent", "Cell.V_DIR_BT.opening_percent",
    "Cell.V_RELIEF.opening_percent", "Cell.V_RELIEF.set_pressure_bar_g",
]
# 제외: timestamp/sample_index(식별자), cycle_type/relief_event/field_data_note(라벨),
#       System.active_mode('relief' 값이 라벨과 동일), Cell.PUMP_01.is_running(상수 yes),
#       Cell.PUMP_01.rated_rpm/Node.N_TANK_SUCTION.pressure_bar_g(상수)

# solver CSV에서 제외할 컬럼 (나머지 수치 전부 사용)
SOLVER_EXCLUDE = [
    "processed_at", "input_row_index", "timestamp", "cycle_id",
    "cycle_phase", "active_mode",              # active_mode='relief'가 라벨과 동일
    "Press_target_pressure_bar_g",             # input의 target_pressure와 중복
    "Press_target_chamber",                    # input의 target_chamber와 중복
    "solver_warning", "contains_inf_or_nan", "status",  # 메타/상수
]

LABEL_COLS = ["cycle_id", "cycle_type", "relief_event", "field_data_note", "cycle_phase"]


def load_and_join():
    inp = pd.read_csv(INPUT_CSV)
    sol = pd.read_csv(SOLVER_CSV)
    assert len(inp) == len(sol) and (inp["cycle_id"].values == sol["cycle_id"].values).all(), \
        "input/solver 행 정렬 불일치"

    sol_feats = sol.drop(columns=[c for c in SOLVER_EXCLUDE if c in sol.columns])
    df = pd.concat([inp[LABEL_COLS + INPUT_NUMERIC + ["Press.target_chamber"]].reset_index(drop=True),
                    sol_feats.reset_index(drop=True)], axis=1)

    # 범주형 -> 수치: cycle_phase 원-핫(3), target_chamber 이진
    df["chamber_is_cap"] = (df["Press.target_chamber"] == "cap").astype(float)
    for ph in ["downstroke", "pressure_hold", "upstroke"]:
        df[f"phase_{ph}"] = (df["cycle_phase"] == ph).astype(float)
    df = df.drop(columns=["Press.target_chamber"])

    feature_cols = [c for c in df.columns if c not in LABEL_COLS]
    df["y"] = (df["relief_event"] == "yes").astype(int)
    return df, feature_cols


def split_cycles(df, seed=SEED):
    """사이클 단위 70/15/15 분할, relief_mixed 비율 층화."""
    from sklearn.model_selection import train_test_split
    cyc = df.groupby("cycle_id")["cycle_type"].first()
    train_c, rest_c = train_test_split(cyc.index, test_size=0.30, stratify=cyc.values, random_state=seed)
    rest_labels = cyc.loc[rest_c]
    val_c, test_c = train_test_split(rest_c, test_size=0.50, stratify=rest_labels.values, random_state=seed)
    return (df[df.cycle_id.isin(train_c)], df[df.cycle_id.isin(val_c)], df[df.cycle_id.isin(test_c)])


# ---------------------------------------------------------------------------
# 2. 오토인코더
# ---------------------------------------------------------------------------

def build_autoencoder(n_features):
    import tensorflow as tf
    from tensorflow import keras
    from tensorflow.keras import layers

    tf.random.set_seed(SEED)
    inputs = keras.Input(shape=(n_features,))
    x = layers.Dense(64, activation="relu")(inputs)
    x = layers.Dense(32, activation="relu")(x)
    z = layers.Dense(LATENT_DIM, activation="relu", name="latent")(x)
    x = layers.Dense(32, activation="relu")(z)
    x = layers.Dense(64, activation="relu")(x)
    outputs = layers.Dense(n_features, activation="linear")(x)
    model = keras.Model(inputs, outputs, name="relief_ae")
    model.compile(optimizer=keras.optimizers.Adam(1e-3), loss="mse")
    return model


def reconstruction_error(model, X, batch_size=1024):
    recon = model.predict(X, batch_size=batch_size, verbose=0)
    return np.mean((X - recon) ** 2, axis=1), recon


# ---------------------------------------------------------------------------
# 3. 평가
# ---------------------------------------------------------------------------

def evaluate(df_test, err_test, threshold):
    from sklearn.metrics import (average_precision_score, classification_report,
                                 confusion_matrix, roc_auc_score)
    y = df_test["y"].values
    pred = (err_test > threshold).astype(int)

    print(f"\n=== test 평가 (행 단위, 양성={y.sum()}행 / 전체 {len(y)}행) ===")
    print(f"ROC-AUC = {roc_auc_score(y, err_test):.4f}")
    print(f"PR-AUC  = {average_precision_score(y, err_test):.4f}")
    print(f"threshold(={threshold:.6f}) 적용 시:")
    print(classification_report(y, pred, target_names=["normal", "relief"], digits=3))
    cm = confusion_matrix(y, pred)
    print("confusion matrix (행=실제, 열=예측):")
    print(pd.DataFrame(cm, index=["normal", "relief"], columns=["normal", "relief"]))

    # 사이클 단위: relief_mixed 사이클에서 relief 행을 1개라도 잡으면 적중
    t = df_test.copy()
    t["pred"] = pred
    cyc = t.groupby("cycle_id").agg(is_relief=("y", "max"), detected=("pred", "max"),
                                    hit=("y", lambda s: 0), )
    relief_cyc = t[t.y == 1].groupby("cycle_id")["pred"].max()
    normal_cyc = t.groupby("cycle_id")["y"].max() == 0
    fp_cyc = t[t.cycle_id.isin(normal_cyc[normal_cyc].index)].groupby("cycle_id")["pred"].max()
    print(f"\n사이클 단위: relief 사이클 탐지 {int(relief_cyc.sum())}/{len(relief_cyc)}"
          f" | 정상 사이클 오탐 {int(fp_cyc.sum())}/{len(fp_cyc)}")
    return pred


# ---------------------------------------------------------------------------
# 4. SHAP
# ---------------------------------------------------------------------------

def run_shap(model, scaler, feature_cols, X_bg, X_pos, df_pos, out_dir, tag):
    """재구성오차 스칼라 출력 래퍼 모델에 GradientExplainer 적용.

    tag: 설명 대상 선택 기준('detected'/'label'). 산출 파일명 접미사로 쓰여 두 기준의
    결과가 서로 덮어쓰이지 않는다.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import shap
    from tensorflow import keras
    from keras import ops

    class ReconErrorLayer(keras.layers.Layer):
        def call(self, tensors):
            x, recon = tensors
            return ops.mean(ops.square(x - recon), axis=1, keepdims=True)

    inputs = keras.Input(shape=(X_bg.shape[1],))
    recon = model(inputs)
    err = ReconErrorLayer()([inputs, recon])
    err_model = keras.Model(inputs, err)

    explainer = shap.GradientExplainer(err_model, X_bg)
    shap_vals = explainer.shap_values(X_pos)
    sv = np.array(shap_vals).reshape(X_pos.shape)  # (n_pos, n_features)

    out_dir.mkdir(parents=True, exist_ok=True)

    plt.figure()
    shap.summary_plot(sv, X_pos, feature_names=feature_cols, max_display=20, show=False)
    plt.tight_layout()
    plt.savefig(out_dir / f"shap_summary_{tag}.png", dpi=150)
    plt.close("all")

    plt.figure()
    shap.summary_plot(sv, X_pos, feature_names=feature_cols, plot_type="bar",
                      max_display=20, show=False)
    plt.tight_layout()
    plt.savefig(out_dir / f"shap_bar_{tag}.png", dpi=150)
    plt.close("all")

    mean_abs = pd.Series(np.abs(sv).mean(axis=0), index=feature_cols).sort_values(ascending=False)
    print(f"\n=== SHAP 상위 15 피처 (target={tag}, 재구성오차 기여, mean|SHAP|) ===")
    print(mean_abs.head(15).to_string())

    # 행 유형별 mean|SHAP| 비교. target=detected면 오탐 행의 'normal_operation' 열이 함께 나와
    # "정상을 이상으로 오판할 때 무슨 피처가 원인인가"를 볼 수 있다.
    note = df_pos["field_data_note"].values
    by_type = {}
    for t in np.unique(note):
        by_type[t] = np.abs(sv[note == t]).mean(axis=0)
    type_df = pd.DataFrame(by_type, index=feature_cols)
    type_df = type_df.loc[mean_abs.head(15).index]
    type_df.to_csv(out_dir / f"shap_by_fault_type_{tag}.csv", encoding="utf-8-sig")
    print("\n행 유형별 mean|SHAP| 상위 15 ->", out_dir / f"shap_by_fault_type_{tag}.csv")

    mean_abs.to_csv(out_dir / f"shap_mean_abs_{tag}.csv", encoding="utf-8-sig")
    return sv, mean_abs


def per_feature_error_plot(X_pos, recon_pos, X_norm, recon_norm, feature_cols, out_dir):
    """보조 설명: 피처별 재구성오차 분해 (relief 행 vs 정상 행)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    err_pos = ((X_pos - recon_pos) ** 2).mean(axis=0)
    err_norm = ((X_norm - recon_norm) ** 2).mean(axis=0)
    s = pd.DataFrame({"relief": err_pos, "normal": err_norm}, index=feature_cols)
    s = s.sort_values("relief", ascending=False).head(20)

    fig, ax = plt.subplots(figsize=(9, 7))
    s[::-1].plot.barh(ax=ax)
    ax.set_xlabel("mean squared reconstruction error (scaled space)")
    ax.set_title("Per-feature reconstruction error: relief vs normal (test)")
    fig.tight_layout()
    fig.savefig(out_dir / "per_feature_recon_error.png", dpi=150)
    plt.close(fig)
    s.to_csv(out_dir / "per_feature_recon_error.csv", encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    import argparse

    from sklearn.preprocessing import StandardScaler
    from tensorflow import keras

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["full", "hard"], default="full",
                        help="full=전체 피처(기본), hard=릴리프 계통 신호 제외")
    parser.add_argument("--shap-target", choices=["detected", "label", "both"], default="detected",
                        help="SHAP 설명 대상: detected=모델이 이상 판정한 행(기본, 라벨 미사용), "
                             "label=실제 relief 행, both=둘 다")
    args = parser.parse_args()
    suffix = "" if args.mode == "full" else "_hard"
    model_out = MODEL_DIR / f"autoencoder_relief{suffix}.keras"
    bundle_out = MODEL_DIR / f"autoencoder_relief{suffix}_bundle.joblib"
    fig_dir = MODEL_DIR / f"ae_figures{suffix}"

    np.random.seed(SEED)

    df, feature_cols = load_and_join()
    if args.mode == "hard":
        feature_cols = [c for c in feature_cols if c not in RELIEF_SIGNAL_COLS]
    print(f"joined: {df.shape}, mode={args.mode}, features={len(feature_cols)}")

    train_df, val_df, test_df = split_cycles(df)
    for name, d in [("train", train_df), ("val", val_df), ("test", test_df)]:
        print(f"{name}: cycles={d.cycle_id.nunique()} rows={len(d)} relief_rows={d.y.sum()}")

    # 정상(relief_event=no) 행만 학습 -- 사용자 결정: relief_mixed 사이클의 비릴리프 행 포함
    train_norm = train_df[train_df.y == 0]
    val_norm = val_df[val_df.y == 0]

    scaler = StandardScaler().fit(train_norm[feature_cols].values)
    Xtr = scaler.transform(train_norm[feature_cols].values)
    Xva_norm = scaler.transform(val_norm[feature_cols].values)
    Xva_all = scaler.transform(val_df[feature_cols].values)
    Xte = scaler.transform(test_df[feature_cols].values)

    model = build_autoencoder(len(feature_cols))
    model.summary()
    es = keras.callbacks.EarlyStopping(monitor="val_loss", patience=10, restore_best_weights=True)
    model.fit(Xtr, Xtr, validation_data=(Xva_norm, Xva_norm),
              epochs=200, batch_size=256, callbacks=[es], verbose=2)

    # 임계값: val 정상 행 재구성오차의 99.5퍼센타일
    err_va_norm, _ = reconstruction_error(model, Xva_norm)
    threshold = float(np.percentile(err_va_norm, THRESHOLD_PERCENTILE))

    # 참고: val 전체에서 F1 최적 임계값
    from sklearn.metrics import precision_recall_curve
    err_va_all, _ = reconstruction_error(model, Xva_all)
    prec, rec, thr = precision_recall_curve(val_df.y.values, err_va_all)
    f1 = 2 * prec * rec / np.clip(prec + rec, 1e-12, None)
    best = np.argmax(f1[:-1])
    print(f"\nthreshold (val normal p{THRESHOLD_PERCENTILE}) = {threshold:.6f}")
    print(f"참고: val F1 최적 threshold = {thr[best]:.6f} (F1={f1[best]:.3f})")

    err_te, recon_te = reconstruction_error(model, Xte)
    evaluate(test_df, err_te, threshold)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model.save(model_out)
    joblib.dump({"scaler": scaler, "feature_cols": feature_cols, "threshold": threshold,
                 "threshold_percentile": THRESHOLD_PERCENTILE, "seed": SEED,
                 "mode": args.mode}, bundle_out)
    print(f"\nmodel -> {model_out}\nbundle -> {bundle_out}")

    # SHAP: 배경 = train 정상 행 200개 샘플
    X_all = scaler.transform(df[feature_cols].values)
    err_all, _ = reconstruction_error(model, X_all)
    rng = np.random.default_rng(SEED)
    bg_idx = rng.choice(len(Xtr), size=min(200, len(Xtr)), replace=False)

    # 설명 대상 마스크. detected는 라벨을 전혀 쓰지 않고 모델 판정만으로 고르므로
    # 분류->설명이 실제로 직렬 연결된다(운영에서 그대로 재현 가능).
    masks = {}
    if args.shap_target in ("detected", "both"):
        masks["detected"] = err_all > threshold
    if args.shap_target in ("label", "both"):
        masks["label"] = df.y.values == 1

    # 대상 집합이 어느 분할에서 왔는지 보고 (train 행은 학습에 쓰여 오차가 낮아 오탐이 과소평가됨)
    split_of = pd.Series("train", index=df.index)
    split_of[df.cycle_id.isin(val_df.cycle_id.unique())] = "val"
    split_of[df.cycle_id.isin(test_df.cycle_id.unique())] = "test"

    for tag, mask in masks.items():
        n = int(mask.sum())
        if n == 0:
            print(f"\n[skip] SHAP target='{tag}': 대상 행 0개")
            continue
        n_true = int((mask & (df.y.values == 1)).sum())
        print(f"\n=== SHAP target='{tag}': {n}행 (실제 relief {n_true} / 오탐 {n - n_true}) ===")
        print(pd.crosstab(split_of[mask], np.where(df.loc[mask, "y"].values == 1, "relief행", "정상행"),
                          rownames=["split"], colnames=["실제"]).to_string())
        run_shap(model, scaler, feature_cols, Xtr[bg_idx], X_all[mask], df[mask], fig_dir, tag)

    # 피처별 재구성오차 분해 (test)
    te_pos = test_df.y.values == 1
    per_feature_error_plot(Xte[te_pos], recon_te[te_pos], Xte[~te_pos], recon_te[~te_pos],
                           feature_cols, fig_dir)
    print(f"\nfigures -> {fig_dir}")


if __name__ == "__main__":
    main()
