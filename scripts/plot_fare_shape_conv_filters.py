from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

BASE = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent')
OUTPUT = BASE / 'output'
DOWNLOADS = Path('/Users/adammarq/Downloads')
VIS_DIR = OUTPUT / 'fare_shape_conv_filter_visuals'
VIS_DIR.mkdir(parents=True, exist_ok=True)

conv1_weights = pd.read_csv(OUTPUT / 'fare_shape_conv_filter_maps_conv1_weights.csv')
conv1_activations = pd.read_csv(OUTPUT / 'fare_shape_conv_filter_maps_conv1_activations.csv')
conv2_weights = pd.read_csv(OUTPUT / 'fare_shape_conv_filter_maps_conv2_weights.csv')
conv2_activations = pd.read_csv(OUTPUT / 'fare_shape_conv_filter_maps_conv2_activations.csv')

for run_name in sorted(conv1_weights['run_name'].unique()):
    run_conv1_weights = conv1_weights[conv1_weights['run_name'] == run_name].copy()
    run_conv1_acts = conv1_activations[conv1_activations['run_name'] == run_name].copy()
    run_conv2_weights = conv2_weights[conv2_weights['run_name'] == run_name].copy()
    run_conv2_acts = conv2_activations[conv2_activations['run_name'] == run_name].copy()

    pivot1 = run_conv1_weights.pivot(index='filter_index', columns='input_feature', values='abs_weight_sum').sort_index()
    fig, ax = plt.subplots(figsize=(8, 10))
    im = ax.imshow(pivot1.to_numpy(), aspect='auto', cmap='viridis')
    ax.set_title(f'{run_name} Conv1 filter weights by input feature')
    ax.set_xlabel('Input feature')
    ax.set_ylabel('Conv1 filter index')
    ax.set_xticks(range(len(pivot1.columns)))
    ax.set_xticklabels(pivot1.columns, rotation=45, ha='right')
    ax.set_yticks(range(len(pivot1.index)))
    ax.set_yticklabels(pivot1.index)
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(VIS_DIR / f'{run_name}_conv1_feature_heatmap.png', dpi=200)
    plt.close(fig)

    kernel_cols = ['kernel_t0', 'kernel_t1', 'kernel_t2', 'kernel_t3', 'kernel_t4']
    top_filters = run_conv1_acts.sort_values('mean_abs_activation', ascending=False).head(8)['filter_index'].tolist()
    top_kernel = run_conv1_weights[run_conv1_weights['filter_index'].isin(top_filters)].copy()
    top_kernel['feature_filter'] = top_kernel['input_feature'] + '_f' + top_kernel['filter_index'].astype(str).str.zfill(2)
    kernel_matrix = top_kernel.set_index('feature_filter')[kernel_cols]
    fig, ax = plt.subplots(figsize=(10, max(6, len(kernel_matrix) * 0.22)))
    vmax = float(kernel_matrix.abs().to_numpy().max()) if len(kernel_matrix) else 1.0
    im = ax.imshow(kernel_matrix.to_numpy(), aspect='auto', cmap='coolwarm', vmin=-vmax, vmax=vmax)
    ax.set_title(f'{run_name} Conv1 top-filter kernels')
    ax.set_xlabel('Kernel position')
    ax.set_ylabel('Feature / filter')
    ax.set_xticks(range(len(kernel_cols)))
    ax.set_xticklabels(kernel_cols)
    ax.set_yticks(range(len(kernel_matrix.index)))
    ax.set_yticklabels(kernel_matrix.index)
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(VIS_DIR / f'{run_name}_conv1_top_kernel_heatmap.png', dpi=200)
    plt.close(fig)

    act1_plot = run_conv1_acts.sort_values('mean_abs_activation', ascending=False).head(20).copy()
    fig, ax = plt.subplots(figsize=(10, 7))
    color_map = {
        'price_value': '#4c78a8',
        'price_delta_per_day': '#f58518',
        'price_acceleration_state_cumulative': '#54a24b',
        'observed_flag': '#b279a2',
    }
    colors = [color_map.get(v, '#777777') for v in act1_plot['dominant_input_feature']]
    ax.barh(act1_plot['filter_index'].astype(str), act1_plot['mean_abs_activation'], color=colors)
    ax.invert_yaxis()
    ax.set_title(f'{run_name} Conv1 top activations')
    ax.set_xlabel('Mean absolute activation')
    ax.set_ylabel('Conv1 filter index')
    fig.tight_layout()
    fig.savefig(VIS_DIR / f'{run_name}_conv1_activation_bars.png', dpi=200)
    plt.close(fig)

    time1 = run_conv1_acts.sort_values('mean_abs_activation', ascending=False).head(20).copy()
    time1 = time1.set_index('filter_index')[['time_early_mean', 'time_mid_mean', 'time_late_mean']]
    fig, ax = plt.subplots(figsize=(8, 7))
    im = ax.imshow(time1.to_numpy(), aspect='auto', cmap='viridis')
    ax.set_title(f'{run_name} Conv1 early/mid/late activation')
    ax.set_xlabel('Booking window slice')
    ax.set_ylabel('Conv1 filter index')
    ax.set_xticks(range(3))
    ax.set_xticklabels(['time_early_mean', 'time_mid_mean', 'time_late_mean'], rotation=30, ha='right')
    ax.set_yticks(range(len(time1.index)))
    ax.set_yticklabels(time1.index)
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(VIS_DIR / f'{run_name}_conv1_time_activation_heatmap.png', dpi=200)
    plt.close(fig)

    top2 = run_conv2_acts.sort_values('mean_abs_activation', ascending=False).head(20)['filter_index'].tolist()
    rows = []
    for _, row in run_conv2_weights[run_conv2_weights['filter_index'].isin(top2)].iterrows():
        filter_idx = row['filter_index']
        for rank in range(1, 6):
            rows.append({
                'filter_index': filter_idx,
                'source': f"f{int(row[f'top{rank}_conv1_filter']):02d}:{row[f'top{rank}_conv1_dominant_feature']}",
                'abs_weight_sum': row[f'top{rank}_abs_weight_sum'],
            })
    top2_df = pd.DataFrame(rows)
    pivot2 = top2_df.pivot(index='filter_index', columns='source', values='abs_weight_sum').fillna(0).sort_index()
    fig, ax = plt.subplots(figsize=(14, 8))
    im = ax.imshow(pivot2.to_numpy(), aspect='auto', cmap='plasma')
    ax.set_title(f'{run_name} Conv2 top source filters')
    ax.set_xlabel('Top contributing Conv1 filters')
    ax.set_ylabel('Conv2 filter index')
    ax.set_xticks(range(len(pivot2.columns)))
    ax.set_xticklabels(pivot2.columns, rotation=90)
    ax.set_yticks(range(len(pivot2.index)))
    ax.set_yticklabels(pivot2.index)
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(VIS_DIR / f'{run_name}_conv2_source_heatmap.png', dpi=200)
    plt.close(fig)

    time2 = run_conv2_acts.sort_values('mean_abs_activation', ascending=False).head(20).copy()
    time2 = time2.set_index('filter_index')[['time_early_mean', 'time_mid_mean', 'time_late_mean']]
    fig, ax = plt.subplots(figsize=(8, 7))
    im = ax.imshow(time2.to_numpy(), aspect='auto', cmap='magma')
    ax.set_title(f'{run_name} Conv2 early/mid/late activation')
    ax.set_xlabel('Booking window slice')
    ax.set_ylabel('Conv2 filter index')
    ax.set_xticks(range(3))
    ax.set_xticklabels(['time_early_mean', 'time_mid_mean', 'time_late_mean'], rotation=30, ha='right')
    ax.set_yticks(range(len(time2.index)))
    ax.set_yticklabels(time2.index)
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(VIS_DIR / f'{run_name}_conv2_time_activation_heatmap.png', dpi=200)
    plt.close(fig)

manifest = sorted(str(path) for path in VIS_DIR.glob('*.png'))
manifest_path = VIS_DIR / 'manifest.txt'
manifest_path.write_text('\n'.join(manifest))
copy_manifest_path = DOWNLOADS / 'fare_shape_conv_filter_visuals_manifest.txt'
copy_manifest_path.write_text('\n'.join(manifest))
print(f'saved_visual_dir={VIS_DIR}')
print(f'saved_manifest={manifest_path}')
print(f'saved_download_manifest={copy_manifest_path}')
