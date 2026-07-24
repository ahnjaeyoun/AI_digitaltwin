"""
궤적형 통합 데이터셋(traj_dataset_train/test/twin.csv) 기반 원인분류 모델 학습·평가.
설계: docs/예지보전_통합데이터셋_설계.md

  python train_trajectory_models.py --task cls   # ① 원인분류(11클래스) 재학습 + test 평가
  python train_trajectory_models.py --task twin  # ② 디지털트윈 세트(95:5) 배포 시뮬레이션

※ 프로젝트 스코프는 고장원인분류까지 — 예지보전(RUL) 모델은 타 담당자 몫이며, 이 저장소는
   데이터셋의 RUL 라벨(cycles_to_failure 등)만 제공한다(2026-07-22 확정, RUL 학습 코드 제거됨).

공통 원칙:
 - 피처는 기존 파이프라인과 동일(train_baseline.NUMERIC/CATEGORICAL + build_dataset.add_engineered_features)
   — 궤적 라벨 컬럼(health_stage/degradation_score/cycles_to_failure/gt_* 등)은 피처로 쓰지 않는다.
 - train 내부 검증 분할은 trajectory_id 단위(같은 궤적이 train/val 양쪽에 걸치지 않음, 사이클 단위로는 부족).
 - fault_class 라벨은 주입(물리) 기준이므로, 주입~관측 onset 사이 "잠복 구간"은 신호가 없어
   분류가 원리적으로 어렵다 → health_stage별 분해 평가로 정직하게 확인한다.
"""
import argparse
import sys
import time
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, f1_score
from sklearn.model_selection import train_test_split

sys.path.insert(0, str(Path(__file__).parent))
from build_dataset import add_engineered_features
from train_baseline import CATEGORICAL_FEATURES, FEATURE_COLS, NUMERIC_FEATURES

DATA_TPL = r"C:\pipes_press\dataset\traj_dataset_{s}.csv"
CLS_MODEL_OUT = r"C:\pipes_press\models\traj_classifier_lgbm.joblib"

# 사이클 간 추세 피처 — 라벨링(trajectory_labels.CLASS_SIG)과 같은 계열의 사이클 감시 신호.
# *_r = 지령(보압 목표압·rpm) OLS 회귀 잔차(σ 15~20배 감소 확인된 정규화, 지령은 관측 가능하므로 누수 아님)
TREND_NORM_SIGS = ["flow_per_rpm", "down_min_pump_in", "down_pump_out", "up_flow"]
TREND_SIGS = ["flow_per_rpm_r", "down_min_pump_in_r", "down_pump_out_r", "up_flow_r",
              "down_max_tgt_err", "frd_max", "mean_temp"]
TREND_FEATURES = [f"tf_{s}_{k}" for s in TREND_SIGS for k in ("last", "rm5", "slope", "d0")]


def add_trend_features(df, coefs=None):
    """사이클 단위 감시 신호 → 지령 회귀 잔차 → '직전 완료 사이클까지'의 추세를 행 단위로 병합.
    last=직전 사이클 값, rm5=최근 5사이클 평균, slope=최근 추세 기울기(rm3 차분),
    d0=궤적 초기 3사이클 베이스라인 대비 드리프트. 전부 관측 가능량에서 인과적으로 계산되므로
    서빙에서는 사이클 종료 시 갱신하는 버퍼로 동일 재현 가능. coefs는 train 정상 궤적에서
    적합해 test/twin에 재사용(세트 간 동일 기준)."""
    key = ["trajectory_id", "cycle_in_traj"]
    t = df.assign(_frd=df["flow_rate_L_min"] - df["return_flow_L_min"])
    d = t[t["cycle_phase"] == "downstroke"].groupby(key)
    c = pd.DataFrame({"down_mean_flow": d["flow_rate_L_min"].mean(),
                      "down_min_pump_in": d["pump_in_pressure_bar"].min(),
                      "down_pump_out": d["pump_out_pressure_bar"].mean(),
                      "down_max_tgt_err": d["target_pressure_error_bar"].max(),
                      "rpm": d["pump_rpm"].mean()})
    c["up_flow"] = t[t["cycle_phase"] == "upstroke"].groupby(key)["flow_rate_L_min"].mean()
    c["tgt_hold"] = t[t["cycle_phase"] == "pressure_hold"].groupby(key)["target_pressure_bar"].mean()
    c["mean_temp"] = t.groupby(key)["temperature_c"].mean()
    c["frd_max"] = t.groupby(key)["_frd"].max()
    c["flow_per_rpm"] = c["down_mean_flow"] / c["rpm"] * 1000
    c["_normal"] = t.groupby(key)["gt_traj_class"].first() == "normal"
    c = c.reset_index().sort_values(key).reset_index(drop=True)

    X = np.column_stack([np.ones(len(c)), c["tgt_hold"], c["rpm"]])
    if coefs is None:
        nm = c["_normal"].to_numpy()
        coefs = {s: np.linalg.lstsq(X[nm], c.loc[nm, s], rcond=None)[0].tolist()
                 for s in TREND_NORM_SIGS}
    for s in TREND_NORM_SIGS:
        c[s + "_r"] = c[s] - X @ np.asarray(coefs[s])

    tid = c["trajectory_id"]
    g = c.groupby("trajectory_id", group_keys=False)
    for s in TREND_SIGS:
        prev = g[s].shift(1)  # 현재 사이클은 진행 중이므로 직전 완료 사이클까지만 사용
        rm3 = prev.groupby(tid).rolling(3, min_periods=3).mean().reset_index(level=0, drop=True)
        c[f"tf_{s}_last"] = prev
        c[f"tf_{s}_rm5"] = prev.groupby(tid).rolling(5, min_periods=2).mean().reset_index(level=0, drop=True)
        c[f"tf_{s}_slope"] = (rm3 - rm3.groupby(tid).shift(7)) / 7.0
        base = g[s].transform(lambda x: x.iloc[:3].mean())  # 궤적 초기 3사이클 = 설비별 베이스라인
        c[f"tf_{s}_d0"] = rm3 - base
    return df.merge(c[key + TREND_FEATURES], on=key, how="left"), coefs


def load_set(name):
    t0 = time.time()
    df = pd.read_csv(DATA_TPL.format(s=name), low_memory=False)
    df = add_engineered_features(df)
    for c in CATEGORICAL_FEATURES:
        df[c] = df[c].astype("category")
    print(f"loaded {name}: {df.shape} ({time.time() - t0:.0f}s)")
    return df


def traj_split(df, test_size=0.15, seed=42):
    """trajectory_id 단위 분할 + gt_traj_class 층화."""
    lab = df.groupby("trajectory_id")["gt_traj_class"].first()
    tr, va = train_test_split(lab.index, test_size=test_size, stratify=lab.values, random_state=seed)
    return df[df["trajectory_id"].isin(tr)], df[df["trajectory_id"].isin(va)]


def stage_breakdown(df, correct):
    """health_stage별 분류 정확도 — 잠복 구간(fault인데 stage=normal)의 난이도 확인용."""
    d = df.assign(_c=correct)
    print("\nhealth_stage별 정확도 (고장 라벨 행만):")
    fault = d[d["fault_class"] != "normal"]
    print(fault.groupby("health_stage", observed=True)["_c"].agg(["mean", "size"]).round(3))
    latent = fault["health_stage"] == "normal"
    print(f"잠복 구간(fault_class=fault & health_stage=normal): {latent.sum():,}행, "
          f"정확도 {fault.loc[latent, '_c'].mean():.3f} — 신호가 관리도 검출 하한 미만인 구간(원리적 한계)")


def task_cls(latent="keep", trend="off", tag=""):
    train_df = load_set("train")
    coefs = None
    if trend == "on":
        t0 = time.time()
        train_df, coefs = add_trend_features(train_df)
        print(f"trend features added ({time.time() - t0:.0f}s)")
    feat_cols = FEATURE_COLS + (TREND_FEATURES if trend == "on" else [])
    if latent == "drop":
        # 잠복 구간(주입은 됐지만 신호가 검출 하한 미만 → health_stage=normal)은 정상과 물리적으로
        # 구별 불가하므로 학습에서 제외해 결정 경계 오염을 막는다. 평가는 기존 기준 그대로 유지.
        m = (train_df["fault_class"] != "normal") & (train_df["health_stage"] == "normal")
        print(f"latent=drop: 잠복 구간 {m.sum():,}행 학습 제외 (전체 {len(train_df):,}행)")
        train_df = train_df[~m]
    tr, va = traj_split(train_df)
    print(f"train traj={tr['trajectory_id'].nunique()} rows={len(tr):,} / val traj={va['trajectory_id'].nunique()} rows={len(va):,}")
    model = lgb.LGBMClassifier(objective="multiclass", n_estimators=2000, learning_rate=0.05,
                               num_leaves=63, random_state=42, n_jobs=-1)
    model.fit(tr[feat_cols], tr["fault_class"], eval_set=[(va[feat_cols], va["fault_class"])],
              eval_metric="multi_logloss", categorical_feature=CATEGORICAL_FEATURES,
              callbacks=[lgb.early_stopping(30), lgb.log_evaluation(200)])

    test_df = load_set("test")
    if trend == "on":
        test_df, _ = add_trend_features(test_df, coefs)
    y_pred = model.predict(test_df[feat_cols])
    y_true = test_df["fault_class"]
    print(f"\n[test 세트] macro F1 (행 단위): {f1_score(y_true, y_pred, average='macro'):.3f}")
    print(classification_report(y_true, y_pred, digits=3))
    stage_breakdown(test_df, (y_pred == y_true.values).astype(int))
    # 잠복 구간 제외 성능 — "신호가 존재하는 구간"만 보면 얼마나 맞히는지
    vis = ~((test_df["fault_class"] != "normal") & (test_df["health_stage"] == "normal"))
    print(f"\n잠복 구간 제외 macro F1: {f1_score(y_true[vis], y_pred[vis], average='macro'):.3f} "
          f"(대상 {vis.sum():,}행)")
    out = model_path(tag)
    joblib.dump({"model": model, "feature_cols": feat_cols,
                 "categorical_features": CATEGORICAL_FEATURES, "trend_coefs": coefs}, out)
    print(f"model saved -> {out}")


def model_path(tag=""):
    """tag가 있으면 실험용 모델 파일로 분리 저장해 기준 모델을 보존한다."""
    return CLS_MODEL_OUT.replace(".joblib", f"_{tag}.joblib") if tag else CLS_MODEL_OUT


def task_twin(latent="keep", trend="off", tag=""):
    cls_pack = joblib.load(model_path(tag))
    df = load_set("twin")
    if cls_pack.get("trend_coefs"):
        df, _ = add_trend_features(df, cls_pack["trend_coefs"])
    df["_pred"] = cls_pack["model"].predict(df[cls_pack["feature_cols"]])

    # 사이클 단위 판정 = 15행 다수결 (스트리밍에서 사이클 종료 시점 판정에 해당)
    cyc = df.groupby(["trajectory_id", "cycle_in_traj"]).agg(
        pred=("_pred", lambda s: s.mode().iloc[0]), true=("fault_class", "first"),
        traj_cls=("gt_traj_class", "first"), inject=("gt_inject_onset", "first"),
        stage=("health_stage", "first")).reset_index()

    normal_cyc = cyc[cyc["true"] == "normal"]
    fa = (normal_cyc["pred"] != "normal").mean()
    print(f"\n[오경보] 진짜 정상 사이클 {len(normal_cyc):,}개 중 비정상 판정 {(normal_cyc['pred'] != 'normal').sum():,}개 "
          f"= {fa:.4%} ({fa * 1000:.1f}건/1000사이클)")
    fa_by_traj = normal_cyc.assign(_f=normal_cyc["pred"] != "normal").groupby(
        normal_cyc["traj_cls"] == "normal")["_f"].mean()
    print(f"  정상 전용 궤적 내: {fa_by_traj.get(True, 0):.4%} / 고장 궤적의 onset 전 구간: {fa_by_traj.get(False, 0):.4%}")

    print("\n[탐지] 고장 궤적 20개 (주입 onset 이후 기준):")
    print(f"  {'traj':>4} {'class':15s} {'inject':>6} {'첫탐지':>6} {'지연':>4} {'post-onset 탐지율':>14} {'클래스정확도':>10}")
    for tid, g in cyc[cyc["traj_cls"] != "normal"].groupby("trajectory_id"):
        inj = pd.to_numeric(g["inject"].iloc[0])
        post = g[g["cycle_in_traj"] >= inj]
        det = post[post["pred"] != "normal"]
        first = int(det["cycle_in_traj"].min()) if len(det) else None
        det_rate = (post["pred"] != "normal").mean()
        cls_acc = (det["pred"] == det["traj_cls"]).mean() if len(det) else np.nan
        print(f"  {tid:>4} {g['traj_cls'].iloc[0]:15s} {int(inj):>6} {first if first else '미탐지':>6} "
              f"{(first - int(inj)) if first else '-':>4} {det_rate:>13.1%} {cls_acc:>10.3f}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", required=True, choices=["cls", "twin"])
    ap.add_argument("--latent", choices=["keep", "drop"], default="keep",
                    help="잠복 구간(fault인데 health_stage=normal) 행을 학습에 포함할지")
    ap.add_argument("--trend", choices=["off", "on"], default="off",
                    help="사이클 간 추세 피처(TREND_FEATURES) 사용 여부 (twin은 모델에 저장된 설정을 따름)")
    ap.add_argument("--tag", default="", help="실험 태그 — 모델 파일명을 분리해 기준 모델 보존")
    args = ap.parse_args()
    {"cls": task_cls, "twin": task_twin}[args.task](latent=args.latent, trend=args.trend, tag=args.tag)
