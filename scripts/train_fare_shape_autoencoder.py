#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
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
DEFAULT_OUTPUT_DIR = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/output/fare_shape_autoencoder")

TWO_FEATURE_STATE_MOTION_NUMERIC_COLUMNS = [
    "price_value",
]

TWO_FEATURE_STATE_MOTION_BOOLEAN_COLUMNS: list[str] = []

TWO_FEATURE_STATE_MOTION_DERIVED_COLUMNS = [
    "price_delta_per_day",
]

RGB_STATE_MOTION_NUMERIC_COLUMNS = [
    "price_value",
]

RGB_STATE_MOTION_BOOLEAN_COLUMNS: list[str] = []

RGB_STATE_MOTION_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_signed_log",
    "observed_flag",
]

RGB_STATE_MOTION_NO_FLAG_NUMERIC_COLUMNS = [
    "price_value",
]

RGB_STATE_MOTION_NO_FLAG_BOOLEAN_COLUMNS: list[str] = []

RGB_STATE_MOTION_NO_FLAG_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_signed_log",
]

VALUE_DELTA_OBSERVED_NUMERIC_COLUMNS = [
    "price_value",
]

VALUE_DELTA_OBSERVED_BOOLEAN_COLUMNS: list[str] = []

VALUE_DELTA_OBSERVED_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "observed_flag",
]

VALUE_DELTA_CHANGE_STATE_OBSERVED_NUMERIC_COLUMNS = [
    "price_value",
]

VALUE_DELTA_CHANGE_STATE_OBSERVED_BOOLEAN_COLUMNS: list[str] = []

VALUE_DELTA_CHANGE_STATE_OBSERVED_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "fare_change_state",
    "observed_flag",
]

VALUE_DELTA_CHANGE_STATE_5LEVEL_OBSERVED_NUMERIC_COLUMNS = [
    "price_value",
]

VALUE_DELTA_CHANGE_STATE_5LEVEL_OBSERVED_BOOLEAN_COLUMNS: list[str] = []

VALUE_DELTA_CHANGE_STATE_5LEVEL_OBSERVED_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "fare_change_state_5level",
    "observed_flag",
]

VALUE_DELTA_CUM_ACCEL_OBSERVED_NUMERIC_COLUMNS = [
    "price_value",
]

VALUE_DELTA_CUM_ACCEL_OBSERVED_BOOLEAN_COLUMNS: list[str] = []

VALUE_DELTA_CUM_ACCEL_OBSERVED_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_state_cumulative",
    "observed_flag",
]

RGB_MOTION_ACCEL_NUMERIC_COLUMNS: list[str] = []

RGB_MOTION_ACCEL_BOOLEAN_COLUMNS: list[str] = []

RGB_MOTION_ACCEL_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_signed_log",
    "observed_flag",
]

TRIMMED_DYNAMIC_NUMERIC_COLUMNS = [
    "days_until_departure",
    "price_value",
    "price_difference_prev_snapshot",
    "price_difference_prev_departure",
    "departure_minutes",
    "duration_minutes",
    "competing_nonstop_same_time_bucket_count",
    "price_diff_vs_cheapest_competing_nonstop",
    "competing_nonstop_other_airlines_same_time_bucket_count",
    "price_diff_vs_cheapest_other_airline_competing_nonstop",
    "alternate_nonstop_any_airline_count",
]

TRIMMED_DYNAMIC_BOOLEAN_COLUMNS = [
    "is_weekend_departure",
    "holiday_window_flag",
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

TRIMMED_DERIVED_COLUMNS = [
    "sequence_day_position",
    "days_gap_from_previous_observation",
    "price_delta_per_day",
    "price_acceleration_signed_log",
    "average_days_between_dates",
    "observed_flag",
]

DERIVED_COLUMNS = [
    "sequence_day_position",
    "days_gap_from_previous_observation",
    "price_delta_per_day",
    "price_acceleration_per_day",
    "price_acceleration_signed_log",
    "average_days_between_dates",
    "observed_flag",
]

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

WEIGHTED_FEATURES = {
    "price_value": 3.0,
    "price_delta_per_day": 2.5,
    "price_acceleration_per_day": 3.0,
    "price_acceleration_state_cumulative": 1.5,
    "fare_change_state": 1.5,
    "fare_change_state_5level": 1.5,
    "price_difference_prev_snapshot": 1.5,
    "price_difference_prev_departure": 1.5,
    "price_diff_vs_cheapest_competing_nonstop": 1.75,
    "price_diff_vs_cheapest_other_airline_competing_nonstop": 1.75,
    "competing_nonstop_same_time_bucket_count": 1.4,
    "competing_nonstop_other_airlines_same_time_bucket_count": 1.4,
    "observed_flag": 0.5,
}


@dataclass
class PreparedAutoencoderData:
    sequences: np.ndarray
    masks: np.ndarray
    feature_names: list[str]
    sample_keys: list[str]
    observed_lengths: np.ndarray
    service_duration_minutes: np.ndarray
    stats: dict[str, Any]


class SequenceAutoencoderDataset(Dataset):
    def __init__(self, sequences: np.ndarray, masks: np.ndarray):
        self.sequences = torch.tensor(sequences, dtype=torch.float32)
        self.masks = torch.tensor(masks, dtype=torch.float32)

    def __len__(self) -> int:
        return int(self.sequences.shape[0])

    def __getitem__(self, idx: int):
        return self.sequences[idx], self.masks[idx]


class TemporalConvAutoencoder(nn.Module):
    def __init__(self, feature_dim: int, latent_dim: int, seq_len: int):
        super().__init__()
        self.feature_dim = feature_dim
        self.seq_len = seq_len
        self.encoder = nn.Sequential(
            nn.Conv1d(feature_dim, 32, kernel_size=5, padding=2),
            nn.ReLU(),
            nn.BatchNorm1d(32),
            nn.Conv1d(32, 64, kernel_size=5, stride=2, padding=2),
            nn.ReLU(),
            nn.BatchNorm1d(64),
            nn.Conv1d(64, 128, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(128),
            nn.Conv1d(128, 128, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(128),
        )
        with torch.no_grad():
            dummy = torch.zeros(1, feature_dim, seq_len)
            encoded = self.encoder(dummy)
            self.encoded_shape = encoded.shape[1:]
            self.flattened_dim = int(encoded.numel())
        self.to_latent = nn.Sequential(
            nn.Flatten(),
            nn.Linear(self.flattened_dim, 256),
            nn.ReLU(),
            nn.Linear(256, latent_dim),
        )
        self.from_latent = nn.Sequential(
            nn.Linear(latent_dim, 256),
            nn.ReLU(),
            nn.Linear(256, self.flattened_dim),
            nn.ReLU(),
        )
        self.decoder = nn.Sequential(
            nn.ConvTranspose1d(128, 128, kernel_size=4, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(128),
            nn.ConvTranspose1d(128, 64, kernel_size=4, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(64),
            nn.ConvTranspose1d(64, 32, kernel_size=4, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(32),
            nn.Conv1d(32, feature_dim, kernel_size=3, padding=1),
        )

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        encoded = self.encoder(x.transpose(1, 2))
        return self.to_latent(encoded)

    def decode(self, latent: torch.Tensor) -> torch.Tensor:
        expanded = self.from_latent(latent)
        expanded = expanded.view(latent.shape[0], *self.encoded_shape)
        decoded = self.decoder(expanded)
        if decoded.shape[-1] > self.seq_len:
            decoded = decoded[..., : self.seq_len]
        elif decoded.shape[-1] < self.seq_len:
            decoded = nn.functional.pad(decoded, (0, self.seq_len - decoded.shape[-1]))
        return decoded.transpose(1, 2)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        latent = self.encode(x)
        reconstruction = self.decode(latent)
        return reconstruction, latent


class CompactTemporalConvAutoencoder(nn.Module):
    def __init__(self, feature_dim: int, latent_dim: int, seq_len: int):
        super().__init__()
        self.feature_dim = feature_dim
        self.seq_len = seq_len
        self.encoder = nn.Sequential(
            nn.Conv1d(feature_dim, 32, kernel_size=5, padding=2),
            nn.ReLU(),
            nn.BatchNorm1d(32),
            nn.Conv1d(32, 64, kernel_size=5, stride=2, padding=2),
            nn.ReLU(),
            nn.BatchNorm1d(64),
            nn.Conv1d(64, 96, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(96),
        )
        with torch.no_grad():
            dummy = torch.zeros(1, feature_dim, seq_len)
            encoded = self.encoder(dummy)
            self.encoded_shape = encoded.shape[1:]
            self.flattened_dim = int(encoded.numel())
        self.to_latent = nn.Sequential(
            nn.Flatten(),
            nn.Linear(self.flattened_dim, 192),
            nn.ReLU(),
            nn.Linear(192, latent_dim),
        )
        self.from_latent = nn.Sequential(
            nn.Linear(latent_dim, 192),
            nn.ReLU(),
            nn.Linear(192, self.flattened_dim),
            nn.ReLU(),
        )
        self.decoder = nn.Sequential(
            nn.ConvTranspose1d(96, 64, kernel_size=4, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(64),
            nn.ConvTranspose1d(64, 32, kernel_size=4, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(32),
            nn.Conv1d(32, feature_dim, kernel_size=3, padding=1),
        )

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        encoded = self.encoder(x.transpose(1, 2))
        return self.to_latent(encoded)

    def decode(self, latent: torch.Tensor) -> torch.Tensor:
        expanded = self.from_latent(latent)
        expanded = expanded.view(latent.shape[0], *self.encoded_shape)
        decoded = self.decoder(expanded)
        if decoded.shape[-1] > self.seq_len:
            decoded = decoded[..., : self.seq_len]
        elif decoded.shape[-1] < self.seq_len:
            decoded = nn.functional.pad(decoded, (0, self.seq_len - decoded.shape[-1]))
        return decoded.transpose(1, 2)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        latent = self.encode(x)
        reconstruction = self.decode(latent)
        return reconstruction, latent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", default=str(DEFAULT_WORKBOOK))
    parser.add_argument("--sheet-name", default=0)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_state_motion_accel_no_flag", "value_delta_observed", "value_delta_change_state_observed", "value_delta_change_state_5level_observed", "value_delta_cum_accel_observed", "rgb_motion_accel_mask", "state_motion_two_feature"], default="full")
    parser.add_argument("--model-profile", choices=["deep4", "compact3"], default="deep4")
    parser.add_argument("--horizon-days", type=int, default=66)
    parser.add_argument("--min-days-until-departure", type=int, default=1)
    parser.add_argument("--max-seq-len", type=int, default=66)
    parser.add_argument("--min-observations", type=int, default=8)
    parser.add_argument("--periodized-cum-accel-steps", type=int, default=48)
    parser.add_argument("--fare-change-threshold-minor", type=float, default=2.5)
    parser.add_argument("--fare-change-threshold", type=float, default=10.0)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--latent-dim", type=int, default=32)
    parser.add_argument("--cluster-count", type=int, default=8)
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--val-fraction", type=float, default=0.15)
    parser.add_argument("--test-fraction", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--split-strategy", choices=["random", "duration_stratified"], default="random")
    return parser.parse_args()


def normalize_bool(series: pd.Series) -> pd.Series:
    return series.fillna(False).astype(bool).astype(float)


def signed_log1p(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return np.sign(numeric) * np.log1p(np.abs(numeric))


def five_level_change_state(values: pd.Series | np.ndarray, minor_threshold: float, major_threshold: float) -> np.ndarray:
    numeric = np.asarray(values, dtype=np.float32)
    return np.where(
        numeric >= major_threshold,
        2.0,
        np.where(
            numeric >= minor_threshold,
            1.0,
            np.where(
                numeric <= -major_threshold,
                -2.0,
                np.where(numeric <= -minor_threshold, -1.0, 0.0),
            ),
        ),
    ).astype(np.float32)


def day_position(series: pd.Series, horizon_days: int) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return (horizon_days - numeric).clip(lower=0)


def one_hot_static(df: pd.DataFrame) -> pd.DataFrame:
    usable = [col for col in STATIC_CATEGORICAL_COLUMNS if col in df.columns]
    if not usable:
        return pd.DataFrame(index=df.index)
    static = df[usable].fillna("unknown").astype(str)
    return pd.get_dummies(static, prefix=usable, dtype=float)


def resolve_feature_profile(args: argparse.Namespace) -> tuple[list[str], list[str], list[str], bool]:
    if args.feature_profile == "state_motion_two_feature":
        return (
            TWO_FEATURE_STATE_MOTION_NUMERIC_COLUMNS,
            TWO_FEATURE_STATE_MOTION_BOOLEAN_COLUMNS,
            TWO_FEATURE_STATE_MOTION_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "rgb_state_motion_accel":
        return (
            RGB_STATE_MOTION_NUMERIC_COLUMNS,
            RGB_STATE_MOTION_BOOLEAN_COLUMNS,
            RGB_STATE_MOTION_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "rgb_state_motion_accel_no_flag":
        return (
            RGB_STATE_MOTION_NO_FLAG_NUMERIC_COLUMNS,
            RGB_STATE_MOTION_NO_FLAG_BOOLEAN_COLUMNS,
            RGB_STATE_MOTION_NO_FLAG_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "value_delta_observed":
        return (
            VALUE_DELTA_OBSERVED_NUMERIC_COLUMNS,
            VALUE_DELTA_OBSERVED_BOOLEAN_COLUMNS,
            VALUE_DELTA_OBSERVED_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "value_delta_change_state_observed":
        return (
            VALUE_DELTA_CHANGE_STATE_OBSERVED_NUMERIC_COLUMNS,
            VALUE_DELTA_CHANGE_STATE_OBSERVED_BOOLEAN_COLUMNS,
            VALUE_DELTA_CHANGE_STATE_OBSERVED_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "value_delta_change_state_5level_observed":
        return (
            VALUE_DELTA_CHANGE_STATE_5LEVEL_OBSERVED_NUMERIC_COLUMNS,
            VALUE_DELTA_CHANGE_STATE_5LEVEL_OBSERVED_BOOLEAN_COLUMNS,
            VALUE_DELTA_CHANGE_STATE_5LEVEL_OBSERVED_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "value_delta_cum_accel_observed":
        return (
            VALUE_DELTA_CUM_ACCEL_OBSERVED_NUMERIC_COLUMNS,
            VALUE_DELTA_CUM_ACCEL_OBSERVED_BOOLEAN_COLUMNS,
            VALUE_DELTA_CUM_ACCEL_OBSERVED_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "rgb_motion_accel_mask":
        return (
            RGB_MOTION_ACCEL_NUMERIC_COLUMNS,
            RGB_MOTION_ACCEL_BOOLEAN_COLUMNS,
            RGB_MOTION_ACCEL_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "trimmed":
        return (
            TRIMMED_DYNAMIC_NUMERIC_COLUMNS,
            TRIMMED_DYNAMIC_BOOLEAN_COLUMNS,
            TRIMMED_DERIVED_COLUMNS,
            False,
        )
    return (
        DYNAMIC_NUMERIC_COLUMNS,
        DYNAMIC_BOOLEAN_COLUMNS,
        DERIVED_COLUMNS,
        True,
    )


def add_sequence_features(group: pd.DataFrame, horizon_days: int, fare_change_threshold: float, fare_change_threshold_minor: float) -> pd.DataFrame:
    group = group.sort_values(["days_until_departure", "query_timestamp"], ascending=[False, True]).copy()
    group["days_until_departure"] = pd.to_numeric(group["days_until_departure"], errors="coerce")
    group["price_value"] = pd.to_numeric(group.get("price_value"), errors="coerce")
    group = group.dropna(subset=["days_until_departure"]).copy()
    group = group.drop_duplicates(subset=["days_until_departure"], keep="last")
    group["sequence_day_position"] = day_position(group["days_until_departure"], horizon_days)
    group["days_gap_from_previous_observation"] = group["days_until_departure"].diff().abs().fillna(0.0)
    group["price_delta_per_day"] = group["price_value"].diff().div(group["days_gap_from_previous_observation"].replace(0, np.nan))
    group["price_delta_per_day"] = group["price_delta_per_day"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    group["price_acceleration_per_day"] = group["price_delta_per_day"].diff().div(group["days_gap_from_previous_observation"].replace(0, np.nan))
    group["price_acceleration_per_day"] = group["price_acceleration_per_day"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    group["price_acceleration_signed_log"] = signed_log1p(group["price_acceleration_per_day"]).fillna(0.0)
    group["price_acceleration_state"] = np.where(
        group["price_acceleration_per_day"] > 5.0,
        1.0,
        np.where(group["price_acceleration_per_day"] < -5.0, -1.0, 0.0),
    )
    group["price_acceleration_state_cumulative"] = group["price_acceleration_state"].cumsum().astype(float)
    group["fare_change_state"] = np.where(
        group["price_delta_per_day"] > fare_change_threshold,
        1.0,
        np.where(group["price_delta_per_day"] < -fare_change_threshold, -1.0, 0.0),
    )
    group["fare_change_state_5level"] = five_level_change_state(
        group["price_delta_per_day"],
        fare_change_threshold_minor,
        fare_change_threshold,
    )
    avg_gap = float(group["days_gap_from_previous_observation"].replace(0, np.nan).mean()) if len(group) > 1 else 0.0
    group["average_days_between_dates"] = 0.0 if np.isnan(avg_gap) else avg_gap
    group["observed_flag"] = 1.0
    return group


def build_periodized_value_delta_frame(group: pd.DataFrame, target_steps: int, feature_profile: str, fare_change_threshold: float, fare_change_threshold_minor: float) -> pd.DataFrame:
    if target_steps <= 0:
        raise ValueError("target_steps must be positive")
    price_values = pd.to_numeric(group["price_value"], errors="coerce").ffill().bfill().fillna(0.0).to_numpy(dtype=np.float32)
    if price_values.shape[0] == 1:
        resampled_price = np.repeat(price_values, target_steps)
    else:
        source_x = np.linspace(1.0, target_steps, num=price_values.shape[0], dtype=np.float32)
        target_x = np.arange(1, target_steps + 1, dtype=np.float32)
        resampled_price = np.interp(target_x, source_x, price_values)
    delta = np.diff(resampled_price, prepend=resampled_price[0]).astype(np.float32)
    if delta.shape[0] > 0:
        delta[0] = 0.0
    frame = {
        "price_value": resampled_price.astype(np.float32),
        "price_delta_per_day": delta,
        "observed_flag": np.ones(target_steps, dtype=np.float32),
    }
    if feature_profile == "value_delta_cum_accel_observed":
        accel = np.diff(delta, prepend=delta[0]).astype(np.float32)
        if accel.shape[0] > 0:
            accel[0] = 0.0
        accel_state = np.where(accel > 1e-6, 1.0, np.where(accel < -1e-6, -1.0, 0.0)).astype(np.float32)
        frame["price_acceleration_state_cumulative"] = accel_state.cumsum().astype(np.float32)
    elif feature_profile == "value_delta_change_state_observed":
        frame["fare_change_state"] = np.where(delta > fare_change_threshold, 1.0, np.where(delta < -fare_change_threshold, -1.0, 0.0)).astype(np.float32)
    elif feature_profile == "value_delta_change_state_5level_observed":
        frame["fare_change_state_5level"] = five_level_change_state(delta, fare_change_threshold_minor, fare_change_threshold)
    return pd.DataFrame(frame)


def build_feature_frame(
    df: pd.DataFrame,
    static_dummies: pd.DataFrame,
    horizon_days: int,
    numeric_source: list[str],
    bool_source: list[str],
    derived_cols: list[str],
    fare_change_threshold: float,
    fare_change_threshold_minor: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    numeric_cols = [col for col in numeric_source if col in df.columns]
    bool_cols = [col for col in bool_source if col in df.columns]
    feature_names = numeric_cols + bool_cols + derived_cols + static_dummies.columns.tolist()
    grouped_rows: list[dict[str, Any]] = []
    for service_label, group in df.groupby("route_service_label", dropna=True):
        group = add_sequence_features(group, horizon_days, fare_change_threshold, fare_change_threshold_minor)
        grouped_rows.append(
            {
                "service_label": service_label,
                "group": group,
            }
        )
    return grouped_rows, feature_names


def prepare_data(args: argparse.Namespace) -> PreparedAutoencoderData:
    df = pd.read_excel(args.workbook, sheet_name=args.sheet_name)
    if "matched_target_flight" in df.columns:
        df = df[df["matched_target_flight"].fillna(False).astype(bool)].copy()
    df["query_timestamp"] = pd.to_datetime(df.get("query_timestamp"), errors="coerce")
    df["departure_date"] = pd.to_datetime(df.get("departure_date"), errors="coerce")
    if args.min_days_until_departure > args.horizon_days:
        raise ValueError("min-days-until-departure cannot exceed horizon-days")
    df = df[df["days_until_departure"].notna()].copy()
    df = df[pd.to_numeric(df["days_until_departure"], errors="coerce") >= args.min_days_until_departure].copy()
    df = df[pd.to_numeric(df["days_until_departure"], errors="coerce") <= args.horizon_days].copy()
    df = df[df["route_service_label"].notna() & df["departure_date"].notna()].copy()

    numeric_source, bool_source, derived_cols, include_static = resolve_feature_profile(args)
    static_dummies = one_hot_static(df) if include_static else pd.DataFrame(index=df.index)
    grouped_rows, feature_names = build_feature_frame(
        df,
        static_dummies,
        args.horizon_days,
        numeric_source,
        bool_source,
        derived_cols,
        args.fare_change_threshold,
        args.fare_change_threshold_minor,
    )

    sequences: list[np.ndarray] = []
    masks: list[np.ndarray] = []
    sample_keys: list[str] = []
    observed_lengths: list[int] = []
    service_duration_minutes: list[float] = []

    numeric_cols = [col for col in numeric_source if col in df.columns]
    bool_cols = [col for col in bool_source if col in df.columns]

    for item in grouped_rows:
        group = item["group"]
        if len(group) < args.min_observations:
            continue
        if args.feature_profile in {"value_delta_cum_accel_observed", "value_delta_change_state_observed", "value_delta_change_state_5level_observed"}:
            feature_frame = build_periodized_value_delta_frame(group, args.periodized_cum_accel_steps, args.feature_profile, args.fare_change_threshold, args.fare_change_threshold_minor)
            array = feature_frame[feature_names].to_numpy(dtype=np.float32)
            padded = np.zeros((args.max_seq_len, array.shape[1]), dtype=np.float32)
            mask = np.zeros(args.max_seq_len, dtype=np.float32)
            effective_len = min(array.shape[0], args.max_seq_len)
            padded[-effective_len:, :] = array[-effective_len:, :]
            mask[-effective_len:] = 1.0
            sequences.append(padded)
            masks.append(mask)
            sample_keys.append(str(item["service_label"]))
            observed_lengths.append(int(effective_len))
            service_duration = pd.to_numeric(group.get("duration_minutes"), errors="coerce")
            duration_value = float(service_duration.median()) if service_duration.notna().any() else float("nan")
            service_duration_minutes.append(duration_value)
            continue
        dynamic_part = pd.DataFrame(index=group.index)
        for col in numeric_cols:
            series = pd.to_numeric(group[col], errors="coerce")
            if col in {
                "price_difference_prev_snapshot",
                "price_difference_prev_departure",
                "price_difference_from_min_for_departure",
                "price_diff_vs_cheapest_competing_nonstop",
                "price_diff_vs_cheapest_other_airline_competing_nonstop",
                "price_diff_vs_cheapest_same_airline_alternate_nonstop",
                "price_diff_vs_cheapest_different_airline_alternate_nonstop",
                "number_of_days_since_last_flight",
                "first_day_offered",
                "last_day_offered",
            }:
                series = signed_log1p(series)
            dynamic_part[col] = series
        for col in bool_cols:
            dynamic_part[col] = normalize_bool(group[col])
        for col in derived_cols:
            dynamic_part[col] = pd.to_numeric(group[col], errors="coerce").fillna(0.0)
        static_part = static_dummies.loc[group.index].copy() if not static_dummies.empty else pd.DataFrame(index=group.index)
        feature_frame = pd.concat([dynamic_part, static_part], axis=1).fillna(0.0)
        array = feature_frame.to_numpy(dtype=np.float32)
        if array.shape[0] > args.max_seq_len:
            array = array[-args.max_seq_len :]
        padded = np.zeros((args.max_seq_len, array.shape[1]), dtype=np.float32)
        mask = np.zeros(args.max_seq_len, dtype=np.float32)
        padded[-array.shape[0] :, :] = array
        mask[-array.shape[0] :] = 1.0
        sequences.append(padded)
        masks.append(mask)
        sample_keys.append(str(item["service_label"]))
        observed_lengths.append(int(array.shape[0]))
        service_duration = pd.to_numeric(group.get("duration_minutes"), errors="coerce")
        duration_value = float(service_duration.median()) if service_duration.notna().any() else float("nan")
        service_duration_minutes.append(duration_value)

    if not sequences:
        raise ValueError("No training samples were created. Try lowering --min-observations or checking the workbook.")

    sequence_array = np.stack(sequences)
    mask_array = np.stack(masks)
    valid = mask_array[..., None] > 0
    means = (sequence_array * valid).sum(axis=(0, 1)) / valid.sum(axis=(0, 1)).clip(min=1)
    variances = (((sequence_array - means) * valid) ** 2).sum(axis=(0, 1)) / valid.sum(axis=(0, 1)).clip(min=1)
    stds = np.sqrt(np.maximum(variances, 1e-6))
    normalized_sequences = np.where(valid, (sequence_array - means) / stds, 0.0).astype(np.float32)

    feature_weights = np.array([WEIGHTED_FEATURES.get(name, 1.0) for name in feature_names], dtype=np.float32)
    stats = {
        "sample_count": int(len(sequences)),
        "feature_count": int(normalized_sequences.shape[2]),
        "min_days_until_departure": int(args.min_days_until_departure),
        "max_seq_len": int(args.max_seq_len),
        "periodized_cum_accel_steps": int(args.periodized_cum_accel_steps),
        "fare_change_threshold_minor": float(args.fare_change_threshold_minor),
        "fare_change_threshold": float(args.fare_change_threshold),
        "feature_profile": args.feature_profile,
        "feature_names": feature_names,
        "feature_means": means.tolist(),
        "feature_stds": stds.tolist(),
        "feature_weights": feature_weights.tolist(),
    }
    return PreparedAutoencoderData(
        normalized_sequences,
        mask_array.astype(np.float32),
        feature_names,
        sample_keys,
        np.array(observed_lengths, dtype=np.int64),
        np.array(service_duration_minutes, dtype=np.float32),
        stats,
    )


def summarize_split(name: str, indices: np.ndarray, prepared: PreparedAutoencoderData) -> dict[str, float]:
    lengths = prepared.observed_lengths[indices]
    durations = prepared.service_duration_minutes[indices]
    valid_durations = durations[~np.isnan(durations)]
    return {
        "count": int(indices.shape[0]),
        "avg_observed_length": float(lengths.mean()) if lengths.size else 0.0,
        "min_observed_length": int(lengths.min()) if lengths.size else 0,
        "max_observed_length": int(lengths.max()) if lengths.size else 0,
        "avg_service_duration_minutes": float(valid_durations.mean()) if valid_durations.size else 0.0,
    }


def build_duration_stratify_labels(prepared: PreparedAutoencoderData) -> np.ndarray | None:
    durations = prepared.service_duration_minutes.astype(np.float64)
    if durations.size < 8:
        return None
    if np.isnan(durations).all():
        return None
    if np.isnan(durations).any():
        fill_value = np.nanmedian(durations)
        durations = np.where(np.isnan(durations), fill_value, durations)
    unique_count = int(np.unique(durations).shape[0])
    if unique_count < 2:
        return None
    quantile_count = min(4, unique_count)
    labels = pd.qcut(durations, q=quantile_count, labels=False, duplicates="drop")
    if labels is None:
        return None
    labels = np.asarray(labels)
    if np.unique(labels).shape[0] < 2:
        return None
    counts = pd.Series(labels).value_counts()
    if (counts < 2).any():
        return None
    return labels


def make_splits(args: argparse.Namespace, prepared: PreparedAutoencoderData) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, dict[str, float]]]:
    total_fraction = args.train_fraction + args.val_fraction + args.test_fraction
    if not np.isclose(total_fraction, 1.0, atol=1e-6):
        raise ValueError("train-fraction, val-fraction, and test-fraction must sum to 1.0")
    indices = np.arange(prepared.sequences.shape[0])
    stratify_labels = None
    if args.split_strategy == "duration_stratified":
        stratify_labels = build_duration_stratify_labels(prepared)
    train_idx, temp_idx = train_test_split(
        indices,
        test_size=(1.0 - args.train_fraction),
        random_state=args.seed,
        shuffle=True,
        stratify=stratify_labels,
    )
    temp_fraction = args.val_fraction + args.test_fraction
    if temp_idx.size == 0:
        raise ValueError("No samples allocated to validation/test. Adjust split fractions.")
    val_share_of_temp = args.val_fraction / temp_fraction if temp_fraction > 0 else 0.5
    temp_stratify = None
    if stratify_labels is not None:
        temp_candidate = stratify_labels[temp_idx]
        temp_counts = pd.Series(temp_candidate).value_counts()
        if not (temp_counts < 2).any() and temp_counts.shape[0] >= 2:
            temp_stratify = temp_candidate
    val_idx, test_idx = train_test_split(
        temp_idx,
        train_size=val_share_of_temp,
        random_state=args.seed,
        shuffle=True,
        stratify=temp_stratify,
    )
    split_summary = {
        "train": summarize_split("train", train_idx, prepared),
        "val": summarize_split("val", val_idx, prepared),
        "test": summarize_split("test", test_idx, prepared),
    }
    return train_idx, val_idx, test_idx, split_summary


def masked_weighted_mse(
    prediction: torch.Tensor,
    target: torch.Tensor,
    mask: torch.Tensor,
    feature_weights: torch.Tensor,
) -> torch.Tensor:
    expanded_mask = mask.unsqueeze(-1)
    expanded_weights = feature_weights.view(1, 1, -1)
    squared_error = (prediction - target) ** 2
    weighted = squared_error * expanded_mask * expanded_weights
    denom = (expanded_mask * expanded_weights).sum().clamp(min=1.0)
    return weighted.sum() / denom


def evaluate(model: nn.Module, loader: DataLoader, device: torch.device, feature_weights: torch.Tensor) -> float:
    model.eval()
    total_loss = 0.0
    total_items = 0
    with torch.no_grad():
        for sequences, masks in loader:
            sequences = sequences.to(device)
            masks = masks.to(device)
            reconstruction, _ = model(sequences)
            loss = masked_weighted_mse(reconstruction, sequences, masks, feature_weights)
            total_loss += float(loss.item()) * sequences.size(0)
            total_items += int(sequences.size(0))
    return total_loss / max(total_items, 1)


def extract_embeddings(
    model: nn.Module,
    sequences: np.ndarray,
    masks: np.ndarray,
    device: torch.device,
    batch_size: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    loader = DataLoader(SequenceAutoencoderDataset(sequences, masks), batch_size=batch_size, shuffle=False)
    embeddings: list[np.ndarray] = []
    reconstructions: list[np.ndarray] = []
    losses: list[np.ndarray] = []
    model.eval()
    with torch.no_grad():
        for batch_sequences, batch_masks in loader:
            batch_sequences = batch_sequences.to(device)
            batch_masks = batch_masks.to(device)
            reconstruction, latent = model(batch_sequences)
            per_item = ((reconstruction - batch_sequences) ** 2 * batch_masks.unsqueeze(-1)).sum(dim=(1, 2))
            denom = batch_masks.sum(dim=1).clamp(min=1.0) * batch_sequences.shape[2]
            per_item = (per_item / denom).detach().cpu().numpy()
            embeddings.append(latent.detach().cpu().numpy())
            reconstructions.append(reconstruction.detach().cpu().numpy())
            losses.append(per_item)
    return np.vstack(embeddings), np.concatenate(losses), np.vstack([r.reshape(r.shape[0], -1) for r in reconstructions])


def train(args: argparse.Namespace) -> None:
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    prepared = prepare_data(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    train_idx, val_idx, test_idx, split_summary = make_splits(args, prepared)
    print(
        "split_summary="
        + json.dumps(split_summary, indent=None)
    )

    train_loader = DataLoader(
        SequenceAutoencoderDataset(prepared.sequences[train_idx], prepared.masks[train_idx]),
        batch_size=args.batch_size,
        shuffle=True,
    )
    val_loader = DataLoader(
        SequenceAutoencoderDataset(prepared.sequences[val_idx], prepared.masks[val_idx]),
        batch_size=args.batch_size,
        shuffle=False,
    )
    test_loader = DataLoader(
        SequenceAutoencoderDataset(prepared.sequences[test_idx], prepared.masks[test_idx]),
        batch_size=args.batch_size,
        shuffle=False,
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model_cls = CompactTemporalConvAutoencoder if args.model_profile == "compact3" else TemporalConvAutoencoder
    model = model_cls(
        feature_dim=prepared.sequences.shape[2],
        latent_dim=args.latent_dim,
        seq_len=args.max_seq_len,
    ).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
    feature_weights = torch.tensor(prepared.stats["feature_weights"], dtype=torch.float32, device=device)

    best_val_loss = float("inf")
    best_state = None
    history: list[dict[str, float]] = []

    for epoch in range(1, args.epochs + 1):
        model.train()
        train_loss_sum = 0.0
        train_items = 0
        for sequences, masks in train_loader:
            sequences = sequences.to(device)
            masks = masks.to(device)
            optimizer.zero_grad()
            reconstruction, _ = model(sequences)
            loss = masked_weighted_mse(reconstruction, sequences, masks, feature_weights)
            loss.backward()
            optimizer.step()
            train_loss_sum += float(loss.item()) * sequences.size(0)
            train_items += int(sequences.size(0))
        train_loss = train_loss_sum / max(train_items, 1)
        val_loss = evaluate(model, val_loader, device, feature_weights)
        history.append({"epoch": epoch, "train_reconstruction_loss": train_loss, "val_reconstruction_loss": val_loss})
        print(f"epoch={epoch:02d} train_recon_loss={train_loss:.5f} val_recon_loss={val_loss:.5f}")
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.detach().cpu() for k, v in model.state_dict().items()}

    if best_state is None:
        raise RuntimeError("Training did not produce a model state.")

    model.load_state_dict(best_state)
    test_loss = evaluate(model, test_loader, device, feature_weights)
    embeddings, reconstruction_loss_per_sample, _ = extract_embeddings(
        model,
        prepared.sequences,
        prepared.masks,
        device,
        args.batch_size,
    )

    effective_clusters = max(2, min(args.cluster_count, embeddings.shape[0]))
    kmeans = KMeans(n_clusters=effective_clusters, random_state=args.seed, n_init=20)
    cluster_ids = kmeans.fit_predict(embeddings)

    embedding_columns = [f"latent_{idx:02d}" for idx in range(embeddings.shape[1])]
    embeddings_df = pd.DataFrame(embeddings, columns=embedding_columns)
    embeddings_df.insert(0, "sample_key", prepared.sample_keys)
    embeddings_df["shape_cluster"] = cluster_ids
    embeddings_df["reconstruction_loss"] = reconstruction_loss_per_sample

    cluster_summary = (
        embeddings_df.groupby("shape_cluster")
        .agg(sample_count=("sample_key", "count"), avg_reconstruction_loss=("reconstruction_loss", "mean"))
        .reset_index()
        .sort_values(["sample_count", "avg_reconstruction_loss"], ascending=[False, True])
    )

    model_path = output_dir / "fare_shape_autoencoder.pt"
    metadata_path = output_dir / "fare_shape_autoencoder_metadata.json"
    embeddings_path = output_dir / "fare_shape_embeddings.csv"
    clusters_path = output_dir / "fare_shape_cluster_summary.csv"

    torch.save(best_state, model_path)
    embeddings_df.to_csv(embeddings_path, index=False)
    cluster_summary.to_csv(clusters_path, index=False)

    metadata = {
        **prepared.stats,
        "history": history,
        "best_val_reconstruction_loss": best_val_loss,
        "test_reconstruction_loss": test_loss,
        "latent_dim": args.latent_dim,
        "model_profile": args.model_profile,
        "cluster_count_requested": args.cluster_count,
        "cluster_count_used": effective_clusters,
        "split_summary": split_summary,
        "workbook": str(args.workbook),
        "sheet_name": args.sheet_name,
        "sample_keys_preview": prepared.sample_keys[:10],
    }
    metadata_path.write_text(json.dumps(metadata, indent=2))

    print(f"saved_model={model_path}")
    print(f"saved_metadata={metadata_path}")
    print(f"saved_embeddings={embeddings_path}")
    print(f"saved_clusters={clusters_path}")
    print(
        f"samples={prepared.stats['sample_count']} features={prepared.stats['feature_count']} "
        f"best_val_recon_loss={best_val_loss:.5f} test_recon_loss={test_loss:.5f} train_samples={len(train_idx)}"
    )


if __name__ == "__main__":
    train(parse_args())
