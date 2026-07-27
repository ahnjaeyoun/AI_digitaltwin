"""
학습된 AE의 **후처리 파라미터**만 최적화한다(재학습 없음).

세 가지 손잡이를 격자 탐색한다:
  1) 임계값 — val 정상 오차만으로 결정(퍼센타일 / 최대값 배수). 라벨 미사용.
  2) 사이클 판정 k-of-n — 사이클 안에서 임계 초과 행이 **k개 이상**일 때만 이상 사이클로 본다.
     현재 규칙은 k=1(1행이라도 초과). 오탐은 사이클당 소수 행만 산발적으로 튀는 반면 진짜
     고장은 여러 행이 연속으로 튀므로, k를 올리면 탐지를 크게 잃지 않고 오경보만 깎을 수 있다.
  3) 위상별 임계값 — `pressure_hold`는 유량이 0이라 오차 분포가 근본적으로 다르다.
     위상별로 val 정상 분위수를 따로 잡으면 위상 간 척도 차이를 흡수한다.

라벨은 **채점에만** 쓴다 — 임계값·k는 전부 val 정상 행에서만 유도되므로 비지도 전제가 유지된다.

실행: python scripts/tune_ae_postproc.py [--tag ""|"_pumpres"]
"""
import argparse
import os
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import joblib
import numpy as np
import pandas as pd

import train_autoencoder_normal as T

FAULTS = ["line_clog", "overheat", "pump_wear", "suction_clog", "valve_stuck"]


def cycle_eval(t, over, k):
    """사이클 내 초과 행이 k개 이상이면 이상 사이클로 판정."""
    g = t.assign(o=over.astype(int)).groupby("cycle_id").agg(
        cls=("fault_class", "first"), n=("o", "sum"))
    det = (g["n"] >= k)
    fa = det[g.cls == "normal"].mean()
    per = {c: det[g.cls == c].mean() for c in FAULTS}
    return fa, per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="")
    ap.add_argument("--split", action="store_true", help="템플릿 풀 분리 데이터셋 사용")
    a = ap.parse_args()

    ti, ts_, ei, es = T.DATASETS[a.split]
    T.TRAIN_IN, T.TRAIN_SOL = T.DATA / ti, T.DATA / ts_
    T.TEST_IN, T.TEST_SOL = T.DATA / ei, T.DATA / es

    b = joblib.load(T.MODEL_DIR / f"autoencoder_normal{a.tag}_bundle.joblib")
    scaler, cols = b["scaler"], b["feature_cols"]
    derived, presid = b.get("derived", True), b.get("pump_residual", False)

    import keras
    model = keras.saving.load_model(T.MODEL_DIR / f"autoencoder_normal{a.tag}.keras")

    # val 정상 오차 재현 (임계값 후보의 유일한 근거)
    Xtr_df, tr_meta = T.load_join(T.TRAIN_IN, T.TRAIN_SOL, derived, presid)
    from sklearn.model_selection import train_test_split
    cyc = tr_meta["cycle_id"].unique()
    _, va_c = train_test_split(cyc, test_size=T.VAL_FRAC, random_state=T.SEED)
    is_va = tr_meta["cycle_id"].isin(va_c).values
    err_va, _ = T.recon_error(model, scaler.transform(Xtr_df.loc[is_va, cols].values))
    va_phase = np.where(Xtr_df.loc[is_va, "phase_pressure_hold"].values > 0.5, "hold", "flow")

    Xte_df, te_meta = T.load_join(T.TEST_IN, T.TEST_SOL, derived, presid)
    err_te, _ = T.recon_error(model, scaler.transform(Xte_df[cols].values))
    t = te_meta.copy()
    t["err"] = err_te
    te_phase = np.where(Xte_df["phase_pressure_hold"].values > 0.5, "hold", "flow")

    print(f"모델 autoencoder_normal{a.tag} | 피처 {len(cols)} | val {is_va.sum()}행 / test {len(t)}행")

    cands = [(f"p{p}", float(np.percentile(err_va, p))) for p in (99.0, 99.5, 99.9, 99.95)]
    mx = float(err_va.max())
    cands += [(f"max x{k:g}", mx * k) for k in (1.0, 1.5, 2.0)]

    # ---- 1+2. 임계값 x k-of-n ----
    rows = []
    for name, th in cands:
        over = err_te > th
        for k in (1, 2, 3, 4):
            fa, per = cycle_eval(t, over, k)
            rows.append(dict(기준=name, 임계값=th, k=k, 오경보=fa,
                             **{c[:9]: v for c, v in per.items()},
                             평균탐지=np.mean(list(per.values()))))
    df = pd.DataFrame(rows)
    print("\n=== 임계값 x k-of-n (사이클 단위) ===")
    print(df.to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    # ---- 3. 위상별 임계값 ----
    print("\n=== 위상별 임계값 (hold/flow 각각 val 분위수) ===")
    prows = []
    for p in (99.0, 99.5, 99.9, 99.95):
        th = {ph: float(np.percentile(err_va[va_phase == ph], p)) for ph in ("hold", "flow")}
        over = err_te > np.array([th[ph] for ph in te_phase])
        for k in (1, 2, 3):
            fa, per = cycle_eval(t, over, k)
            prows.append(dict(기준=f"phase p{p}", hold임계=th["hold"], flow임계=th["flow"], k=k,
                              오경보=fa, **{c[:9]: v for c, v in per.items()},
                              평균탐지=np.mean(list(per.values()))))
    pdf = pd.DataFrame(prows)
    print(pdf.to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    out = T.MODEL_DIR / f"ae_normal{a.tag}_postproc_sweep.csv"
    pd.concat([df.assign(방식="global"), pdf.assign(방식="phase")], ignore_index=True) \
        .to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n-> {out}")

    # 오경보 사이클 vs 고장 사이클의 초과 행 수 분포 (k-of-n 근거)
    th = dict(cands)["p99.5"]
    g = t.assign(o=(t.err > th).astype(int)).groupby("cycle_id").agg(
        cls=("fault_class", "first"), n=("o", "sum"), tot=("o", "size"))
    print("\n=== 사이클당 임계 초과 행 수 (p99.5) ===")
    print(g[g.n > 0].groupby("cls")["n"].describe()[["count", "mean", "50%", "max"]]
          .to_string(float_format=lambda v: f"{v:.1f}"))


if __name__ == "__main__":
    main()
