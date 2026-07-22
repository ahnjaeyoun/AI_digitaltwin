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


def task_cls():
    train_df = load_set("train")
    tr, va = traj_split(train_df)
    print(f"train traj={tr['trajectory_id'].nunique()} rows={len(tr):,} / val traj={va['trajectory_id'].nunique()} rows={len(va):,}")
    model = lgb.LGBMClassifier(objective="multiclass", n_estimators=2000, learning_rate=0.05,
                               num_leaves=63, random_state=42, n_jobs=-1)
    model.fit(tr[FEATURE_COLS], tr["fault_class"], eval_set=[(va[FEATURE_COLS], va["fault_class"])],
              eval_metric="multi_logloss", categorical_feature=CATEGORICAL_FEATURES,
              callbacks=[lgb.early_stopping(30), lgb.log_evaluation(200)])

    test_df = load_set("test")
    y_pred = model.predict(test_df[FEATURE_COLS])
    y_true = test_df["fault_class"]
    print(f"\n[test 세트] macro F1 (행 단위): {f1_score(y_true, y_pred, average='macro'):.3f}")
    print(classification_report(y_true, y_pred, digits=3))
    stage_breakdown(test_df, (y_pred == y_true.values).astype(int))
    # 잠복 구간 제외 성능 — "신호가 존재하는 구간"만 보면 얼마나 맞히는지
    vis = ~((test_df["fault_class"] != "normal") & (test_df["health_stage"] == "normal"))
    print(f"\n잠복 구간 제외 macro F1: {f1_score(y_true[vis], y_pred[vis], average='macro'):.3f} "
          f"(대상 {vis.sum():,}행)")
    joblib.dump({"model": model, "feature_cols": FEATURE_COLS,
                 "categorical_features": CATEGORICAL_FEATURES}, CLS_MODEL_OUT)
    print(f"model saved -> {CLS_MODEL_OUT}")


def task_twin():
    cls_pack = joblib.load(CLS_MODEL_OUT)
    df = load_set("twin")
    df["_pred"] = cls_pack["model"].predict(df[FEATURE_COLS])

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
    args = ap.parse_args()
    {"cls": task_cls, "twin": task_twin}[args.task]()
