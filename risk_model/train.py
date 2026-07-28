"""가변 길이 LSTM Autoencoder의 학습과 위험도 계산을 담당합니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import torch
from sklearn.preprocessing import StandardScaler
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_sequence
from torch.utils.data import DataLoader, Dataset

from config import (
    CALIBRATION_SEQUENCE_STRIDE,
    LSTM_HIDDEN_SIZE,
    LSTM_LATENT_SIZE,
    MAX_SEQUENCE_ROWS,
    SEQUENCE_LENGTHS,
    TRAIN_SEQUENCE_STRIDE,
)


def collate_sequences(batch):
    """길이가 다른 시계열을 같은 배치에 넣고 실제 데이터 위치를 표시합니다.

    PyTorch 배치는 직사각형 텐서여야 하므로 짧은 시계열 뒤에 임시 0을 붙입니다.
    ``mask``가 실제 행의 위치를 알려 주므로 이 임시 0은 학습과 위험도 계산에서 제외됩니다.
    """
    sequences = [torch.as_tensor(values, dtype=torch.float32) for values, _ in batch]
    lengths = torch.as_tensor([length for _, length in batch], dtype=torch.long)
    padded = pad_sequence(sequences, batch_first=True)
    positions = torch.arange(padded.size(1)).unsqueeze(0)
    mask = positions < lengths.unsqueeze(1)
    return padded, lengths, mask


class LSTMAutoencoder(nn.Module):
    """센서·솔버 값의 시간 흐름을 압축한 뒤 원래 시계열로 복원합니다."""

    def __init__(self, input_size: int, hidden_size: int, latent_size: int):
        super().__init__()
        self.encoder = nn.LSTM(input_size, hidden_size, batch_first=True)
        self.to_latent = nn.Linear(hidden_size, latent_size)
        self.decoder = nn.LSTM(latent_size, hidden_size, batch_first=True)
        self.output = nn.Linear(hidden_size, input_size)

    def forward(self, values: torch.Tensor, lengths: torch.Tensor) -> torch.Tensor:
        """패딩을 제외한 실제 길이만 Encoder에 넣고 입력 길이만큼 복원합니다."""
        packed = pack_padded_sequence(
            values,
            lengths.cpu(),
            batch_first=True,
            enforce_sorted=False,
        )
        _, (hidden, _) = self.encoder(packed)
        latent = self.to_latent(hidden[-1])
        repeated = latent.unsqueeze(1).expand(-1, values.size(1), -1)
        decoded, _ = self.decoder(repeated)
        return self.output(decoded)


class SequenceWindowDataset(Dataset):
    """원본 배열을 복사하지 않고 필요한 시계열 구간만 잘라서 반환합니다."""

    def __init__(self, values: np.ndarray, windows: Iterable[tuple[int, int]]):
        self.values = np.asarray(values, dtype=np.float32)
        self.windows = list(windows)

    def __len__(self) -> int:
        return len(self.windows)

    def __getitem__(self, index: int):
        start, end = self.windows[index]
        sequence = self.values[start:end]
        return sequence, len(sequence)


def _training_windows(row_count: int) -> list[tuple[int, int]]:
    """정상 운전 전체에서 여러 길이의 학습 시계열 위치를 만듭니다."""
    windows: list[tuple[int, int]] = []
    for length in SEQUENCE_LENGTHS:
        if length > row_count:
            continue
        starts = list(range(0, row_count - length + 1, TRAIN_SEQUENCE_STRIDE))
        final_start = row_count - length
        if not starts or starts[-1] != final_start:
            starts.append(final_start)
        windows.extend((start, start + length) for start in starts)
    if not windows:
        raise ValueError("LSTM 학습에 사용할 정상 시계열이 없습니다.")
    return windows


def _calibration_windows(row_count: int) -> list[tuple[int, int]]:
    """실시간에 가능한 1~150행 각각의 정상 오차 기준용 구간을 만듭니다."""
    windows: list[tuple[int, int]] = []
    for length in range(1, min(MAX_SEQUENCE_ROWS, row_count) + 1):
        starts = list(range(0, row_count - length + 1, CALIBRATION_SEQUENCE_STRIDE))
        final_start = row_count - length
        if not starts or starts[-1] != final_start:
            starts.append(final_start)
        windows.extend((start, start + length) for start in starts)
    return windows


def _stream_windows(
    row_count: int,
    group_ids: np.ndarray | None,
    max_rows: int,
) -> list[tuple[int, int]]:
    """각 행의 위험도 계산에 사용할 최근 시계열 위치를 순서대로 만듭니다."""
    if row_count == 0:
        return []
    if group_ids is None:
        group_ids = np.zeros(row_count, dtype=np.int8)
    group_ids = np.asarray(group_ids)
    if len(group_ids) != row_count:
        raise ValueError("입력 행 수와 시계열 그룹 번호의 개수가 다릅니다.")

    windows: list[tuple[int, int]] = []
    group_start = 0
    for end_index in range(row_count):
        if end_index > 0 and group_ids[end_index] != group_ids[end_index - 1]:
            group_start = end_index
        start = max(group_start, end_index - max_rows + 1)
        windows.append((start, end_index + 1))
    return windows


def _masked_sequence_errors(
    rebuilt: torch.Tensor,
    original: torch.Tensor,
    mask: torch.Tensor,
) -> torch.Tensor:
    """각 시계열의 실제 행에서만 평균 복원 오차를 계산합니다."""
    row_errors = torch.mean((rebuilt - original) ** 2, dim=2)
    numeric_mask = mask.to(dtype=row_errors.dtype)
    return (row_errors * numeric_mask).sum(dim=1) / numeric_mask.sum(dim=1)


def _collect_errors(
    network: LSTMAutoencoder,
    dataset: SequenceWindowDataset,
    batch_size: int,
) -> tuple[np.ndarray, np.ndarray]:
    """시계열 목록의 복원 오차와 실제 길이를 배치 단위로 계산합니다."""
    if len(dataset) == 0:
        return np.empty(0, dtype=np.float32), np.empty(0, dtype=np.int32)

    loader = DataLoader(
        dataset,
        batch_size=min(batch_size, len(dataset)),
        shuffle=False,
        collate_fn=collate_sequences,
    )
    errors: list[np.ndarray] = []
    sequence_lengths: list[np.ndarray] = []
    network.eval()
    with torch.no_grad():
        for batch, lengths, mask in loader:
            rebuilt = network(batch, lengths)
            batch_errors = _masked_sequence_errors(rebuilt, batch, mask)
            errors.append(batch_errors.cpu().numpy())
            sequence_lengths.append(lengths.cpu().numpy())
    return np.concatenate(errors), np.concatenate(sequence_lengths)


def risk_score(errors, p95, p99):
    """정상 오차의 95~99 백분위 구간을 0~100% 위험도로 변환합니다."""
    denominator = max(float(p99) - float(p95), 1e-12)
    return np.clip((np.asarray(errors) - float(p95)) / denominator * 100.0, 0.0, 100.0)


@dataclass
class RiskModel:
    """학습된 LSTM, 표준화 기준, 길이별 정상 오차 기준을 하나로 묶습니다."""

    network: LSTMAutoencoder
    scaler: StandardScaler
    columns: tuple[str, ...]
    calibrations: dict[int, dict[str, float]]
    max_sequence_rows: int = MAX_SEQUENCE_ROWS
    batch_size: int = 256

    def score_stream(
        self,
        rows: np.ndarray,
        group_ids: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """각 행에서 끝나는 최근 1~150행을 사용해 위험도를 계산합니다."""
        values = np.asarray(rows, dtype=np.float32)
        if values.ndim != 2 or values.shape[1] != len(self.columns):
            raise ValueError(
                f"모델 입력은 {len(self.columns)}개 컬럼을 가진 2차원 배열이어야 합니다."
            )
        if len(values) == 0:
            return np.empty(0, dtype=np.float32), np.empty(0, dtype=np.int32)

        scaled = self.scaler.transform(values).astype(np.float32)
        windows = _stream_windows(len(scaled), group_ids, self.max_sequence_rows)
        dataset = SequenceWindowDataset(scaled, windows)
        errors, lengths = _collect_errors(self.network, dataset, self.batch_size)

        scores = np.empty(len(errors), dtype=np.float32)
        for length, calibration in self.calibrations.items():
            selected = lengths == length
            if selected.any():
                scores[selected] = risk_score(
                    errors[selected],
                    calibration["p95"],
                    calibration["p99"],
                )
        return scores, lengths.astype(np.int32)


def fit(features, columns, epochs, batch_size, seed):
    """정상 시계열만 사용해 LSTM Autoencoder와 위험도 기준을 학습합니다."""
    values = np.asarray(features, dtype=np.float32)
    if values.ndim != 2 or len(values) == 0:
        raise ValueError("LSTM 학습 데이터는 한 행 이상인 2차원 배열이어야 합니다.")

    np.random.seed(seed)
    torch.manual_seed(seed)
    scaler = StandardScaler()
    scaled = scaler.fit_transform(values).astype(np.float32)
    dataset = SequenceWindowDataset(scaled, _training_windows(len(scaled)))
    loader = DataLoader(
        dataset,
        batch_size=min(batch_size, len(dataset)),
        shuffle=True,
        collate_fn=collate_sequences,
    )
    network = LSTMAutoencoder(
        input_size=scaled.shape[1],
        hidden_size=LSTM_HIDDEN_SIZE,
        latent_size=LSTM_LATENT_SIZE,
    )
    optimizer = torch.optim.Adam(network.parameters(), lr=0.001)

    network.train()
    for epoch in range(epochs):
        total_loss = 0.0
        total_sequences = 0
        for batch, lengths, mask in loader:
            optimizer.zero_grad()
            rebuilt = network(batch, lengths)
            sequence_errors = _masked_sequence_errors(rebuilt, batch, mask)
            loss = sequence_errors.mean()
            loss.backward()
            optimizer.step()
            total_loss += float(loss.item()) * len(batch)
            total_sequences += len(batch)
        average_loss = total_loss / max(total_sequences, 1)
        print(f"Epoch {epoch + 1}/{epochs} - 복원 손실 {average_loss:.6f}")

    calibration_dataset = SequenceWindowDataset(
        scaled,
        _calibration_windows(len(scaled)),
    )
    calibration_errors, calibration_lengths = _collect_errors(
        network,
        calibration_dataset,
        batch_size,
    )
    calibrations: dict[int, dict[str, float]] = {}
    for length in range(1, min(MAX_SEQUENCE_ROWS, len(scaled)) + 1):
        selected = calibration_lengths == length
        bucket_errors = calibration_errors[selected]
        if len(bucket_errors) == 0:
            raise ValueError(f"{length}행 정상 위험도 기준을 계산할 데이터가 없습니다.")
        p95 = float(np.quantile(bucket_errors, 0.95))
        p99 = max(float(np.quantile(bucket_errors, 0.99)), p95 + 1e-12)
        calibrations[length] = {"p95": p95, "p99": p99}

    return RiskModel(
        network=network.eval(),
        scaler=scaler,
        columns=tuple(columns),
        calibrations=calibrations,
    )


def save(model, model_path: Path):
    """LSTM 가중치와 실행에 필요한 모든 기준을 pt 파일 하나에 저장합니다."""
    model_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model_type": "lstm_autoencoder",
            "input_size": len(model.columns),
            "hidden_size": LSTM_HIDDEN_SIZE,
            "latent_size": LSTM_LATENT_SIZE,
            "columns": model.columns,
            "max_sequence_rows": model.max_sequence_rows,
            "calibrations": model.calibrations,
            "state": model.network.state_dict(),
            "scaler": model.scaler,
        },
        model_path,
    )


def load(model_path: Path) -> RiskModel:
    """이 프로젝트가 직접 저장한 단일 pt 파일에서 LSTM 위험도 모델을 복원합니다."""
    artifact = torch.load(model_path, map_location="cpu", weights_only=False)
    if artifact.get("model_type") != "lstm_autoencoder":
        raise ValueError("LSTM Autoencoder 형식의 위험도 모델 파일이 아닙니다.")

    network = LSTMAutoencoder(
        input_size=int(artifact["input_size"]),
        hidden_size=int(artifact["hidden_size"]),
        latent_size=int(artifact["latent_size"]),
    )
    network.load_state_dict(artifact["state"])
    return RiskModel(
        network=network.eval(),
        scaler=artifact["scaler"],
        columns=tuple(artifact["columns"]),
        calibrations=dict(artifact["calibrations"]),
        max_sequence_rows=int(artifact["max_sequence_rows"]),
    )
