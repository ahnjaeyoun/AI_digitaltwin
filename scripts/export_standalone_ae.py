"""
학습된 AE를 **모델 파일 하나로 끝나는** 자립형(standalone) 형태로 내보낸다.

문제: `autoencoder_normal_split24pr.keras`는 57피처를 받는데 그중 11개가 파이썬으로 계산해야 하는
파생 피처(`flow_per_sr`, `pump_curve_residual`, 위상 원-핫 등)라, 모델 파일만으로는 추론이 안 되고
`train_autoencoder_normal.py`의 `load_join`/`add_derived`/`add_pump_residual`이 런타임 의존성이었다.

해법: 파생 피처 계산 + StandardScaler + AE + 재구성오차 + 임계값 판정을 **전부 Keras 그래프 안에
구워 넣는다**. 스케일러 평균/표준편차와 임계값은 상수로 박히므로 joblib도 필요 없다.

입력 (batch, 48) float32 — 순서 고정:
    [0..45]  원시 46열 (아래 RAW_COLS 순서, input/solver CSV에서 그대로 읽은 값)
    [46]     phase_idx : downstroke=0 / pressure_hold=1 / upstroke=2
    [47]     chamber_is_cap : cap=1 / rod=0
출력 3개:
    recon_error (batch,1) — 재구성오차
    is_anomaly  (batch,1) — recon_error > threshold 이면 1.0
    score       (batch,1) — recon_error / threshold (1.0 초과 시 이상, 임계값 재조정용)

※ 사이클 단위 판정(k-of-n)은 행 판정을 모아 세는 규칙이라 그래프에 넣지 않는다 —
   `is_anomaly` 합이 K_OF_N 이상이면 이상 사이클이다(K_OF_N은 아래 상수, 번들 postproc과 동일).

산출물:
  models/press_ae_standalone_savedmodel/    — **최종 산출물**. tf.saved_model.load()만으로 동작하며
                                              Keras 객체도 이 저장소 코드도 필요 없다.
  models/press_ae_standalone_io.json        — 입력 컬럼 순서·상수·사용법

※ `.keras` 포맷으로는 자립형이 안 된다: 전처리를 담은 Lambda가 함수를 **이름으로만** 직렬화해
   로드 시 정의 모듈을 다시 import해야 한다(`Could not locate function 'preprocess'`).
   그래서 그래프 자체를 굽는 SavedModel만 내보낸다.

실행: python scripts/export_standalone_ae.py [--tag _split24pr]
"""
import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import joblib
import numpy as np

ROOT = Path(r"C:\pipes_press")
MODEL_DIR = ROOT / "models"

# 파생 피처 계산에 쓰는 원시 열의 인덱스 (feature_cols 앞 46개 기준)
I_TARGET, I_RPM = 5, 11
I_Q_LPM, I_Q_M3H, I_POWER, I_HEAD = 18, 19, 20, 21
I_LOSS = [37, 38, 39, 40, 41]      # C_SUCTION / C_PRESSURE / C_A_LINE / C_B_LINE / C_RETURN
N_RAW = 46

RATED_RPM, EPS = 1800.0, 1e-2
PUMP_SHUTOFF_M, PUMP_QUAD = 2900.0, 100.0
K_OF_N = 3


def build_standalone(ae, mean, scale, threshold):
    """원시 48열 -> 파생 -> 표준화 -> AE -> 오차/판정 을 하나의 그래프로 묶는다."""
    import keras
    from keras import ops

    inp = keras.Input(shape=(N_RAW + 2,), dtype="float32", name="raw_input")

    def preprocess(x):
        raw = x[:, :N_RAW]
        phase_idx = ops.cast(x[:, N_RAW], "int32")
        chamber = x[:, N_RAW + 1:N_RAW + 2]
        # 위상 원-핫 순서는 feature_cols의 downstroke/pressure_hold/upstroke와 일치해야 한다
        phase_oh = ops.one_hot(phase_idx, 3)

        col = lambda i: raw[:, i:i + 1]
        sr = ops.maximum(col(I_RPM) / RATED_RPM, EPS)
        q_m3h, q_lpm = col(I_Q_M3H), col(I_Q_LPM)
        flowing = ops.cast(q_lpm > EPS, "float32")     # 보압 구간(유량 0)은 유량 기반 항을 0으로

        flow_per_sr = q_m3h / sr
        head_per_sr2 = col(I_HEAD) / ops.square(sr)
        power_per_flow = flowing * (col(I_POWER) / ops.maximum(q_lpm, EPS))
        total_loss = ops.sum(ops.concatenate([col(i) for i in I_LOSS], axis=1), axis=1, keepdims=True)
        line_resistance = flowing * (total_loss / ops.square(ops.maximum(q_m3h, EPS)))
        flow_to_target = q_lpm / ops.maximum(col(I_TARGET), EPS)
        h_nom = (PUMP_SHUTOFF_M - PUMP_QUAD * ops.square(q_m3h / sr)) * ops.square(sr)
        pump_resid = flowing * (col(I_HEAD) - h_nom)

        feats = ops.concatenate(
            [raw, chamber, phase_oh, flow_per_sr, head_per_sr2, power_per_flow,
             total_loss, line_resistance, flow_to_target, pump_resid], axis=1)
        return (feats - ops.convert_to_tensor(mean)) / ops.convert_to_tensor(scale)

    scaled = keras.layers.Lambda(preprocess, output_shape=(len(mean),), name="preprocess")(inp)
    recon = ae(scaled)

    err = keras.layers.Lambda(
        lambda t: ops.mean(ops.square(t[0] - t[1]), axis=1, keepdims=True),
        output_shape=(1,), name="recon_error")([scaled, recon])
    flag = keras.layers.Lambda(lambda e: ops.cast(e > threshold, "float32"),
                               output_shape=(1,), name="is_anomaly")(err)
    score = keras.layers.Lambda(lambda e: e / threshold,
                                output_shape=(1,), name="score")(err)
    return keras.Model(inp, [err, flag, score], name="press_ae_standalone")


def main():
    import keras

    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="_split24pr")
    a = ap.parse_args()

    b = joblib.load(MODEL_DIR / f"autoencoder_normal{a.tag}_bundle.joblib")
    ae = keras.saving.load_model(MODEL_DIR / f"autoencoder_normal{a.tag}.keras")
    fc, sc, thr = b["feature_cols"], b["scaler"], float(b["threshold"])
    mean = np.asarray(sc.mean_, dtype="float32")
    scale = np.asarray(sc.scale_, dtype="float32")
    raw_cols = fc[:N_RAW]
    assert len(fc) == 57 and raw_cols == [c for c in fc[:N_RAW]], "피처 순서 가정 불일치"

    model = build_standalone(ae, mean, scale, thr)
    # SavedModel만 내보낸다(모듈 docstring 참고 — .keras는 자립형이 될 수 없다)
    out_sm = MODEL_DIR / "press_ae_standalone_savedmodel"
    model.export(out_sm)

    io = {
        "input_name": "raw_input",
        "input_shape": [None, N_RAW + 2],
        "input_layout": {
            "0..45": "원시 46열 (raw_columns 순서)",
            "46": "phase_idx: downstroke=0, pressure_hold=1, upstroke=2",
            "47": "chamber_is_cap: cap=1, rod=0",
        },
        "raw_columns": raw_cols,
        "outputs": ["recon_error", "is_anomaly", "score"],
        "threshold": thr,
        "threshold_rule": b["threshold_percentile"],
        "k_of_n": K_OF_N,
        "cycle_rule": "한 사이클에서 is_anomaly 합계가 k_of_n 이상이면 이상 사이클",
        "latent": b.get("latent"),
        "train_source": b["train_source"],
        "usage": "m = tf.saved_model.load('press_ae_standalone_savedmodel'); err, flag, score = m.serve(x)",
        "dtype": "float32 (입력·출력 모두)",
    }
    (MODEL_DIR / "press_ae_standalone_io.json").write_text(
        json.dumps(io, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"-> {out_sm}")
    print(f"-> {MODEL_DIR / 'press_ae_standalone_io.json'}")
    print(f"입력 {N_RAW + 2}열 | 임계값 {thr:.9f} | k_of_n {K_OF_N}")


if __name__ == "__main__":
    main()
