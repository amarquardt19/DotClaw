from pathlib import Path

path = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/train_fare_shape_autoencoder.py")
text = path.read_text()
original = text

old_dataclass = '''@dataclass
class PreparedAutoencoderData:
    sequences: np.ndarray
    masks: np.ndarray
    feature_names: list[str]
    sample_keys: list[str]
    observed_lengths: np.ndarray
    stats: dict[str, Any]
'''
new_dataclass = '''@dataclass
class PreparedAutoencoderData:
    sequences: np.ndarray
    masks: np.ndarray
    feature_names: list[str]
    sample_keys: list[str]
    observed_lengths: np.ndarray
    service_duration_minutes: np.ndarray
    stats: dict[str, Any]
'''
if 'service_duration_minutes: np.ndarray' not in text:
    if old_dataclass not in text:
        raise SystemExit('dataclass block not found')
    text = text.replace(old_dataclass, new_dataclass, 1)

old_args = '    parser.add_argument("--seed", type=int, default=42)\n'
new_args = '    parser.add_argument("--seed", type=int, default=42)\n    parser.add_argument("--split-strategy", choices=["random", "duration_stratified"], default="random")\n'
if 'split-strategy' not in text:
    if old_args not in text:
        raise SystemExit('seed arg line not found')
    text = text.replace(old_args, new_args, 1)

old_prepare_lists = '''    sequences: list[np.ndarray] = []
    masks: list[np.ndarray] = []
    sample_keys: list[str] = []
    observed_lengths: list[int] = []
'''
new_prepare_lists = '''    sequences: list[np.ndarray] = []
    masks: list[np.ndarray] = []
    sample_keys: list[str] = []
    observed_lengths: list[int] = []
    service_duration_minutes: list[float] = []
'''
if 'service_duration_minutes: list[float] = []' not in text:
    if old_prepare_lists not in text:
        raise SystemExit('prepare lists block not found')
    text = text.replace(old_prepare_lists, new_prepare_lists, 1)

old_append_block = '''        sequences.append(padded)
        masks.append(mask)
        sample_keys.append(str(item["service_label"]))
        observed_lengths.append(int(array.shape[0]))
'''
new_append_block = '''        sequences.append(padded)
        masks.append(mask)
        sample_keys.append(str(item["service_label"]))
        observed_lengths.append(int(array.shape[0]))
        service_duration = pd.to_numeric(group.get("duration_minutes"), errors="coerce")
        duration_value = float(service_duration.median()) if service_duration.notna().any() else float("nan")
        service_duration_minutes.append(duration_value)
'''
if 'service_duration = pd.to_numeric(group.get("duration_minutes"), errors="coerce")' not in text:
    if old_append_block not in text:
        raise SystemExit('append block not found')
    text = text.replace(old_append_block, new_append_block, 1)

old_return = '''    return PreparedAutoencoderData(
        normalized_sequences,
        mask_array.astype(np.float32),
        feature_names,
        sample_keys,
        np.array(observed_lengths, dtype=np.int64),
        stats,
    )
'''
new_return = '''    return PreparedAutoencoderData(
        normalized_sequences,
        mask_array.astype(np.float32),
        feature_names,
        sample_keys,
        np.array(observed_lengths, dtype=np.int64),
        np.array(service_duration_minutes, dtype=np.float32),
        stats,
    )
'''
if 'np.array(service_duration_minutes, dtype=np.float32)' not in text:
    if old_return not in text:
        raise SystemExit('PreparedAutoencoderData return block not found')
    text = text.replace(old_return, new_return, 1)

old_summarize = '''def summarize_split(name: str, indices: np.ndarray, prepared: PreparedAutoencoderData) -> dict[str, float]:
    lengths = prepared.observed_lengths[indices]
    return {
        "count": int(indices.shape[0]),
        "avg_observed_length": float(lengths.mean()) if lengths.size else 0.0,
        "min_observed_length": int(lengths.min()) if lengths.size else 0,
        "max_observed_length": int(lengths.max()) if lengths.size else 0,
    }
'''
new_summarize = '''def summarize_split(name: str, indices: np.ndarray, prepared: PreparedAutoencoderData) -> dict[str, float]:
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
'''
if 'avg_service_duration_minutes' not in text:
    if old_summarize not in text:
        raise SystemExit('summarize_split block not found')
    text = text.replace(old_summarize, new_summarize, 1)

anchor_make_splits = '''def make_splits(args: argparse.Namespace, prepared: PreparedAutoencoderData) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, dict[str, float]]]:
    total_fraction = args.train_fraction + args.val_fraction + args.test_fraction
    if not np.isclose(total_fraction, 1.0, atol=1e-6):
        raise ValueError("train-fraction, val-fraction, and test-fraction must sum to 1.0")
    indices = np.arange(prepared.sequences.shape[0])
    train_idx, temp_idx = train_test_split(
        indices,
        test_size=(1.0 - args.train_fraction),
        random_state=args.seed,
        shuffle=True,
    )
    temp_fraction = args.val_fraction + args.test_fraction
    if temp_idx.size == 0:
        raise ValueError("No samples allocated to validation/test. Adjust split fractions.")
    val_share_of_temp = args.val_fraction / temp_fraction if temp_fraction > 0 else 0.5
    val_idx, test_idx = train_test_split(
        temp_idx,
        train_size=val_share_of_temp,
        random_state=args.seed,
        shuffle=True,
    )
    split_summary = {
        "train": summarize_split("train", train_idx, prepared),
        "val": summarize_split("val", val_idx, prepared),
        "test": summarize_split("test", test_idx, prepared),
    }
    return train_idx, val_idx, test_idx, split_summary
'''
replacement_make_splits = '''def build_duration_stratify_labels(prepared: PreparedAutoencoderData) -> np.ndarray | None:
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
'''
if 'def build_duration_stratify_labels' not in text:
    if anchor_make_splits not in text:
        raise SystemExit('make_splits block not found')
    text = text.replace(anchor_make_splits, replacement_make_splits, 1)

if text == original:
    print('no changes needed')
else:
    path.write_text(text)
    print(f'updated {path}')
