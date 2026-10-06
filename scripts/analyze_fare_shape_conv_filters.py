import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import torch

import train_fare_shape_autoencoder as t

RUNS = {
    'days7_66': Path('output/fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_days7_66_periodized48'),
    'full_window': Path('output/fare_shape_autoencoder_value_delta_cum_accel_observed_compact3_duration_split_full_window_periodized48_clusters3'),
}

DOWNLOADS = Path('/Users/adammarq/Downloads')


def summarize_run(run_name: str, base: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    meta = json.loads((base / 'fare_shape_autoencoder_metadata.json').read_text())
    args = SimpleNamespace(
        workbook=meta['workbook'],
        sheet_name=meta['sheet_name'],
        horizon_days=66,
        min_days_until_departure=meta['min_days_until_departure'],
        max_seq_len=66,
        min_observations=8,
        periodized_cum_accel_steps=meta['periodized_cum_accel_steps'],
        feature_profile=meta['feature_profile'],
    )
    prepared = t.prepare_data(args)
    model = t.CompactTemporalConvAutoencoder(
        feature_dim=prepared.sequences.shape[2],
        latent_dim=meta['latent_dim'],
        seq_len=66,
    )
    state = torch.load(base / 'fare_shape_autoencoder.pt', map_location='cpu')
    model.load_state_dict(state)
    model.eval()

    x = torch.tensor(prepared.sequences, dtype=torch.float32).transpose(1, 2)
    conv1_raw = model.encoder[0](x)
    conv1_map = model.encoder[2](model.encoder[1](conv1_raw)).detach().numpy()
    conv2_raw = model.encoder[3](conv1_map if isinstance(conv1_map, torch.Tensor) else torch.tensor(conv1_map, dtype=torch.float32))
    conv2_map = model.encoder[5](model.encoder[4](conv2_raw)).detach().numpy()

    w1 = state['encoder.0.weight'].numpy()
    w2 = state['encoder.3.weight'].numpy()
    feature_names = meta['feature_names']

    absw1 = np.abs(w1).sum(axis=2)
    act1 = np.abs(conv1_map).mean(axis=(0, 2))
    conv1_dominant_feature = [feature_names[int(np.argmax(absw1[i]))] for i in range(absw1.shape[0])]

    absw2 = np.abs(w2).sum(axis=2)
    act2 = np.abs(conv2_map).mean(axis=(0, 2))

    conv1_weight_rows: list[dict] = []
    for filter_idx in range(w1.shape[0]):
        for feature_idx, feature_name in enumerate(feature_names):
            kernel = w1[filter_idx, feature_idx, :]
            conv1_weight_rows.append({
                'run_name': run_name,
                'layer': 'conv1',
                'filter_index': filter_idx,
                'input_feature': feature_name,
                'dominant_for_filter': feature_name == conv1_dominant_feature[filter_idx],
                'abs_weight_sum': float(absw1[filter_idx, feature_idx]),
                'kernel_t0': float(kernel[0]),
                'kernel_t1': float(kernel[1]),
                'kernel_t2': float(kernel[2]),
                'kernel_t3': float(kernel[3]),
                'kernel_t4': float(kernel[4]),
            })

    conv1_activation_rows: list[dict] = []
    for filter_idx in range(conv1_map.shape[1]):
        time_means = np.abs(conv1_map[:, filter_idx, :]).mean(axis=0)
        conv1_activation_rows.append({
            'run_name': run_name,
            'layer': 'conv1',
            'filter_index': filter_idx,
            'mean_abs_activation': float(act1[filter_idx]),
            'dominant_input_feature': conv1_dominant_feature[filter_idx],
            'time_early_mean': float(time_means[:16].mean()),
            'time_mid_mean': float(time_means[16:32].mean()),
            'time_late_mean': float(time_means[32:].mean()),
        })

    conv2_weight_rows: list[dict] = []
    for filter_idx in range(w2.shape[0]):
        ranking = np.argsort(-absw2[filter_idx])
        top_inputs = ranking[:5]
        row = {
            'run_name': run_name,
            'layer': 'conv2',
            'filter_index': filter_idx,
        }
        for pos, input_idx in enumerate(top_inputs, start=1):
            row[f'top{pos}_conv1_filter'] = int(input_idx)
            row[f'top{pos}_conv1_dominant_feature'] = conv1_dominant_feature[int(input_idx)]
            row[f'top{pos}_abs_weight_sum'] = float(absw2[filter_idx, input_idx])
        conv2_weight_rows.append(row)

    conv2_activation_rows: list[dict] = []
    for filter_idx in range(conv2_map.shape[1]):
        time_means = np.abs(conv2_map[:, filter_idx, :]).mean(axis=0)
        conv2_activation_rows.append({
            'run_name': run_name,
            'layer': 'conv2',
            'filter_index': filter_idx,
            'mean_abs_activation': float(act2[filter_idx]),
            'time_early_mean': float(time_means[:8].mean()),
            'time_mid_mean': float(time_means[8:16].mean()),
            'time_late_mean': float(time_means[16:].mean()),
        })

    print(f'RUN {run_name}')
    print('FEATURES ' + ', '.join(feature_names))
    print('CONV1_TOP_FILTERS')
    for idx in np.argsort(-act1)[:8]:
        ranking = np.argsort(-absw1[idx])
        parts = [f'{feature_names[j]}:{absw1[idx, j]:.2f}' for j in ranking[:4]]
        print(f'filter_{idx:02d} act={act1[idx]:.3f} | ' + ', '.join(parts))
    print('CONV2_TOP_FILTERS')
    for idx in np.argsort(-act2)[:8]:
        ranking = np.argsort(-absw2[idx])
        parts = [f'f{j:02d}({conv1_dominant_feature[j]}):{absw2[idx, j]:.2f}' for j in ranking[:5]]
        print(f'filter_{idx:02d} act={act2[idx]:.3f} | ' + ', '.join(parts))
    print()

    return (
        pd.DataFrame(conv1_weight_rows),
        pd.DataFrame(conv1_activation_rows),
        pd.DataFrame(conv2_weight_rows),
        pd.DataFrame(conv2_activation_rows),
    )


conv1_weights_all = []
conv1_activations_all = []
conv2_weights_all = []
conv2_activations_all = []
for run_name, base in RUNS.items():
    conv1_weights, conv1_activations, conv2_weights, conv2_activations = summarize_run(run_name, base)
    conv1_weights_all.append(conv1_weights)
    conv1_activations_all.append(conv1_activations)
    conv2_weights_all.append(conv2_weights)
    conv2_activations_all.append(conv2_activations)

conv1_weights_df = pd.concat(conv1_weights_all, ignore_index=True)
conv1_activations_df = pd.concat(conv1_activations_all, ignore_index=True)
conv2_weights_df = pd.concat(conv2_weights_all, ignore_index=True)
conv2_activations_df = pd.concat(conv2_activations_all, ignore_index=True)

out_dir = Path('output')
csv_prefix = out_dir / 'fare_shape_conv_filter_maps'
conv1_weights_df.to_csv(f'{csv_prefix}_conv1_weights.csv', index=False)
conv1_activations_df.to_csv(f'{csv_prefix}_conv1_activations.csv', index=False)
conv2_weights_df.to_csv(f'{csv_prefix}_conv2_weights.csv', index=False)
conv2_activations_df.to_csv(f'{csv_prefix}_conv2_activations.csv', index=False)

workbook_path = DOWNLOADS / 'fare_shape_conv_filter_maps.xlsx'
with pd.ExcelWriter(workbook_path) as writer:
    conv1_weights_df.to_excel(writer, sheet_name='conv1_weights', index=False)
    conv1_activations_df.to_excel(writer, sheet_name='conv1_activations', index=False)
    conv2_weights_df.to_excel(writer, sheet_name='conv2_weights', index=False)
    conv2_activations_df.to_excel(writer, sheet_name='conv2_activations', index=False)

print(f'saved_workbook={workbook_path}')
