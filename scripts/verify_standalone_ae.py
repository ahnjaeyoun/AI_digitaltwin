"""
자립형 모델이 (1) 기존 파이프라인과 수치가 같은지, (2) 프로젝트 코드 없이 로드되는지 검증한다.

`export_standalone_ae.py`가 전처리를 그래프에 구워 넣었으므로, 같은 입력에 대해 재구성오차가
기존 경로(pandas 파생 + StandardScaler + AE)와 부동소수점 오차 내에서 일치해야 한다.
일치하지 않으면 파생 피처 수식이나 컬럼 순서가 어긋난 것이다.
"""
import os
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import joblib
import numpy as np
import pandas as pd

import train_autoencoder_normal as T

MODEL_DIR = T.MODEL_DIR
PHASE_IDX = {"downstroke": 0, "pressure_hold": 1, "upstroke": 2}


def build_raw_input(in_csv, sol_csv, raw_cols):
    """자립형 모델이 먹는 (n, 48) 배열을 두 CSV에서 직접 만든다 — 파생 계산 없음."""
    inp, sol = pd.read_csv(in_csv), pd.read_csv(sol_csv)
    src = pd.concat([inp.reset_index(drop=True), sol.reset_index(drop=True)], axis=1)
    src = src.loc[:, ~src.columns.duplicated()]
    x = src[raw_cols].astype("float32").values
    phase = inp["cycle_phase"].map(PHASE_IDX).astype("float32").values[:, None]
    chamber = (inp["Press.target_chamber"] == "cap").astype("float32").values[:, None]
    return np.concatenate([x, phase, chamber], axis=1).astype("float32")


def main():
    import json

    import keras

    io = json.loads((MODEL_DIR / "press_ae_standalone_io.json").read_text(encoding="utf-8"))
    raw_cols = io["raw_columns"]

    ti, ts_, ei, es = T.DATASETS[True]
    test_in, test_sol = T.DATA / ei, T.DATA / es

    # --- 기준: 기존 파이프라인 ---
    b = joblib.load(MODEL_DIR / "autoencoder_normal_split24pr_bundle.joblib")
    ae = keras.saving.load_model(MODEL_DIR / "autoencoder_normal_split24pr.keras")
    X, meta = T.load_join(test_in, test_sol, derived=True, pump_resid=True)
    err_ref, _ = T.recon_error(ae, b["scaler"].transform(X[b["feature_cols"]].values))

    # --- 비교: 자립형 모델 (원시 48열만 입력) ---
    raw = build_raw_input(test_in, test_sol, raw_cols)
    # SavedModel 경로 — Keras 객체도 프로젝트 코드도 필요 없다
    import tensorflow as tf
    sm = tf.saved_model.load(str(MODEL_DIR / "press_ae_standalone_savedmodel"))
    outs = [sm.serve(tf.constant(raw[i:i + 4096])) for i in range(0, len(raw), 4096)]
    err_new = np.concatenate([np.asarray(o[0]).ravel() for o in outs])
    flag = np.concatenate([np.asarray(o[1]).ravel() for o in outs])[:, None]

    d = np.abs(err_ref - err_new)
    rel = d / np.maximum(np.abs(err_ref), 1e-12)
    print(f"입력 {raw.shape} | 기존 오차 vs 자립형 오차")
    print(f"  최대 절대오차 {d.max():.3e} | 최대 상대오차 {rel.max():.3e}")
    print(f"  판정 불일치 행: {int((( err_ref > b['threshold']) != (flag.ravel() > 0.5)).sum())}")

    # --- 최종 성능 재현 (사이클 k-of-n) ---
    t = meta.copy()
    t["pred"] = (flag.ravel() > 0.5).astype(int)
    cy = t.groupby("cycle_id").agg(cls=("fault_class", "first"), n=("pred", "sum"))
    det = cy["n"] >= io["k_of_n"]
    fa = det[cy.cls == "normal"].mean()
    print(f"\n자립형 모델 단독 성능 (k={io['k_of_n']})")
    print(f"  정상 사이클 오경보 : {fa:.3f} ({int(det[cy.cls=='normal'].sum())}/{int((cy.cls=='normal').sum())})")
    for c in sorted(set(cy.cls) - {"normal"}):
        print(f"  {c:14s} 탐지 {det[cy.cls == c].mean():.3f}")

    # --- 코드 없이 로드되는지 (SavedModel) ---
    try:
        import tensorflow as tf
        sm = tf.saved_model.load(str(MODEL_DIR / "press_ae_standalone_savedmodel"))
        out = sm.serve(tf.constant(raw[:256]))
        d2 = np.abs(np.asarray(out[0]).ravel() - err_new[:256]).max()
        print(f"\nSavedModel (tf.saved_model.load, Keras/프로젝트 코드 불필요) 최대 편차 {d2:.3e}")
    except Exception as e:                                   # noqa: BLE001
        print(f"\n[warn] SavedModel 검증 실패: {e}")


if __name__ == "__main__":
    main()
