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
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset

import train_fare_shape_autoencoder as base

DEFAULT_WORKBOOK = Path('/Users/adammarq/Downloads/Flights Claw Final Project v6.xlsx')
DEFAULT_OUTPUT_DIR = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/output/fare_shape_image_autoencoder')

PRICE_BIN_EDGES = np.arange(0.0, 2000.0 + 100.0, 100.0, dtype=np.float32)
DELTA_BIN_EDGES = np.arange(-100.0, 100.0 + 10.0, 10.0, dtype=np.float32)
ACCEL_BIN_EDGES = np.arange(-5.0, 5.0 + 0.5, 0.5, dtype=np.float32)
OBSERVED_BIN_COUNT = 20
IMAGE_CHANNELS = 4
IMAGE_HEIGHT = 20


@dataclass
class PreparedImageData:
    sequences: np.ndarray
    masks: np.ndarray
    feature_names: list[str]
    sample_keys: list[str]
    observed_lengths: np.ndarray
    service_duration_minutes: np.ndarray
    stats: dict[str, Any]


class ImageAutoencoderDataset(Dataset):
    def __init__(self, sequences: np.ndarray, masks: np.ndarray):
        self.sequences = torch.tensor(sequences, dtype=torch.float32)
        self.masks = torch.tensor(masks, dtype=torch.float32)

    def __len__(self) -> int:
        return int(self.sequences.shape[0])

    def __getitem__(self, idx: int):
        return self.sequences[idx], self.masks[idx]


class BinnedConv2dAutoencoder(nn.Module):
    def __init__(self, channels: int, height: int, width: int, latent_dim: int, first_filters: int):
        super().__init__()
        self.channels = channels
        self.height = height
        self.width = width
        second_filters = first_filters * 2
        third_filters = first_filters * 3
        self.encoder = nn.Sequential(
            nn.Conv2d(channels, first_filters, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.BatchNorm2d(first_filters),
            nn.Conv2d(first_filters, second_filters, kernel_size=3, stride=(1, 2), padding=1),
            nn.ReLU(),
            nn.BatchNorm2d(second_filters),
            nn.Conv2d(second_filters, third_filters, kernel_size=3, stride=(1, 2), padding=1),
            nn.ReLU(),
            nn.BatchNorm2d(third_filters),
        )
        with torch.no_grad():
            dummy = torch.zeros(1, channels, height, width)
            encoded = self.encoder(dummy)
            self.encoded_shape = encoded.shape[1:]
            self.flattened_dim = int(encoded.numel())
        self.to_latent = nn.Sequential(
            nn.Flatten(),
            nn.Linear(self.flattened_dim, 128),
            nn.ReLU(),
            nn.Linear(128, latent_dim),
        )
        self.from_latent = nn.Sequential(
            nn.Linear(latent_dim, 128),
            nn.ReLU(),
            nn.Linear(128, self.flattened_dim),
            nn.ReLU(),
        )
        self.decoder = nn.Sequential(
            nn.ConvTranspose2d(third_filters, second_filters, kernel_size=4, stride=(1, 2), padding=1),
            nn.ReLU(),
            nn.BatchNorm2d(second_filters),
            nn.ConvTranspose2d(second_filters, first_filters, kernel_size=4, stride=(1, 2), padding=1),
            nn.ReLU(),
            nn.BatchNorm2d(first_filters),
            nn.Conv2d(first_filters, channels, kernel_size=3, padding=1),
        )

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        encoded = self.encoder(x)
        return self.to_latent(encoded)

    def decode(self, latent: torch.Tensor) -> torch.Tensor:
        expanded = self.from_latent(latent)
        expanded = expanded.view(latent.shape[0], *self.encoded_shape)
        decoded = self.decoder(expanded)
        if decoded.shape[-2] > self.height:
            decoded = decoded[:, :, : self.height, :]
        elif decoded.shape[-2] < self.height:
            decoded = nn.functional.pad(decoded, (0, 0, 0, self.height - decoded.shape[-2]))
        if decoded.shape[-1] > self.width:
            decoded = decoded[..., : self.width]
        elif decoded.shape[-1] < self.width:
            decoded = nn.functional.pad(decoded, (0, self.width - decoded.shape[-1], 0, 0))
        return decoded

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        latent = self.encode(x)
        reconstruction = self.decode(latent)
        return reconstruction, latent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--workbook', default=str(DEFAULT_WORKBOOK))
    parser.add_argument('--sheet-name', default=0)
    parser.add_argument('--output-dir', default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument('--horizon-days', type=int, default=66)
    parser.add_argument('--min-days-until-departure', type=int, default=1)
    parser.add_argument('--periodized-cum-accel-steps', type=int, default=48)
    parser.add_argument('--min-observations', type=int, default=8)
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--epochs', type=int, default=30)
    parser.add_argument('--learning-rate', type=float, default=1e-3)
    parser.add_argument('--latent-dim', type=int, default=32)
    parser.add_argument('--first-filters', type=int, default=32)
    parser.add_argument('--cluster-count', type=int, default=8)
    parser.add_argument('--train-fraction', type=float, default=0.7)
    parser.add_argument('--val-fraction', type=float, default=0.15)
    parser.add_argument('--test-fraction', type=float, default=0.15)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--split-strategy', choices=['random', 'duration_stratified'], default='random')
    return parser.parse_args()


def to_bin_index(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    idx = np.digitize(values, edges, right=False) - 1
    return np.clip(idx, 0, len(edges) - 2)


def build_observed_band(observed: np.ndarray, height: int) -> np.ndarray:
    band = np.zeros((height, observed.shape[0]), dtype=np.float32)
    if observed.size == 0:
        return band
    active_rows = int(round(height * 0.5))
    active_rows = min(max(active_rows, 1), height)
    band[-active_rows:, observed > 0] = 1.0
    return band


def build_image_tensor(frame: pd.DataFrame) -> np.ndarray:
    width = frame.shape[0]
    image = np.zeros((IMAGE_CHANNELS, IMAGE_HEIGHT, width), dtype=np.float32)

    price_idx = to_bin_index(frame['price_value'].to_numpy(dtype=np.float32), PRICE_BIN_EDGES)
    delta_idx = to_bin_index(frame['price_delta_per_day'].to_numpy(dtype=np.float32), DELTA_BIN_EDGES)
    accel_idx = to_bin_index(frame['price_acceleration_state_cumulative'].to_numpy(dtype=np.float32), ACCEL_BIN_EDGES)
    observed = frame['observed_flag'].to_numpy(dtype=np.float32)

    cols = np.arange(width)
    image[0, price_idx, cols] = 1.0
    image[1, delta_idx, cols] = 1.0
    image[2, accel_idx, cols] = 1.0
    image[3] = build_observed_band(observed, IMAGE_HEIGHT)
    return image


def prepare_data(args: argparse.Namespace) -> PreparedImageData:
    df = pd.read_excel(args.workbook, sheet_name=args.sheet_name)
    if 'matched_target_flight' in df.columns:
        df = df[df['matched_target_flight'].fillna(False).astype(bool)].copy()
    df['query_timestamp'] = pd.to_datetime(df.get('query_timestamp'), errors='coerce')
    df['departure_date'] = pd.to_datetime(df.get('departure_date'), errors='coerce')
    if args.min_days_until_departure > args.horizon_days:
        raise ValueError('min-days-until-departure cannot exceed horizon-days')
    df = df[df['days_until_departure'].notna()].copy()
    df = df[pd.to_numeric(df['days_until_departure'], errors='coerce') >= args.min_days_until_departure].copy()
    df = df[pd.to_numeric(df['days_until_departure'], errors='coerce') <= args.horizon_days].copy()
    df = df[df['route_service_label'].notna() & df['departure_date'].notna()].copy()

    grouped_rows, _ = base.build_feature_frame(df, pd.DataFrame(index=df.index), args.horizon_days, ['price_value'], [], ['price_delta_per_day', 'price_acceleration_state_cumulative', 'observed_flag'])

    sequences: list[np.ndarray] = []
    masks: list[np.ndarray] = []
    sample_keys: list[str] = []
    observed_lengths: list[int] = []
    service_duration_minutes: list[float] = []

    for item in grouped_rows:
        group = item['group']
        if len(group) < args.min_observations:
            continue
        frame = base.build_periodized_value_delta_cum_accel_frame(group, args.periodized_cum_accel_steps)
        sequences.append(build_image_tensor(frame))
        masks.append(np.ones(args.periodized_cum_accel_steps, dtype=np.float32))
        sample_keys.append(str(item['service_label']))
        observed_lengths.append(int(args.periodized_cum_accel_steps))
        service_duration = pd.to_numeric(group.get('duration_minutes'), errors='coerce')
        duration_value = float(service_duration.median()) if service_duration.notna().any() else float('nan')
        service_duration_minutes.append(duration_value)

    if not sequences:
        raise ValueError('No training samples were created. Try lowering --min-observations or checking the workbook.')

    sequence_array = np.stack(sequences)
    mask_array = np.stack(masks)
    feature_names = ['price_bin_image', 'delta_bin_image', 'accel_bin_image', 'observed_bin_image']
    stats = {
        'sample_count': int(len(sequences)),
        'feature_count': int(sequence_array.shape[1]),
        'image_height': int(sequence_array.shape[2]),
        'image_width': int(sequence_array.shape[3]),
        'min_days_until_departure': int(args.min_days_until_departure),
        'periodized_cum_accel_steps': int(args.periodized_cum_accel_steps),
        'feature_profile': 'binned_image_price_delta_cum_accel_observed',
        'feature_names': feature_names,
        'price_bin_edges': PRICE_BIN_EDGES.tolist(),
        'delta_bin_edges': DELTA_BIN_EDGES.tolist(),
        'accel_bin_edges': ACCEL_BIN_EDGES.tolist(),
        'feature_weights': [1.0, 1.0, 1.0, 0.5],
    }
    return PreparedImageData(sequence_array, mask_array, feature_names, sample_keys, np.array(observed_lengths, dtype=np.int64), np.array(service_duration_minutes, dtype=np.float32), stats)


def summarize_split(name: str, indices: np.ndarray, prepared: PreparedImageData) -> dict[str, float]:
    observed = prepared.observed_lengths[indices] if indices.size else np.array([], dtype=np.int64)
    durations = prepared.service_duration_minutes[indices] if indices.size else np.array([], dtype=np.float32)
    return {
        'count': int(indices.size),
        'avg_observed_length': float(observed.mean()) if observed.size else 0.0,
        'min_observed_length': int(observed.min()) if observed.size else 0,
        'max_observed_length': int(observed.max()) if observed.size else 0,
        'avg_service_duration_minutes': float(np.nanmean(durations)) if durations.size else 0.0,
    }


def make_splits(args: argparse.Namespace, prepared: PreparedImageData):
    split_ready = base.PreparedAutoencoderData(
        sequences=np.zeros((prepared.sequences.shape[0], prepared.sequences.shape[3], 1), dtype=np.float32),
        masks=prepared.masks,
        feature_names=prepared.feature_names,
        sample_keys=prepared.sample_keys,
        observed_lengths=prepared.observed_lengths,
        service_duration_minutes=prepared.service_duration_minutes,
        stats=prepared.stats,
    )
    return base.make_splits(args, split_ready)


def masked_mse_2d(prediction: torch.Tensor, target: torch.Tensor, mask: torch.Tensor, feature_weights: torch.Tensor) -> torch.Tensor:
    expanded_mask = mask.view(mask.shape[0], 1, 1, mask.shape[1])
    expanded_weights = feature_weights.view(1, -1, 1, 1)
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
            loss = masked_mse_2d(reconstruction, sequences, masks, feature_weights)
            total_loss += float(loss.item()) * sequences.size(0)
            total_items += int(sequences.size(0))
    return total_loss / max(total_items, 1)


def extract_embeddings(model: nn.Module, sequences: np.ndarray, masks: np.ndarray, device: torch.device, batch_size: int):
    loader = DataLoader(ImageAutoencoderDataset(sequences, masks), batch_size=batch_size, shuffle=False)
    embeddings: list[np.ndarray] = []
    losses: list[np.ndarray] = []
    recons: list[np.ndarray] = []
    model.eval()
    with torch.no_grad():
        for batch_sequences, batch_masks in loader:
            batch_sequences = batch_sequences.to(device)
            batch_masks = batch_masks.to(device)
            reconstruction, latent = model(batch_sequences)
            per_item = ((reconstruction - batch_sequences) ** 2 * batch_masks.view(batch_masks.shape[0], 1, 1, batch_masks.shape[1])).sum(dim=(1, 2, 3))
            denom = batch_masks.sum(dim=1).clamp(min=1.0) * batch_sequences.shape[1] * batch_sequences.shape[2]
            per_item = (per_item / denom).detach().cpu().numpy()
            embeddings.append(latent.detach().cpu().numpy())
            losses.append(per_item)
            recons.append(reconstruction.detach().cpu().numpy())
    return np.vstack(embeddings), np.concatenate(losses), np.concatenate(recons, axis=0)


def train(args: argparse.Namespace) -> None:
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    prepared = prepare_data(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    train_idx, val_idx, test_idx, split_summary = make_splits(args, prepared)
    print('split_summary=' + json.dumps(split_summary, indent=None))

    train_loader = DataLoader(ImageAutoencoderDataset(prepared.sequences[train_idx], prepared.masks[train_idx]), batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(ImageAutoencoderDataset(prepared.sequences[val_idx], prepared.masks[val_idx]), batch_size=args.batch_size, shuffle=False)
    test_loader = DataLoader(ImageAutoencoderDataset(prepared.sequences[test_idx], prepared.masks[test_idx]), batch_size=args.batch_size, shuffle=False)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = BinnedConv2dAutoencoder(
        channels=prepared.sequences.shape[1],
        height=prepared.sequences.shape[2],
        width=prepared.sequences.shape[3],
        latent_dim=args.latent_dim,
        first_filters=args.first_filters,
    ).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
    feature_weights = torch.tensor(prepared.stats['feature_weights'], dtype=torch.float32, device=device)

    best_val_loss = float('inf')
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
            loss = masked_mse_2d(reconstruction, sequences, masks, feature_weights)
            loss.backward()
            optimizer.step()
            train_loss_sum += float(loss.item()) * sequences.size(0)
            train_items += int(sequences.size(0))
        train_loss = train_loss_sum / max(train_items, 1)
        val_loss = evaluate(model, val_loader, device, feature_weights)
        history.append({'epoch': epoch, 'train_reconstruction_loss': train_loss, 'val_reconstruction_loss': val_loss})
        print(f'epoch={epoch:02d} train_recon_loss={train_loss:.5f} val_recon_loss={val_loss:.5f}')
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    if best_state is None:
        raise RuntimeError('Training did not produce a model state.')

    model.load_state_dict(best_state)
    test_loss = evaluate(model, test_loader, device, feature_weights)
    embeddings, reconstruction_loss_per_sample, _ = extract_embeddings(model, prepared.sequences, prepared.masks, device, args.batch_size)

    effective_clusters = max(2, min(args.cluster_count, embeddings.shape[0]))
    kmeans = KMeans(n_clusters=effective_clusters, random_state=args.seed, n_init=20)
    cluster_ids = kmeans.fit_predict(embeddings)

    embedding_columns = [f'latent_{idx:02d}' for idx in range(embeddings.shape[1])]
    embeddings_df = pd.DataFrame(embeddings, columns=embedding_columns)
    embeddings_df.insert(0, 'sample_key', prepared.sample_keys)
    embeddings_df['shape_cluster'] = cluster_ids
    embeddings_df['reconstruction_loss'] = reconstruction_loss_per_sample

    cluster_summary = (
        embeddings_df.groupby('shape_cluster')
        .agg(sample_count=('sample_key', 'count'), avg_reconstruction_loss=('reconstruction_loss', 'mean'))
        .reset_index()
        .sort_values(['sample_count', 'avg_reconstruction_loss'], ascending=[False, True])
    )

    model_path = output_dir / 'fare_shape_autoencoder.pt'
    metadata_path = output_dir / 'fare_shape_autoencoder_metadata.json'
    embeddings_path = output_dir / 'fare_shape_embeddings.csv'
    clusters_path = output_dir / 'fare_shape_cluster_summary.csv'

    torch.save(best_state, model_path)
    embeddings_df.to_csv(embeddings_path, index=False)
    cluster_summary.to_csv(clusters_path, index=False)

    metadata = {
        **prepared.stats,
        'history': history,
        'best_val_reconstruction_loss': best_val_loss,
        'test_reconstruction_loss': test_loss,
        'latent_dim': args.latent_dim,
        'model_profile': f'binned_conv2d_{args.first_filters}_{args.first_filters * 2}_{args.first_filters * 3}',
        'first_filters': args.first_filters,
        'cluster_count_requested': args.cluster_count,
        'cluster_count_used': effective_clusters,
        'split_summary': split_summary,
        'workbook': str(args.workbook),
        'sheet_name': args.sheet_name,
        'sample_keys_preview': prepared.sample_keys[:10],
    }
    metadata_path.write_text(json.dumps(metadata, indent=2))

    print(f'saved_model={model_path}')
    print(f'saved_metadata={metadata_path}')
    print(f'saved_embeddings={embeddings_path}')
    print(f'saved_clusters={clusters_path}')
    print(f"samples={prepared.stats['sample_count']} features={prepared.stats['feature_count']} best_val_recon_loss={best_val_loss:.5f} test_recon_loss={test_loss:.5f} train_samples={len(train_idx)}")


if __name__ == '__main__':
    train(parse_args())
