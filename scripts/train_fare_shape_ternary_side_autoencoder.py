#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import train_fare_shape_autoencoder as base

try:
    import torch
    from torch import nn
    from torch.utils.data import DataLoader
except ImportError as exc:
    raise SystemExit(
        "PyTorch is required for this script. Install it with `pip install torch` and rerun."
    ) from exc

DEFAULT_WORKBOOK = Path("/Users/adammarq/Downloads/Flights Claw Final Project v6.xlsx")
DEFAULT_OUTPUT_DIR = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/output/fare_shape_ternary_side_autoencoder")


@dataclass
class PreparedSideData:
    sequences: np.ndarray
    masks: np.ndarray
    feature_names: list[str]
    sample_keys: list[str]
    observed_lengths: np.ndarray
    service_duration_minutes: np.ndarray
    stats: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", default=str(DEFAULT_WORKBOOK))
    parser.add_argument("--sheet-name", default=0)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--input-mode", choices=["accel_state_only", "delta_accel_state", "cumulative_accel_state_only", "delta_cumulative_accel_state"], default="delta_accel_state")
    parser.add_argument("--model-profile", choices=["deep4", "compact3"], default="compact3")
    parser.add_argument("--horizon-days", type=int, default=66)
    parser.add_argument("--min-days-until-departure", type=int, default=7)
    parser.add_argument("--accel-state-threshold", type=float, default=5.0)
    parser.add_argument("--max-seq-len", type=int, default=66)
    parser.add_argument("--min-observations", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--latent-dim", type=int, default=16)
    parser.add_argument("--cluster-count", type=int, default=3)
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--val-fraction", type=float, default=0.15)
    parser.add_argument("--test-fraction", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--split-strategy", choices=["random", "duration_stratified"], default="duration_stratified")
    return parser.parse_args()


def add_side_features(group: pd.DataFrame, horizon_days: int, accel_state_threshold: float) -> pd.DataFrame:
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
    group["price_acceleration_state"] = np.where(
        group["price_acceleration_per_day"] > accel_state_threshold,
        1.0,
        np.where(group["price_acceleration_per_day"] < -accel_state_threshold, -1.0, 0.0),
    )
    group["price_acceleration_state_cumulative"] = group["price_acceleration_state"].cumsum().astype(float)
    return group


def resolve_feature_names(input_mode: str) -> list[str]:
    if input_mode == "accel_state_only":
        return ["price_acceleration_state"]
    if input_mode == "cumulative_accel_state_only":
        return ["price_acceleration_state_cumulative"]
    if input_mode == "delta_cumulative_accel_state":
        return ["price_delta_per_day", "price_acceleration_state_cumulative"]
    return ["price_delta_per_day", "price_acceleration_state"]


def prepare_data(args: argparse.Namespace) -> PreparedSideData:
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

    feature_names = resolve_feature_names(args.input_mode)
    sequences: list[np.ndarray] = []
    masks: list[np.ndarray] = []
    sample_keys: list[str] = []
    observed_lengths: list[int] = []
    service_duration_minutes: list[float] = []

    for service_label, group in df.groupby("route_service_label", dropna=True):
        group = add_side_features(group, args.horizon_days, args.accel_state_threshold)
        if len(group) < args.min_observations:
            continue
        feature_frame = group[feature_names].apply(pd.to_numeric, errors="coerce").fillna(0.0)
        array = feature_frame.to_numpy(dtype=np.float32)
        if array.shape[0] > args.max_seq_len:
            array = array[-args.max_seq_len :]
        padded = np.zeros((args.max_seq_len, array.shape[1]), dtype=np.float32)
        mask = np.zeros(args.max_seq_len, dtype=np.float32)
        padded[-array.shape[0] :, :] = array
        mask[-array.shape[0] :] = 1.0
        sequences.append(padded)
        masks.append(mask)
        sample_keys.append(str(service_label))
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

    feature_weights = np.array([1.0 for _ in feature_names], dtype=np.float32)
    stats = {
        "sample_count": int(len(sequences)),
        "feature_count": int(normalized_sequences.shape[2]),
        "min_days_until_departure": int(args.min_days_until_departure),
        "max_seq_len": int(args.max_seq_len),
        "feature_profile": args.input_mode,
        "feature_names": feature_names,
        "feature_means": means.tolist(),
        "feature_stds": stds.tolist(),
        "feature_weights": feature_weights.tolist(),
        "accel_state_threshold": float(args.accel_state_threshold),
    }
    return PreparedSideData(
        normalized_sequences,
        mask_array.astype(np.float32),
        feature_names,
        sample_keys,
        np.array(observed_lengths, dtype=np.int64),
        np.array(service_duration_minutes, dtype=np.float32),
        stats,
    )


def train(args: argparse.Namespace) -> None:
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    prepared = prepare_data(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    split_ready = base.PreparedAutoencoderData(
        sequences=prepared.sequences,
        masks=prepared.masks,
        feature_names=prepared.feature_names,
        sample_keys=prepared.sample_keys,
        observed_lengths=prepared.observed_lengths,
        service_duration_minutes=prepared.service_duration_minutes,
        stats=prepared.stats,
    )
    train_idx, val_idx, test_idx, split_summary = base.make_splits(args, split_ready)
    print("split_summary=" + json.dumps(split_summary, indent=None))

    train_loader = DataLoader(base.SequenceAutoencoderDataset(prepared.sequences[train_idx], prepared.masks[train_idx]), batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(base.SequenceAutoencoderDataset(prepared.sequences[val_idx], prepared.masks[val_idx]), batch_size=args.batch_size, shuffle=False)
    test_loader = DataLoader(base.SequenceAutoencoderDataset(prepared.sequences[test_idx], prepared.masks[test_idx]), batch_size=args.batch_size, shuffle=False)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model_cls = base.CompactTemporalConvAutoencoder if args.model_profile == "compact3" else base.TemporalConvAutoencoder
    model = model_cls(feature_dim=prepared.sequences.shape[2], latent_dim=args.latent_dim, seq_len=args.max_seq_len).to(device)
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
            loss = base.masked_weighted_mse(reconstruction, sequences, masks, feature_weights)
            loss.backward()
            optimizer.step()
            train_loss_sum += float(loss.item()) * sequences.size(0)
            train_items += int(sequences.size(0))
        train_loss = train_loss_sum / max(train_items, 1)
        val_loss = base.evaluate(model, val_loader, device, feature_weights)
        history.append({"epoch": epoch, "train_reconstruction_loss": train_loss, "val_reconstruction_loss": val_loss})
        print(f"epoch={epoch:02d} train_recon_loss={train_loss:.5f} val_recon_loss={val_loss:.5f}")
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    if best_state is None:
        best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
    model.load_state_dict(best_state)

    test_loss = base.evaluate(model, test_loader, device, feature_weights)
    embeddings, reconstruction_loss, _ = base.extract_embeddings(model, prepared.sequences, prepared.masks, device, args.batch_size)
    kmeans = base.KMeans(n_clusters=min(args.cluster_count, len(prepared.sample_keys)), n_init=20, random_state=args.seed)
    labels = kmeans.fit_predict(embeddings)

    embeddings_df = pd.DataFrame(embeddings)
    embeddings_df.insert(0, "sample_key", prepared.sample_keys)
    embeddings_df.insert(1, "shape_cluster", labels)
    embeddings_df.insert(2, "reconstruction_loss", reconstruction_loss)
    embeddings_path = output_dir / "fare_shape_embeddings.csv"
    embeddings_df.to_csv(embeddings_path, index=False)

    cluster_summary = (
        embeddings_df.groupby("shape_cluster", as_index=False)
        .agg(sample_count=("sample_key", "size"), avg_reconstruction_loss=("reconstruction_loss", "mean"))
        .sort_values(["sample_count", "avg_reconstruction_loss"], ascending=[False, True])
    )
    cluster_summary_path = output_dir / "fare_shape_cluster_summary.csv"
    cluster_summary.to_csv(cluster_summary_path, index=False)

    model_path = output_dir / "fare_shape_autoencoder.pt"
    torch.save(model.state_dict(), model_path)

    metadata = {
        "history": history,
        "best_val_reconstruction_loss": best_val_loss,
        "test_reconstruction_loss": test_loss,
        "latent_dim": args.latent_dim,
        "model_profile": args.model_profile,
        "cluster_count_requested": args.cluster_count,
        "cluster_count_used": int(cluster_summary.shape[0]),
        "split_summary": split_summary,
        "workbook": args.workbook,
        "sheet_name": args.sheet_name,
        "feature_profile": args.input_mode,
        "feature_names": prepared.feature_names,
        "feature_means": prepared.stats["feature_means"],
        "feature_stds": prepared.stats["feature_stds"],
        "feature_weights": prepared.stats["feature_weights"],
        "min_days_until_departure": args.min_days_until_departure,
        "split_strategy": args.split_strategy,
        "accel_state_threshold": args.accel_state_threshold,
        "sample_keys_preview": prepared.sample_keys[:10],
    }
    metadata_path = output_dir / "fare_shape_autoencoder_metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2))

    print(f"saved_model={model_path}")
    print(f"saved_metadata={metadata_path}")
    print(f"saved_embeddings={embeddings_path}")
    print(f"saved_clusters={cluster_summary_path}")
    print(f"samples={prepared.stats['sample_count']} features={prepared.stats['feature_count']} best_val_recon_loss={best_val_loss:.5f} test_recon_loss={test_loss:.5f} train_samples={len(train_idx)}")


if __name__ == "__main__":
    train(parse_args())
