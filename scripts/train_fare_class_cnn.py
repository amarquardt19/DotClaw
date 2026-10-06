#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

try:
    import torch
    from torch import nn
    from torch.utils.data import DataLoader, Dataset
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyTorch is required for this script. Install it with `pip install torch` and rerun."
    ) from exc

DEFAULT_WORKBOOK = Path("/Users/adammarq/Downloads/Flights Claw Final Project v6.xlsx")
DEFAULT_OUTPUT_DIR = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/output/fare_class_cnn")

DYNAMIC_NUMERIC_COLUMNS = [
    "days_until_departure",
    "price_value",
    "price_difference_prev_snapshot",
    "price_difference_prev_departure",
    "price_difference_from_min_for_departure",
    "price_quantile_overall",
    "departure_minutes",
    "duration_minutes",
    "competing_nonstop_same_time_bucket_count",
    "price_diff_vs_cheapest_competing_nonstop",
    "competing_nonstop_other_airlines_same_time_bucket_count",
    "price_diff_vs_cheapest_other_airline_competing_nonstop",
    "alternate_nonstop_any_airline_count",
    "alternate_nonstop_same_airline_count",
    "price_diff_vs_cheapest_same_airline_alternate_nonstop",
    "alternate_nonstop_different_airline_count",
    "price_diff_vs_cheapest_different_airline_alternate_nonstop",
    "estimated_booking_pressure_score",
    "estimated_bookings_daily",
    "service_estimated_bookings_total_period",
    "service_estimated_bookings_avg_daily",
    "service_observation_days",
    "total_days_service_start_to_end",
    "number_of_flights_offered",
    "number_of_days_since_last_flight",
    "first_day_offered",
    "last_day_offered",
]

DYNAMIC_BOOLEAN_COLUMNS = [
    "is_weekend_departure",
    "holiday_window_flag",
    "fourth_of_july_flag",
    "labor_day_flag",
    "period_60_to_30_days_out_flag",
    "period_30_to_14_days_out_flag",
    "period_14_to_7_days_out_flag",
    "period_7_to_3_days_out_flag",
    "period_3_to_0_days_out_flag",
    "is_europe_route",
    "is_latam_route",
    "is_canada_route",
    "is_caribbean_route",
]

STATIC_CATEGORICAL_COLUMNS = [
    "origin",
    "destination",
    "departure_dow",
    "airline",
    "time_of_day_bucket",
    "holiday_season",
]

TARGET_CANDIDATES = ["fare_letter_bucket", "fare_bucket_proxy"]


@dataclass
class PreparedData:
    sequences: np.ndarray
    masks: np.ndarray
    labels: np.ndarray
    label_names: list[str]
    feature_names: list[str]
    sample_keys: list[str]
    stats: dict[str, Any]


class SequenceDataset(Dataset):
    def __init__(self, sequences: np.ndarray, masks: np.ndarray, labels: np.ndarray):
        self.sequences = torch.tensor(sequences, dtype=torch.float32)
        self.masks = torch.tensor(masks, dtype=torch.float32)
        self.labels = torch.tensor(labels, dtype=torch.long)

    def __len__(self) -> int:
        return int(self.labels.shape[0])

    def __getitem__(self, idx: int):
        return self.sequences[idx], self.masks[idx], self.labels[idx]


class TemporalFareCNN(nn.Module):
    def __init__(self, feature_dim: int, class_count: int):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(feature_dim, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(64),
            nn.Conv1d(64, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(64),
            nn.Conv1d(64, 128, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(128),
        )
        self.head = nn.Sequential(
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(64, class_count),
        )

    def forward(self, x: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        x = x.transpose(1, 2)
        features = self.net(x)
        mask = mask.unsqueeze(1)
        features = features * mask
        pooled = features.sum(dim=2) / mask.sum(dim=2).clamp(min=1.0)
        return self.head(pooled)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", default=str(DEFAULT_WORKBOOK))
    parser.add_argument("--sheet-name", default=0)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--horizon-days", type=int, default=66)
    parser.add_argument("--max-seq-len", type=int, default=66)
    parser.add_argument("--min-observations", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--target-column", default="")
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def choose_target_column(df: pd.DataFrame, explicit: str) -> str:
    if explicit:
        if explicit not in df.columns:
            raise ValueError(f"Target column not found: {explicit}")
        return explicit
    for candidate in TARGET_CANDIDATES:
        if candidate in df.columns and df[candidate].notna().any():
            return candidate
    raise ValueError("No usable target column found. Tried fare_letter_bucket and fare_bucket_proxy.")


def normalize_bool(series: pd.Series) -> pd.Series:
    return series.fillna(False).astype(bool).astype(float)


def one_hot_static(df: pd.DataFrame) -> pd.DataFrame:
    usable = [col for col in STATIC_CATEGORICAL_COLUMNS if col in df.columns]
    if not usable:
        return pd.DataFrame(index=df.index)
    static = df[usable].fillna("unknown").astype(str)
    return pd.get_dummies(static, prefix=usable, dtype=float)


def add_sequence_features(group: pd.DataFrame) -> pd.DataFrame:
    group = group.sort_values(["days_until_departure", "query_timestamp"], ascending=[False, True]).copy()
    group["days_until_departure"] = pd.to_numeric(group["days_until_departure"], errors="coerce")
    group["price_value"] = pd.to_numeric(group.get("price_value"), errors="coerce")
    group = group.dropna(subset=["days_until_departure"]).copy()
    group = group.drop_duplicates(subset=["days_until_departure"], keep="last")
    group["days_gap_from_previous_observation"] = group["days_until_departure"].diff().abs().fillna(0.0)
    group["price_delta_per_day"] = group["price_value"].diff().div(group["days_gap_from_previous_observation"].replace(0, np.nan))
    group["price_delta_per_day"] = group["price_delta_per_day"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    group["price_acceleration_per_day"] = group["price_delta_per_day"].diff().div(group["days_gap_from_previous_observation"].replace(0, np.nan))
    group["price_acceleration_per_day"] = group["price_acceleration_per_day"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    avg_gap = float(group["days_gap_from_previous_observation"].replace(0, np.nan).mean()) if len(group) > 1 else 0.0
    group["average_days_between_dates"] = 0.0 if np.isnan(avg_gap) else avg_gap
    group["observed_flag"] = 1.0
    return group


def prepare_data(args: argparse.Namespace) -> PreparedData:
    df = pd.read_excel(args.workbook, sheet_name=args.sheet_name)
    if "matched_target_flight" in df.columns:
        df = df[df["matched_target_flight"].fillna(False).astype(bool)].copy()
    df["query_timestamp"] = pd.to_datetime(df.get("query_timestamp"), errors="coerce")
    df["departure_date"] = pd.to_datetime(df.get("departure_date"), errors="coerce")
    df = df[df["days_until_departure"].notna()].copy()
    df = df[pd.to_numeric(df["days_until_departure"], errors="coerce") <= args.horizon_days].copy()
    df = df[df["route_service_label"].notna() & df["departure_date"].notna()].copy()

    target_col = choose_target_column(df, args.target_column)
    df = df[df[target_col].notna()].copy()
    df[target_col] = df[target_col].astype(str)

    static_dummies = one_hot_static(df)
    label_names = sorted(df[target_col].dropna().unique().tolist())
    label_to_idx = {label: idx for idx, label in enumerate(label_names)}

    numeric_cols = [col for col in DYNAMIC_NUMERIC_COLUMNS if col in df.columns]
    bool_cols = [col for col in DYNAMIC_BOOLEAN_COLUMNS if col in df.columns]
    sequence_feature_names = numeric_cols + bool_cols + [
        "days_gap_from_previous_observation",
        "price_delta_per_day",
        "price_acceleration_per_day",
        "average_days_between_dates",
        "observed_flag",
    ] + static_dummies.columns.tolist()

    grouped = []
    for (service_label, departure_date), group in df.groupby(["route_service_label", "departure_date"], dropna=True):
        group = add_sequence_features(group)
        if len(group) < args.min_observations:
            continue
        target_row = group.sort_values(["days_until_departure", "query_timestamp"], ascending=[True, False]).iloc[0]
        target_label = str(target_row[target_col])
        if target_label not in label_to_idx:
            continue
        dynamic_part = pd.DataFrame(index=group.index)
        for col in numeric_cols:
            dynamic_part[col] = pd.to_numeric(group[col], errors="coerce")
        for col in bool_cols:
            dynamic_part[col] = normalize_bool(group[col])
        for extra in ["days_gap_from_previous_observation", "price_delta_per_day", "price_acceleration_per_day", "average_days_between_dates", "observed_flag"]:
            dynamic_part[extra] = pd.to_numeric(group[extra], errors="coerce").fillna(0.0)
        static_part = static_dummies.loc[group.index].copy() if not static_dummies.empty else pd.DataFrame(index=group.index)
        feature_frame = pd.concat([dynamic_part, static_part], axis=1).fillna(0.0)
        array = feature_frame.to_numpy(dtype=np.float32)
        if array.shape[0] > args.max_seq_len:
            array = array[-args.max_seq_len :]
        mask = np.zeros(args.max_seq_len, dtype=np.float32)
        padded = np.zeros((args.max_seq_len, array.shape[1]), dtype=np.float32)
        padded[-array.shape[0] :, :] = array
        mask[-array.shape[0] :] = 1.0
        grouped.append(
            {
                "key": f"{service_label}|{departure_date.date().isoformat()}",
                "sequence": padded,
                "mask": mask,
                "label": label_to_idx[target_label],
            }
        )

    if not grouped:
        raise ValueError("No training samples were created. Try lowering --min-observations or checking the workbook.")

    sequences = np.stack([item["sequence"] for item in grouped])
    masks = np.stack([item["mask"] for item in grouped])
    labels = np.array([item["label"] for item in grouped], dtype=np.int64)
    sample_keys = [item["key"] for item in grouped]

    valid = masks[..., None] > 0
    means = (sequences * valid).sum(axis=(0, 1)) / valid.sum(axis=(0, 1)).clip(min=1)
    variances = (((sequences - means) * valid) ** 2).sum(axis=(0, 1)) / valid.sum(axis=(0, 1)).clip(min=1)
    stds = np.sqrt(np.maximum(variances, 1e-6))
    sequences = np.where(valid, (sequences - means) / stds, 0.0).astype(np.float32)

    stats = {
        "sample_count": int(len(grouped)),
        "class_count": int(len(label_names)),
        "target_column": target_col,
        "feature_count": int(sequences.shape[2]),
        "max_seq_len": int(args.max_seq_len),
        "class_distribution": {label: int((labels == idx).sum()) for label, idx in label_to_idx.items()},
        "feature_means": means.tolist(),
        "feature_stds": stds.tolist(),
    }
    return PreparedData(sequences, masks, labels, label_names, sequence_feature_names, sample_keys, stats)


def evaluate(model: nn.Module, loader: DataLoader, device: torch.device) -> tuple[float, float]:
    model.eval()
    total_loss = 0.0
    total_correct = 0
    total = 0
    criterion = nn.CrossEntropyLoss()
    with torch.no_grad():
        for sequences, masks, labels in loader:
            sequences = sequences.to(device)
            masks = masks.to(device)
            labels = labels.to(device)
            logits = model(sequences, masks)
            loss = criterion(logits, labels)
            total_loss += float(loss.item()) * labels.size(0)
            total_correct += int((logits.argmax(dim=1) == labels).sum().item())
            total += int(labels.size(0))
    return total_loss / max(total, 1), total_correct / max(total, 1)


def train(args: argparse.Namespace) -> None:
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    prepared = prepare_data(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    indices = np.arange(prepared.labels.shape[0])
    stratify = prepared.labels if len(np.unique(prepared.labels)) > 1 else None
    train_idx, val_idx = train_test_split(indices, test_size=0.2, random_state=args.seed, stratify=stratify)

    train_dataset = SequenceDataset(prepared.sequences[train_idx], prepared.masks[train_idx], prepared.labels[train_idx])
    val_dataset = SequenceDataset(prepared.sequences[val_idx], prepared.masks[val_idx], prepared.labels[val_idx])
    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = TemporalFareCNN(feature_dim=prepared.sequences.shape[2], class_count=len(prepared.label_names)).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
    criterion = nn.CrossEntropyLoss()

    best_val_acc = -1.0
    best_state = None
    history: list[dict[str, float]] = []

    for epoch in range(1, args.epochs + 1):
        model.train()
        running_loss = 0.0
        running_correct = 0
        seen = 0
        for sequences, masks, labels in train_loader:
            sequences = sequences.to(device)
            masks = masks.to(device)
            labels = labels.to(device)
            optimizer.zero_grad()
            logits = model(sequences, masks)
            loss = criterion(logits, labels)
            loss.backward()
            optimizer.step()
            running_loss += float(loss.item()) * labels.size(0)
            running_correct += int((logits.argmax(dim=1) == labels).sum().item())
            seen += int(labels.size(0))
        train_loss = running_loss / max(seen, 1)
        train_acc = running_correct / max(seen, 1)
        val_loss, val_acc = evaluate(model, val_loader, device)
        history.append({
            "epoch": epoch,
            "train_loss": train_loss,
            "train_accuracy": train_acc,
            "val_loss": val_loss,
            "val_accuracy": val_acc,
        })
        print(
            f"epoch={epoch:02d} train_loss={train_loss:.4f} train_acc={train_acc:.3f} "
            f"val_loss={val_loss:.4f} val_acc={val_acc:.3f}"
        )
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.detach().cpu() for k, v in model.state_dict().items()}

    if best_state is None:
        raise RuntimeError("Training did not produce a model state.")

    model_path = output_dir / "fare_class_cnn.pt"
    metadata_path = output_dir / "fare_class_cnn_metadata.json"
    torch.save(best_state, model_path)
    metadata = {
        **prepared.stats,
        "label_names": prepared.label_names,
        "feature_names": prepared.feature_names,
        "history": history,
        "best_val_accuracy": best_val_acc,
        "workbook": str(args.workbook),
        "sheet_name": args.sheet_name,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2))
    print(f"saved_model={model_path}")
    print(f"saved_metadata={metadata_path}")
    print(f"samples={prepared.stats['sample_count']} classes={prepared.stats['class_count']} best_val_acc={best_val_acc:.3f}")


if __name__ == "__main__":
    train(parse_args())
