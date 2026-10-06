from pathlib import Path

path = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/train_fare_shape_autoencoder.py')
text = path.read_text()
original = text

parse_old = '''def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", default=str(DEFAULT_WORKBOOK))
    parser.add_argument("--sheet-name", default=0)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_state_motion_accel_no_flag", "value_delta_observed", "value_delta_cum_accel_observed", "rgb_motion_accel_mask", "state_motion_two_feature"], default="full")
    parser.add_argument("--model-profile", choices=["deep4", "compact3"], default="deep4")
    parser.add_argument("--horizon-days", type=int, default=66)
    parser.add_argument("--min-days-until-departure", type=int, default=1)
    parser.add_argument("--max-seq-len", type=int, default=66)
    parser.add_argument("--min-observations", type=int, default=8)
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
'''
parse_new = '''def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", default=str(DEFAULT_WORKBOOK))
    parser.add_argument("--sheet-name", default=0)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_state_motion_accel_no_flag", "value_delta_observed", "value_delta_cum_accel_observed", "rgb_motion_accel_mask", "state_motion_two_feature"], default="full")
    parser.add_argument("--model-profile", choices=["deep4", "compact3"], default="deep4")
    parser.add_argument("--horizon-days", type=int, default=66)
    parser.add_argument("--min-days-until-departure", type=int, default=1)
    parser.add_argument("--max-seq-len", type=int, default=66)
    parser.add_argument("--min-observations", type=int, default=8)
    parser.add_argument("--periodized-cum-accel-steps", type=int, default=48)
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
'''
if parse_old not in text:
    raise SystemExit('parse_args block not found')
text = text.replace(parse_old, parse_new, 1)

helper_anchor = '''def build_feature_frame(
    df: pd.DataFrame,
'''
helper_insert = '''def build_periodized_value_delta_cum_accel_frame(group: pd.DataFrame, target_steps: int) -> pd.DataFrame:
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
    accel = np.diff(delta, prepend=delta[0]).astype(np.float32)
    if accel.shape[0] > 0:
        accel[0] = 0.0
    accel_state = np.where(accel > 1e-6, 1.0, np.where(accel < -1e-6, -1.0, 0.0)).astype(np.float32)
    accel_state_cumulative = accel_state.cumsum().astype(np.float32)
    return pd.DataFrame(
        {
            "price_value": resampled_price.astype(np.float32),
            "price_delta_per_day": delta,
            "price_acceleration_state_cumulative": accel_state_cumulative,
            "observed_flag": np.ones(target_steps, dtype=np.float32),
        }
    )


def build_feature_frame(
    df: pd.DataFrame,
'''
if helper_anchor not in text:
    raise SystemExit('helper anchor not found')
text = text.replace(helper_anchor, helper_insert, 1)

loop_old = '''    for item in grouped_rows:
        group = item["group"]
        if len(group) < args.min_observations:
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
'''
loop_new = '''    for item in grouped_rows:
        group = item["group"]
        if len(group) < args.min_observations:
            continue
        if args.feature_profile == "value_delta_cum_accel_observed":
            feature_frame = build_periodized_value_delta_cum_accel_frame(group, args.periodized_cum_accel_steps)
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
'''
if loop_old not in text:
    raise SystemExit('prepare_data loop block not found')
text = text.replace(loop_old, loop_new, 1)

stats_old = '''        "max_seq_len": int(args.max_seq_len),
        "feature_profile": args.feature_profile,
'''
stats_new = '''        "max_seq_len": int(args.max_seq_len),
        "periodized_cum_accel_steps": int(args.periodized_cum_accel_steps),
        "feature_profile": args.feature_profile,
'''
if stats_old not in text:
    raise SystemExit('stats block not found')
text = text.replace(stats_old, stats_new, 1)

if text == original:
    print('no changes needed')
else:
    path.write_text(text)
    print('updated training for periodized cumulative acceleration')
