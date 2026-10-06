from pathlib import Path

path = Path('/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/train_fare_shape_autoencoder.py')
text = path.read_text()
original = text

anchor_constants = '''RGB_STATE_MOTION_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_signed_log",
    "observed_flag",
]

RGB_MOTION_ACCEL_NUMERIC_COLUMNS: list[str] = []
'''
insert_constants = '''RGB_STATE_MOTION_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_signed_log",
    "observed_flag",
]

RGB_STATE_MOTION_NO_FLAG_NUMERIC_COLUMNS = [
    "price_value",
]

RGB_STATE_MOTION_NO_FLAG_BOOLEAN_COLUMNS: list[str] = []

RGB_STATE_MOTION_NO_FLAG_DERIVED_COLUMNS = [
    "price_delta_per_day",
    "price_acceleration_signed_log",
]

RGB_MOTION_ACCEL_NUMERIC_COLUMNS: list[str] = []
'''
if 'RGB_STATE_MOTION_NO_FLAG_DERIVED_COLUMNS' not in text:
    if anchor_constants not in text:
        raise SystemExit('constants anchor not found')
    text = text.replace(anchor_constants, insert_constants, 1)

old_feature_profile = '    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_motion_accel_mask", "state_motion_two_feature"], default="full")\n'
new_feature_profile = '    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_state_motion_accel_no_flag", "rgb_motion_accel_mask", "state_motion_two_feature"], default="full")\n'
if 'rgb_state_motion_accel_no_flag' not in text:
    if old_feature_profile not in text:
        raise SystemExit('feature profile line not found')
    text = text.replace(old_feature_profile, new_feature_profile, 1)

anchor_resolver = '''    if args.feature_profile == "rgb_state_motion_accel":
        return (
            RGB_STATE_MOTION_NUMERIC_COLUMNS,
            RGB_STATE_MOTION_BOOLEAN_COLUMNS,
            RGB_STATE_MOTION_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "rgb_motion_accel_mask":
'''
insert_resolver = '''    if args.feature_profile == "rgb_state_motion_accel":
        return (
            RGB_STATE_MOTION_NUMERIC_COLUMNS,
            RGB_STATE_MOTION_BOOLEAN_COLUMNS,
            RGB_STATE_MOTION_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "rgb_state_motion_accel_no_flag":
        return (
            RGB_STATE_MOTION_NO_FLAG_NUMERIC_COLUMNS,
            RGB_STATE_MOTION_NO_FLAG_BOOLEAN_COLUMNS,
            RGB_STATE_MOTION_NO_FLAG_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "rgb_motion_accel_mask":
'''
if 'if args.feature_profile == "rgb_state_motion_accel_no_flag":' not in text:
    if anchor_resolver not in text:
        raise SystemExit('resolver anchor not found')
    text = text.replace(anchor_resolver, insert_resolver, 1)

if text == original:
    print('no changes needed')
else:
    path.write_text(text)
    print(f'updated {path}')
