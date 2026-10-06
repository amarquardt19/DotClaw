from pathlib import Path

path = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/train_fare_shape_autoencoder.py')
text = path.read_text()
original = text

anchor_constants = '''VALUE_DELTA_OBSERVED_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "observed_flag",
]

RGB_MOTION_ACCEL_NUMERIC_COLUMNS: list[str] = []
'''
insert_constants = '''VALUE_DELTA_OBSERVED_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "observed_flag",
]

VALUE_DELTA_ACCEL_STATE_OBSERVED_NUMERIC_COLUMNS = [
    "price_value",
]

VALUE_DELTA_ACCEL_STATE_OBSERVED_BOOLEAN_COLUMNS: list[str] = []

VALUE_DELTA_ACCEL_STATE_OBSERVED_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_state",
    "observed_flag",
]

RGB_MOTION_ACCEL_NUMERIC_COLUMNS: list[str] = []
'''
if 'VALUE_DELTA_ACCEL_STATE_OBSERVED_DERIVED_COLUMNS' not in text:
    if anchor_constants not in text:
        raise SystemExit('constants anchor not found')
    text = text.replace(anchor_constants, insert_constants, 1)

old_weights = '''WEIGHTED_FEATURES = {
    "price_value": 3.0,
    "price_delta_per_day": 2.5,
    "price_acceleration_per_day": 3.0,
    "price_difference_prev_snapshot": 1.5,
'''
new_weights = '''WEIGHTED_FEATURES = {
    "price_value": 3.0,
    "price_delta_per_day": 2.5,
    "price_acceleration_per_day": 3.0,
    "price_acceleration_state": 2.0,
    "price_difference_prev_snapshot": 1.5,
'''
if '"price_acceleration_state": 2.0' not in text:
    if old_weights not in text:
        raise SystemExit('weights anchor not found')
    text = text.replace(old_weights, new_weights, 1)

old_parser = '    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_state_motion_accel_no_flag", "value_delta_observed", "rgb_motion_accel_mask", "state_motion_two_feature"], default="full")\n'
new_parser = '    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_state_motion_accel_no_flag", "value_delta_observed", "value_delta_accel_state_observed", "rgb_motion_accel_mask", "state_motion_two_feature"], default="full")\n'
if '"value_delta_accel_state_observed"' not in text:
    if old_parser not in text:
        raise SystemExit('feature-profile parser line not found')
    text = text.replace(old_parser, new_parser, 1)

threshold_anchor = '    parser.add_argument("--min-days-until-departure", type=int, default=1)\n'
threshold_insert = '    parser.add_argument("--min-days-until-departure", type=int, default=1)\n    parser.add_argument("--accel-state-threshold", type=float, default=5.0)\n'
if 'parser.add_argument("--accel-state-threshold"' not in text:
    if threshold_anchor not in text:
        raise SystemExit('threshold anchor not found')
    text = text.replace(threshold_anchor, threshold_insert, 1)

resolver_anchor = '''    if args.feature_profile == "value_delta_observed":
        return (
            VALUE_DELTA_OBSERVED_NUMERIC_COLUMNS,
            VALUE_DELTA_OBSERVED_BOOLEAN_COLUMNS,
            VALUE_DELTA_OBSERVED_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "rgb_motion_accel_mask":
'''
resolver_insert = '''    if args.feature_profile == "value_delta_observed":
        return (
            VALUE_DELTA_OBSERVED_NUMERIC_COLUMNS,
            VALUE_DELTA_OBSERVED_BOOLEAN_COLUMNS,
            VALUE_DELTA_OBSERVED_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "value_delta_accel_state_observed":
        return (
            VALUE_DELTA_ACCEL_STATE_OBSERVED_NUMERIC_COLUMNS,
            VALUE_DELTA_ACCEL_STATE_OBSERVED_BOOLEAN_COLUMNS,
            VALUE_DELTA_ACCEL_STATE_OBSERVED_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "rgb_motion_accel_mask":
'''
if 'if args.feature_profile == "value_delta_accel_state_observed":' not in text:
    if resolver_anchor not in text:
        raise SystemExit('resolver anchor not found')
    text = text.replace(resolver_anchor, resolver_insert, 1)

feature_anchor = '''    group["price_acceleration_per_day"] = group["price_acceleration_per_day"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    group["price_acceleration_signed_log"] = signed_log1p(group["price_acceleration_per_day"]).fillna(0.0)
    avg_gap = float(group["days_gap_from_previous_observation"].replace(0, np.nan).mean()) if len(group) > 1 else 0.0
'''
feature_insert = '''    group["price_acceleration_per_day"] = group["price_acceleration_per_day"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    group["price_acceleration_signed_log"] = signed_log1p(group["price_acceleration_per_day"]).fillna(0.0)
    accel_threshold = float(getattr(add_sequence_features, "accel_state_threshold", 5.0))
    group["price_acceleration_state"] = np.where(
        group["price_acceleration_per_day"] > accel_threshold,
        1.0,
        np.where(group["price_acceleration_per_day"] < -accel_threshold, -1.0, 0.0),
    )
    avg_gap = float(group["days_gap_from_previous_observation"].replace(0, np.nan).mean()) if len(group) > 1 else 0.0
'''
if 'group["price_acceleration_state"]' not in text:
    if feature_anchor not in text:
        raise SystemExit('feature engineering anchor not found')
    text = text.replace(feature_anchor, feature_insert, 1)

prepare_anchor = '    numeric_source, bool_source, derived_cols, include_static = resolve_feature_profile(args)\n'
prepare_insert = '    numeric_source, bool_source, derived_cols, include_static = resolve_feature_profile(args)\n    add_sequence_features.accel_state_threshold = args.accel_state_threshold\n'
if 'add_sequence_features.accel_state_threshold = args.accel_state_threshold' not in text:
    if prepare_anchor not in text:
        raise SystemExit('prepare anchor not found')
    text = text.replace(prepare_anchor, prepare_insert, 1)

if text == original:
    print('no changes needed')
else:
    path.write_text(text)
    print(f'updated {path}')
