from pathlib import Path

path = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/train_fare_shape_autoencoder.py')
text = path.read_text()
original = text

block = '''VALUE_DELTA_ACCEL_STATE_OBSERVED_NUMERIC_COLUMNS = [
    "price_value",
]

VALUE_DELTA_ACCEL_STATE_OBSERVED_BOOLEAN_COLUMNS: list[str] = []

VALUE_DELTA_ACCEL_STATE_OBSERVED_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_state",
    "observed_flag",
]

'''
text = text.replace(block, '')
text = text.replace('    "price_acceleration_state": 2.0,\n', '')
text = text.replace('"value_delta_accel_state_observed", ', '')
text = text.replace('    parser.add_argument("--accel-state-threshold", type=float, default=5.0)\n', '')

resolver_block = '''    if args.feature_profile == "value_delta_accel_state_observed":
        return (
            VALUE_DELTA_ACCEL_STATE_OBSERVED_NUMERIC_COLUMNS,
            VALUE_DELTA_ACCEL_STATE_OBSERVED_BOOLEAN_COLUMNS,
            VALUE_DELTA_ACCEL_STATE_OBSERVED_DERIVED_COLUMNS,
            False,
        )
'''
text = text.replace(resolver_block, '')

feature_block = '''    accel_threshold = float(getattr(add_sequence_features, "accel_state_threshold", 5.0))
    group["price_acceleration_state"] = np.where(
        group["price_acceleration_per_day"] > accel_threshold,
        1.0,
        np.where(group["price_acceleration_per_day"] < -accel_threshold, -1.0, 0.0),
    )
'''
text = text.replace(feature_block, '')
text = text.replace('    add_sequence_features.accel_state_threshold = args.accel_state_threshold\n', '')

if text == original:
    print('no changes needed')
else:
    path.write_text(text)
    print(f'reverted {path}')
