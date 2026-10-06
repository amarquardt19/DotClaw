from pathlib import Path

path = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/train_fare_shape_autoencoder.py')
text = path.read_text()
original = text

anchor_constants = '''RGB_STATE_MOTION_NO_FLAG_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_signed_log",
]

RGB_MOTION_ACCEL_NUMERIC_COLUMNS: list[str] = []
'''
insert_constants = '''RGB_STATE_MOTION_NO_FLAG_DERIVED_COLUMNS = [
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

RGB_MOTION_ACCEL_NUMERIC_COLUMNS: list[str] = []
'''
if 'VALUE_DELTA_OBSERVED_DERIVED_COLUMNS' not in text:
    if anchor_constants not in text:
        raise SystemExit('constants anchor not found')
    text = text.replace(anchor_constants, insert_constants, 1)

old_choices = '    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_state_motion_accel_no_flag", "rgb_motion_accel_mask", "state_motion_two_feature"], default="full")\n'
new_choices = '    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_state_motion_accel_no_flag", "value_delta_observed", "rgb_motion_accel_mask", "state_motion_two_feature"], default="full")\n'
if '"value_delta_observed"' not in text:
    if old_choices not in text:
        raise SystemExit('feature profile choices not found')
    text = text.replace(old_choices, new_choices, 1)

anchor_resolver = '''    if args.feature_profile == "rgb_state_motion_accel_no_flag":
        return (
            RGB_STATE_MOTION_NO_FLAG_NUMERIC_COLUMNS,
            RGB_STATE_MOTION_NO_FLAG_BOOLEAN_COLUMNS,
            RGB_STATE_MOTION_NO_FLAG_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "rgb_motion_accel_mask":
'''
insert_resolver = '''    if args.feature_profile == "rgb_state_motion_accel_no_flag":
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
    if args.feature_profile == "rgb_motion_accel_mask":
'''
if 'if args.feature_profile == "value_delta_observed":' not in text:
    if anchor_resolver not in text:
        raise SystemExit('resolver anchor not found')
    text = text.replace(anchor_resolver, insert_resolver, 1)

if text == original:
    print('no changes needed')
else:
    path.write_text(text)
    print(f'updated {path}')
