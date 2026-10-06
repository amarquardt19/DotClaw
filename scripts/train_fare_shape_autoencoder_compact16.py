#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from sklearn.cluster import KMeans
from torch import nn
from torch.utils.data import DataLoader

import train_fare_shape_autoencoder as base


class CompactTemporalConvAutoencoder16(nn.Module):
    def __init__(self, feature_dim: int, latent_dim: int, seq_len: int):
        super().__init__()
        self.feature_dim = feature_dim
        self.seq_len = seq_len
        self.encoder = nn.Sequential(
            nn.Conv1d(feature_dim, 16, kernel_size=5, padding=2),
            nn.ReLU(),
            nn.BatchNorm1d(16),
            nn.Conv1d(16, 32, kernel_size=5, stride=2, padding=2),
            nn.ReLU(),
            nn.BatchNorm1d(32),
            nn.Conv1d(32, 48, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(48),
        )
        with torch.no_grad():
            dummy = torch.zeros(1, feature_dim, seq_len)
            encoded = self.encoder(dummy)
            self.encoded_shape = encoded.shape[1:]
            self.flattened_dim = int(encoded.numel())
        self.to_latent = nn.Sequential(
            nn.Flatten(),
            nn.Linear(self.flattened_dim, 144),
            nn.ReLU(),
            nn.Linear(144, latent_dim),
        )
        self.from_latent = nn.Sequential(
            nn.Linear(latent_dim, 144),
            nn.ReLU(),
            nn.Linear(144, self.flattened_dim),
            nn.ReLU(),
        )
        self.decoder = nn.Sequential(
            nn.ConvTranspose1d(48, 32, kernel_size=4, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(32),
            nn.ConvTranspose1d(32, 16, kernel_size=4, stride=2, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(16),
            nn.Conv1d(16, feature_dim, kernel_size=3, padding=1),
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
    parser = base.parse_args()
    parser.model_profile = 'compact16'
    return parser


def train(args: argparse.Namespace) -> None:
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    prepared = base.prepare_data(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    train_idx, val_idx, test_idx, split_summary = base.make_splits(args, prepared)
    print('split_summary=' + json.dumps(split_summary, indent=None))

    train_loader = DataLoader(base.SequenceAutoencoderDataset(prepared.sequences[train_idx], prepared.masks[train_idx]), batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(base.SequenceAutoencoderDataset(prepared.sequences[val_idx], prepared.masks[val_idx]), batch_size=args.batch_size, shuffle=False)
    test_loader = DataLoader(base.SequenceAutoencoderDataset(prepared.sequences[test_idx], prepared.masks[test_idx]), batch_size=args.batch_size, shuffle=False)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = CompactTemporalConvAutoencoder16(feature_dim=prepared.sequences.shape[2], latent_dim=args.latent_dim, seq_len=args.max_seq_len).to(device)
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
            loss = base.masked_weighted_mse(reconstruction, sequences, masks, feature_weights)
            loss.backward()
            optimizer.step()
            train_loss_sum += float(loss.item()) * sequences.size(0)
            train_items += int(sequences.size(0))
        train_loss = train_loss_sum / max(train_items, 1)
        val_loss = base.evaluate(model, val_loader, device, feature_weights)
        history.append({'epoch': epoch, 'train_reconstruction_loss': train_loss, 'val_reconstruction_loss': val_loss})
        print(f'epoch={epoch:02d} train_recon_loss={train_loss:.5f} val_recon_loss={val_loss:.5f}')
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    if best_state is None:
        raise RuntimeError('Training did not produce a model state.')

    model.load_state_dict(best_state)
    test_loss = base.evaluate(model, test_loader, device, feature_weights)
    embeddings, reconstruction_loss_per_sample, _ = base.extract_embeddings(model, prepared.sequences, prepared.masks, device, args.batch_size)

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
        'model_profile': 'compact16',
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
