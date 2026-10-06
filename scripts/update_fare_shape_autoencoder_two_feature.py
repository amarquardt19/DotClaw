from pathlib import Path

path = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/scripts/train_fare_shape_autoencoder.py")
text = path.read_text()
original = text

anchor1 = '''DEFAULT_WORKBOOK = Path("/Users/adammarq/Downloads/Flights Claw Final Project v6.xlsx")
DEFAULT_OUTPUT_DIR = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/output/fare_shape_autoencoder")

RGB_STATE_MOTION_NUMERIC_COLUMNS = [
'''
insert1 = '''DEFAULT_WORKBOOK = Path("/Users/adammarq/Downloads/Flights Claw Final Project v6.xlsx")
DEFAULT_OUTPUT_DIR = Path("/Users/adammarq/CascadeProjects/openclaw-inbox-agent/output/fare_shape_autoencoder")

TWO_FEATURE_STATE_MOTION_NUMERIC_COLUMNS = [
    "price_value",
]

TWO_FEATURE_STATE_MOTION_BOOLEAN_COLUMNS: list[str] = []

TWO_FEATURE_STATE_MOTION_DERIVED_COLUMNS = [
    "price_delta_per_day",
]

RGB_STATE_MOTION_NUMERIC_COLUMNS = [
'''
if "TWO_FEATURE_STATE_MOTION_NUMERIC_COLUMNS" not in text:
    if anchor1 not in text:
        raise SystemExit("anchor1 not found")
    text = text.replace(anchor1, insert1, 1)

old_line = '    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_motion_accel_mask"], default="full")\n'
new_line = '    parser.add_argument("--feature-profile", choices=["full", "trimmed", "rgb_state_motion_accel", "rgb_motion_accel_mask", "state_motion_two_feature"], default="full")\n'
if 'state_motion_two_feature' not in text:
    if old_line not in text:
        raise SystemExit("feature-profile line not found")
    text = text.replace(old_line, new_line, 1)

anchor2 = '''def resolve_feature_profile(args: argparse.Namespace) -> tuple[list[str], list[str], list[str], bool]:
    if args.feature_profile == "rgb_state_motion_accel":
'''
insert2 = '''def resolve_feature_profile(args: argparse.Namespace) -> tuple[list[str], list[str], list[str], bool]:
    if args.feature_profile == "state_motion_two_feature":
        return (
            TWO_FEATURE_STATE_MOTION_NUMERIC_COLUMNS,
            TWO_FEATURE_STATE_MOTION_BOOLEAN_COLUMNS,
            TWO_FEATURE_STATE_MOTION_DERIVED_COLUMNS,
            False,
        )
    if args.feature_profile == "rgb_state_motion_accel":
'''
if 'if args.feature_profile == "state_motion_two_feature":' not in text:
    if anchor2 not in text:
        raise SystemExit("resolve_feature_profile anchor not found")
    text = text.replace(anchor2, insert2, 1)

if text == original:
    print("no changes needed")
else:
    path.write_text(text)
    print(f"updated {path}")
