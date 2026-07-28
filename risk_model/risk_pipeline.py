"""F5에서 실행하는 LSTM 위험도 모델의 전체 학습·검증 순서입니다."""

import json

import numpy as np

from config import (
    EVALUATION_PATH,
    FEATURE_COLUMNS,
    MAX_SEQUENCE_ROWS,
    MODEL_PATH,
    OUTPUT_DIRECTORY,
    RANDOM_SEED,
    RISK_RESULT_PATH,
    TRAIN_BATCH_SIZE,
    TRAIN_EPOCHS,
)
from evaluation import evaluate
from preparation import prepare_training_data
from train import fit, save


def attach_risk_results(result, scores, window_rows):
    """기존 식별 정보에 위험도와 실제 시계열 확보량을 붙입니다."""
    result = result.copy()
    result["risk_score_percent"] = np.round(scores, 2)
    result["window_rows"] = window_rows
    result["history_coverage_percent"] = np.round(
        np.asarray(window_rows) / MAX_SEQUENCE_ROWS * 100.0,
        2,
    )
    return result


def main():
    """전처리부터 학습, 오류 검증, 정상 시계열 결과 저장까지 차례로 실행합니다."""
    print("[1/4] 원본 입력·솔버 출력 결합과 전처리")
    source, _, features = prepare_training_data()

    print("[2/4] 정상 시계열 LSTM Autoencoder 학습")
    model = fit(
        features,
        FEATURE_COLUMNS,
        TRAIN_EPOCHS,
        TRAIN_BATCH_SIZE,
        RANDOM_SEED,
    )
    save(model, MODEL_PATH)

    print("[3/4] 기존 오류 Test 시계열 검증")
    metrics = evaluate(model)
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    evaluation_result = {
        "model_type": "lstm_autoencoder",
        "max_sequence_rows": MAX_SEQUENCE_ROWS,
        "training_rows": len(features),
        "metrics": metrics,
    }
    EVALUATION_PATH.write_text(
        json.dumps(evaluation_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("[4/4] 3,000사이클 시계열 위험도 결과 저장")
    scores, window_rows = model.score_stream(features)
    result = source.loc[
        :,
        ["timestamp", "sample_index", "cycle_id", "cycle_second"],
    ].copy()
    result = attach_risk_results(result, scores, window_rows)
    result.to_csv(RISK_RESULT_PATH, index=False, encoding="utf-8-sig")

    print(f"정상 오경보율: {metrics['normal_false_alarm_rate']:.2%}")
    print(f"오류 데이터 위험 탐지율: {metrics['fault_detection_rate']:.2%}")
    print(f"모델 파일: {MODEL_PATH}")
    print(f"평가 결과: {EVALUATION_PATH}")
    print(f"위험도 결과: {RISK_RESULT_PATH}")


if __name__ == "__main__":
    main()
