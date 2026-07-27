"""
정상 전용 3,000사이클로 학습하는 오토인코더 이상탐지 모델 (2026-07-27 신규).

기존 `train_autoencoder.py`(릴리프 혼합 1,000사이클)와는 **별개 모델**이다. 차이:
  - 학습 데이터가 릴리프 이벤트를 전혀 포함하지 않는 정상 43,488행(3,000사이클)
  - 평가가 같은 파일 내부 분할이 아니라 **독립 테스트셋**(1,000사이클, 정상:이상 1:1, 고장 5종)
  - 따라서 "정상만 학습 -> 처음 보는 이상을 잡는가"를 처음으로 실제 측정할 수 있다

라벨은 학습에 일절 쓰지 않는다. 임계값도 val **정상** 행의 p99.5로만 정하므로 전 과정이 라벨프리다
(테스트셋 라벨은 채점과 SHAP 검증에만 쓴다).

피처: 두 CSV를 행 1:1 조인 후, 식별자/메타/라벨을 뺀 수치 전부 + `cycle_phase` 원-핫 3 +
`target_chamber` 이진. 학습 데이터에서 **분산 0인 컬럼은 자동 제외**한다 — 릴리프 계통 5열
(`V_RELIEF_*`, `C_RELIEF_velocity`, `Cell.V_RELIEF.opening_percent`)과 `N_TANK_SUCTION`,
`rated_rpm`이 여기 걸린다. 분산 0 컬럼을 남기면 StandardScaler가 scale_=1로 두어 원값이 그대로
오차에 실리는 왜곡이 생긴다(구 full 모델이 릴리프 신호에 지배당한 원인).
`Cell.V_RELIEF.set_pressure_bar_g`는 분산은 있으나 릴리프 계통이 완전히 비활성이라 물리적 결합이
없는 잡음이므로 명시적으로 제외한다.

실행: C:\anaconda3\envs\aj\python.exe scripts/train_autoencoder_normal.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import joblib
import numpy as np
import pandas as pd

ROOT = Path(r"C:\pipes_press")
DATA = ROOT / "dataset"
# --split: 템플릿 풀을 학습700/테스트195로 분리해 재생성한 데이터셋(2026-07-27).
# 분리 전 파일은 학습·테스트가 같은 895개 파형을 공유해 오탐률이 낙관적이었다.
DATASETS = {
    False: ("realtime_input_3000cycles_normal_only.csv",
            "realtime_solver_output_3000cycles_normal_only.csv",
            "realtime_input_1000cycles_test_mixed.csv",
            "realtime_solver_output_1000cycles_test_mixed.csv"),
    True: ("realtime_input_3000cycles_normal_only_split.csv",
           "realtime_solver_output_3000cycles_normal_only_split.csv",
           "realtime_input_1000cycles_test_mixed_split.csv",
           "realtime_solver_output_1000cycles_test_mixed_split.csv"),
}
TRAIN_IN = DATA / DATASETS[False][0]
TRAIN_SOL = DATA / DATASETS[False][1]
TEST_IN = DATA / DATASETS[False][2]
TEST_SOL = DATA / DATASETS[False][3]
MODEL_DIR = ROOT / "models"
MODEL_OUT = MODEL_DIR / "autoencoder_normal.keras"
BUNDLE_OUT = MODEL_DIR / "autoencoder_normal_bundle.joblib"
FIG_DIR = MODEL_DIR / "ae_normal_figures"

SEED = 42
LATENT_DIM = 8
THRESHOLD_PERCENTILE = 99.5
VAL_FRAC = 0.15

# 식별자/메타/라벨 — 피처가 될 수 없는 열
DROP_IN = ["timestamp", "sample_index", "cycle_id", "cycle_phase", "cycle_type", "relief_event",
           "System.active_mode", "field_data_note", "Press.target_chamber",
           "Cell.PUMP_01.is_running",
           "fault_class", "severity_level", "onset_type", "fault_severity"]
DROP_SOL = ["processed_at", "input_row_index", "timestamp", "cycle_id", "cycle_phase", "active_mode",
            "Press_target_pressure_bar_g", "Press_target_chamber",
            "solver_warning", "contains_inf_or_nan", "status"]
# 분산은 있으나 물리적으로 죽은 계통(릴리프 전면 비활성)이라 제외
DROP_EXTRA = ["Cell.V_RELIEF.set_pressure_bar_g"]

PHASES = ["downstroke", "pressure_hold", "upstroke"]
RATED_RPM = 1800.0
EPS = 1e-2


PUMP_SHUTOFF_M = 2900.0     # network.json PUMP_01.pump_head_shutoff_m
PUMP_QUAD = 100.0           # network.json PUMP_01.pump_head_quadratic_coeff


def add_pump_residual(f):
    """명판 펌프 곡선으로부터의 헤드 잔차: H - (2900 - 100*(Q/sr)^2)*sr^2.

    실제 설비의 펌프 상태감시에서 표준으로 쓰는 진단량이고, 측정량(헤드·유량·rpm)과
    설계 상수(명판 곡선)만으로 만들어진다 -- 고장 파라미터를 넣는 게 아니다.

    **다만 이 합성 데이터에서는 성격이 특수하다**: 솔버가 같은 곡선으로 헤드를 계산하므로
    정상 행의 잔차가 결정적으로 0이다(std 0.0092 = CSV 4자리 반올림 오차뿐).
    반면 pump_wear는 평균 -103.8. 즉 이 피처를 넣으면 pump_wear가 ~11,000σ로 떠서
    사실상 **손으로 만든 단일 고장 탐지기**가 된다 -- AE가 일반화해서 잡는 게 아니다.
    구 full 모델이 릴리프 상수 컬럼에 지배당했던 것과 구조적으로 같은 현상이라,
    기본 비활성(`--pump-residual`로 명시 활성)으로 둔다. 실제 설비라면 센서 잡음과
    곡선 드리프트 때문에 잔차 바닥이 훨씬 두꺼워져 이만한 성능은 안 나온다.
    """
    sr = (f["Cell.PUMP_01.rpm"] / RATED_RPM).clip(lower=EPS)
    q = f["calculated_flow_rate_m3_h"]
    h_nom = (PUMP_SHUTOFF_M - PUMP_QUAD * (q / sr) ** 2) * sr ** 2
    # 보압 구간은 유량 0이라 곡선 관계가 성립하지 않으므로 0으로 둔다
    f["pump_curve_residual"] = np.where(f["calculated_flow_rate_L_min"] > EPS,
                                        f["pump_head_m"] - h_nom, 0.0)
    return f


def add_derived(f):
    """물리 관계를 정규화한 파생 피처.

    원시 피처(유량/헤드/rpm)를 따로 주면 8차원 병목이 펌프 특성곡선 관계를 잡아내지 못해
    `pump_wear`(shutoff head 3~9% 감소)가 유량 자체의 큰 변동폭(σ=9.3)에 묻힌다.
    상사법칙으로 정규화하면 정상 데이터가 고정 포물선
        head_per_sr2 = 2900 - 100 * flow_per_sr^2
    위에 놓이므로, AE가 그 1차원 다양체를 학습하고 shutoff 열화는 곡선에서의 이탈로 드러난다.
    **고장 파라미터를 직접 넣는 게 아니라 측정량만으로 만든 좌표**라는 점이 중요하다
    (nameplate 곡선 상수는 실제 설비에서도 알 수 있는 설계값).

    보압 구간은 유량 0이라 유량 기반 항이 전부 0이 된다 — 정상/고장 모두 그러므로 무해하다.
    """
    sr = (f["Cell.PUMP_01.rpm"] / RATED_RPM).clip(lower=EPS)
    q_m3h = f["calculated_flow_rate_m3_h"]
    q_lpm = f["calculated_flow_rate_L_min"]
    qpos = q_lpm > EPS

    f["flow_per_sr"] = q_m3h / sr                                  # 상사 정규화 유량
    f["head_per_sr2"] = f["pump_head_m"] / (sr ** 2)               # 상사 정규화 헤드
    f["power_per_flow"] = np.where(qpos, f["hydraulic_power_kW"] / q_lpm.clip(lower=EPS), 0.0)
    loss = (f["C_SUCTION_pressure_loss_bar"] + f["C_PRESSURE_pressure_loss_bar"]
            + f["C_A_LINE_pressure_loss_bar"] + f["C_B_LINE_pressure_loss_bar"]
            + f["C_RETURN_pressure_loss_bar"])
    f["total_line_loss_bar"] = loss
    f["line_resistance"] = np.where(qpos, loss / (q_m3h.clip(lower=EPS) ** 2), 0.0)
    f["flow_to_target"] = q_lpm / f["Press.target_pressure_bar_g"].clip(lower=EPS)
    return f


def load_join(in_csv, sol_csv, derived=True, pump_resid=False):
    inp = pd.read_csv(in_csv)
    sol = pd.read_csv(sol_csv)
    assert len(inp) == len(sol), "행 수 불일치"
    assert (inp["cycle_id"].values == sol["cycle_id"].values).all(), "cycle_id 정렬 불일치"

    feats = pd.concat([inp.drop(columns=[c for c in DROP_IN if c in inp.columns]).reset_index(drop=True),
                       sol.drop(columns=[c for c in DROP_SOL if c in sol.columns]).reset_index(drop=True)],
                      axis=1)
    feats["chamber_is_cap"] = (inp["Press.target_chamber"] == "cap").astype(float)
    for p in PHASES:
        feats[f"phase_{p}"] = (inp["cycle_phase"] == p).astype(float)
    feats = feats.drop(columns=[c for c in DROP_EXTRA if c in feats.columns])
    feats = feats.astype(float)
    if derived:
        feats = add_derived(feats)
    if pump_resid:
        feats = add_pump_residual(feats)

    meta = pd.DataFrame({"cycle_id": inp["cycle_id"].values})
    for c in ["fault_class", "severity_level", "onset_type", "fault_severity"]:
        meta[c] = inp[c].values if c in inp.columns else ("normal" if c == "fault_class" else 0)
    meta["y"] = (meta["fault_class"] != "normal").astype(int)
    return feats, meta


def threshold_sweep(err_va, err_te, t):
    """라벨을 쓰지 않는 임계값 후보들의 test 성능 대조.

    후보는 전부 **val 정상 행 오차만으로** 정해진다(퍼센타일 / 최대값 배수) — 테스트 라벨로
    임계값을 고르면 비지도 전제가 깨지므로, 라벨은 결과 채점에만 쓴다.
    """
    cand = [(f"val p{p}", float(np.percentile(err_va, p)))
            for p in (99.0, 99.5, 99.9, 99.95)]
    mx = float(err_va.max())
    cand += [(f"val max x{k:g}", mx * k) for k in (1.0, 1.5, 2.0, 3.0)]

    y = t["y"].values
    rows = []
    for name, th in cand:
        pred = (t["err"].values > th).astype(int)
        tp = int(((pred == 1) & (y == 1)).sum())
        fp = int(((pred == 1) & (y == 0)).sum())
        rec = tp / max(1, int(y.sum()))
        prec = tp / max(1, tp + fp)
        cy = t.assign(p=pred).groupby("cycle_id").agg(cls=("fault_class", "first"), d=("p", "max"))
        fa = cy[cy.cls == "normal"]["d"].mean()
        det = {c: cy[cy.cls == c]["d"].mean() for c in sorted(set(t.fault_class) - {"normal"})}
        rows.append(dict(기준=name, 임계값=th, 행recall=rec, 행precision=prec,
                         F1=2 * prec * rec / max(1e-9, prec + rec),
                         사이클오경보=fa, **{c[:9]: v for c, v in det.items()}))
    df = pd.DataFrame(rows)
    print("\n--- 임계값 후보별 성능 (모두 val 정상 오차만으로 결정, 라벨 미사용) ---")
    print(df.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    return df


def build_ae(n, latent=LATENT_DIM):
    import tensorflow as tf
    from tensorflow import keras
    from tensorflow.keras import layers
    tf.random.set_seed(SEED)
    inp = keras.Input(shape=(n,))
    x = layers.Dense(64, activation="relu")(inp)
    x = layers.Dense(32, activation="relu")(x)
    z = layers.Dense(latent, activation="relu", name="latent")(x)
    x = layers.Dense(32, activation="relu")(z)
    x = layers.Dense(64, activation="relu")(x)
    out = layers.Dense(n, activation="linear")(x)
    m = keras.Model(inp, out, name="normal_ae")
    m.compile(optimizer=keras.optimizers.Adam(1e-3), loss="mse")
    return m


def recon_error(model, X, bs=2048):
    r = model.predict(X, batch_size=bs, verbose=0)
    return np.mean((X - r) ** 2, axis=1), r


def main():
    import argparse

    from sklearn.metrics import (average_precision_score, classification_report,
                                 confusion_matrix, roc_auc_score)
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler
    from tensorflow import keras

    ap = argparse.ArgumentParser()
    ap.add_argument("--no-derived", action="store_true", help="파생 피처 없이 학습(대조군)")
    ap.add_argument("--pump-residual", action="store_true",
                    help="명판 펌프곡선 잔차 피처 추가(pump_wear 전용, 성격은 add_pump_residual 참고)")
    ap.add_argument("--latent", type=int, default=LATENT_DIM, help="잠재 차원(기본 8)")
    ap.add_argument("--quiet-eval", action="store_true", help="SHAP 생략(스윕용)")
    ap.add_argument("--split", action="store_true",
                    help="템플릿 풀 분리 데이터셋 사용(학습700/테스트195, 파형 교집합 0)")
    ap.add_argument("--tag", default="", help="산출물 파일명 접미사")
    args = ap.parse_args()
    derived = not args.no_derived
    tag = args.tag or ("_base" if not derived else ("_pumpres" if args.pump_residual else ""))
    if args.latent != LATENT_DIM and not args.tag:
        tag = f"_lat{args.latent}"
    model_out = MODEL_DIR / f"autoencoder_normal{tag}.keras"
    bundle_out = MODEL_DIR / f"autoencoder_normal{tag}_bundle.joblib"
    fig_dir = MODEL_DIR / f"ae_normal_figures{tag}"

    global TRAIN_IN, TRAIN_SOL, TEST_IN, TEST_SOL
    ti, ts_, ei, es = DATASETS[args.split]
    TRAIN_IN, TRAIN_SOL, TEST_IN, TEST_SOL = DATA / ti, DATA / ts_, DATA / ei, DATA / es
    print(f"데이터셋: {'풀분리' if args.split else '기존'} | 학습 {TRAIN_IN.name} | 평가 {TEST_IN.name}")

    np.random.seed(SEED)

    # ---- 1. 학습 데이터 (전부 정상) ----
    Xtr_df, tr_meta = load_join(TRAIN_IN, TRAIN_SOL, derived, args.pump_residual)
    assert tr_meta["y"].sum() == 0, "학습 데이터에 이상 행이 있으면 안 됨"

    std = Xtr_df.std()
    dead = sorted(std[std < 1e-9].index)
    feature_cols = [c for c in Xtr_df.columns if c not in dead]
    print(f"조인: {Xtr_df.shape} | 분산 0으로 제외 {len(dead)}열: {dead}")
    print(f"최종 피처 {len(feature_cols)}개")

    cyc = tr_meta["cycle_id"].unique()
    tr_c, va_c = train_test_split(cyc, test_size=VAL_FRAC, random_state=SEED)
    is_tr = tr_meta["cycle_id"].isin(tr_c).values
    is_va = tr_meta["cycle_id"].isin(va_c).values
    print(f"train {is_tr.sum()}행({len(tr_c)}사이클) / val {is_va.sum()}행({len(va_c)}사이클)")

    scaler = StandardScaler().fit(Xtr_df.loc[is_tr, feature_cols].values)
    Xtr = scaler.transform(Xtr_df.loc[is_tr, feature_cols].values)
    Xva = scaler.transform(Xtr_df.loc[is_va, feature_cols].values)

    # ---- 2. 학습 ----
    model = build_ae(len(feature_cols), args.latent)
    es = keras.callbacks.EarlyStopping(monitor="val_loss", patience=10, restore_best_weights=True)
    model.fit(Xtr, Xtr, validation_data=(Xva, Xva), epochs=200, batch_size=256,
              callbacks=[es], verbose=2)

    err_va, _ = recon_error(model, Xva)
    threshold = float(np.percentile(err_va, THRESHOLD_PERCENTILE))
    print(f"\n임계값 = val 정상 p{THRESHOLD_PERCENTILE} = {threshold:.6f}"
          f"  (val 오차 중앙 {np.median(err_va):.6f} / 최대 {err_va.max():.6f})")

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model.save(model_out)
    joblib.dump({"scaler": scaler, "feature_cols": feature_cols, "threshold": threshold,
                 "threshold_percentile": THRESHOLD_PERCENTILE, "seed": SEED,
                 "dead_cols": dead, "train_source": TRAIN_IN.name, "derived": derived,
                 "pump_residual": args.pump_residual, "latent": args.latent}, bundle_out)
    print(f"model -> {model_out}\nbundle -> {bundle_out}")

    # ---- 3. 독립 테스트셋 평가 ----
    Xte_df, te_meta = load_join(TEST_IN, TEST_SOL, derived, args.pump_residual)
    Xte = scaler.transform(Xte_df[feature_cols].values)
    err_te, recon_te = recon_error(model, Xte)
    y = te_meta["y"].values
    pred = (err_te > threshold).astype(int)

    print(f"\n{'=' * 66}\n독립 테스트셋 평가 ({len(y)}행 / 이상 {y.sum()}행)\n{'=' * 66}")
    print(f"ROC-AUC = {roc_auc_score(y, err_te):.4f}   PR-AUC = {average_precision_score(y, err_te):.4f}")
    print(classification_report(y, pred, target_names=["normal", "anomaly"], digits=3))
    print("confusion matrix (행=실제, 열=예측):")
    print(pd.DataFrame(confusion_matrix(y, pred), index=["normal", "anomaly"],
                       columns=["normal", "anomaly"]).to_string())

    t = te_meta.copy()
    t["err"] = err_te
    t["pred"] = pred

    print("\n--- 고장 유형별 행 단위 탐지율 ---")
    rows = []
    for cls, g in t.groupby("fault_class"):
        rows.append(dict(fault_class=cls, 행수=len(g),
                         탐지율=g["pred"].mean(), 오차중앙=g["err"].median()))
    print(pd.DataFrame(rows).sort_values("탐지율", ascending=False).to_string(index=False,
          formatters={"탐지율": "{:.3f}".format, "오차중앙": "{:.5f}".format}))

    print("\n--- 심각도 x 발생방식별 탐지율 (이상 행만) ---")
    a = t[t.y == 1]
    print(pd.crosstab(a["fault_class"], [a["severity_level"], a["onset_type"]],
                      values=a["pred"], aggfunc="mean").round(3).to_string())

    print("\n--- 사이클 단위 (사이클 내 1행이라도 초과하면 탐지) ---")
    cy = t.groupby("cycle_id").agg(cls=("fault_class", "first"), det=("pred", "max"))
    cs = cy.groupby("cls")["det"].agg(["sum", "count"])
    cs["탐지율"] = cs["sum"] / cs["count"]
    cs.columns = ["탐지", "전체", "탐지율"]
    print(cs.to_string(formatters={"탐지율": "{:.3f}".format}))
    fa = cy[cy.cls == "normal"]["det"].mean()
    print(f"\n정상 사이클 오경보율: {fa:.3f}  ({int(cy[cy.cls=='normal']['det'].sum())}/{int((cy.cls=='normal').sum())})")

    # ---- 4. 임계값 트레이드오프 ----
    sweep = threshold_sweep(err_va, err_te, t)
    sweep.to_csv(fig_dir.parent / f"ae_normal{tag}_threshold_sweep.csv",
                 index=False, encoding="utf-8-sig")

    # ---- 5. SHAP: 탐지된 행 기준 + 고장 유형별 채점 ----
    if args.quiet_eval:
        return
    run_shap(model, feature_cols, Xtr, Xte, t, fig_dir)


def run_shap(model, feature_cols, Xtr, Xte, t, FIG_DIR):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import shap
    from keras import ops
    from tensorflow import keras

    class ReconErrorLayer(keras.layers.Layer):
        def call(self, tensors):
            x, r = tensors
            return ops.mean(ops.square(x - r), axis=1, keepdims=True)

    inp = keras.Input(shape=(Xtr.shape[1],))
    err = ReconErrorLayer()([inp, model(inp)])
    err_model = keras.Model(inp, err)

    det = t["pred"].values == 1
    if det.sum() == 0:
        print("\n[skip] SHAP: 탐지된 행 없음")
        return
    # 탐지 행이 너무 많으면 표본 추출 (GradientExplainer 비용)
    idx = np.where(det)[0]
    rng = np.random.default_rng(SEED)
    if len(idx) > 3000:
        idx = rng.choice(idx, 3000, replace=False)
    bg = Xtr[rng.choice(len(Xtr), 200, replace=False)]

    print(f"\nSHAP: 탐지 행 {int(det.sum())}개 중 {len(idx)}개 설명 (배경 200)")
    sv = np.array(shap.GradientExplainer(err_model, bg).shap_values(Xte[idx])).reshape(
        (len(idx), len(feature_cols)))

    FIG_DIR.mkdir(parents=True, exist_ok=True)
    mean_abs = pd.Series(np.abs(sv).mean(0), index=feature_cols).sort_values(ascending=False)
    print("\n=== SHAP 상위 15 (탐지 행 전체) ===")
    print(mean_abs.head(15).to_string())
    mean_abs.to_csv(FIG_DIR / "shap_mean_abs_detected.csv", encoding="utf-8-sig")

    for kind, fname in [(None, "shap_summary_detected.png"), ("bar", "shap_bar_detected.png")]:
        plt.figure()
        shap.summary_plot(sv, Xte[idx], feature_names=feature_cols, plot_type=kind,
                          max_display=20, show=False)
        plt.tight_layout()
        plt.savefig(FIG_DIR / fname, dpi=150)
        plt.close("all")

    # 고장 유형별 mean|SHAP| — "SHAP이 실제 주입 부위를 짚는가" 채점표
    cls = t["fault_class"].values[idx]
    by = {c: np.abs(sv[cls == c]).mean(0) for c in np.unique(cls)}
    bt = pd.DataFrame(by, index=feature_cols)
    bt.to_csv(FIG_DIR / "shap_by_fault_class.csv", encoding="utf-8-sig")
    print("\n=== 고장 유형별 SHAP 1~3위 (주입 부위와 대조) ===")
    for c in bt.columns:
        print(f"  {c:16s} {' | '.join(bt[c].nlargest(3).index)}")
    print(f"\nfigures -> {FIG_DIR}")


if __name__ == "__main__":
    main()
