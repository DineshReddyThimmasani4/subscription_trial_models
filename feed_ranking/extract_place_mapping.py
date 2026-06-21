"""
Process the PlaceWithLatLngV3 JSON file from Desktop and upload mapping to S3.

Usage:
  python process_place_file.py
"""

import json
import boto3

INPUT_FILE = "/Users/thimmasanidineshreddy/Desktop/nearme.PlaceWithLatLngV3.json"
BUCKET = "nearme-feed-store"
OUTPUT_KEY = "config/district_state_mapping.json"

def process_place_file():
    """
    Read the PlaceWithLatLngV3 JSON file and create district->state mapping.
    """

    print(f"Reading {INPUT_FILE}...")
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        places = json.load(f)

    print(f"Loaded {len(places)} places")

    # Build state lookup: state_pid -> state_name
    states = {}
    districts = {}

    for place in places:
        place_type = place.get("ty")
        place_id = place.get("id")
        place_name = place.get("name")

        if place_type == "STATE":
            states[place_id] = place_name
        elif place_type == "DISTRICT":
            parent_id = place.get("parentId")
            districts[place_id] = {
                "district_name": place_name,
                "state_pid": parent_id
            }

    print(f"Found {len(states)} states")
    print(f"Found {len(districts)} districts")

    # Create district->state mapping
    district_state_map = {}

    for district_pid, info in districts.items():
        state_pid = info["state_pid"]

        if state_pid and state_pid in states:
            district_state_map[district_pid] = {
                "district_name": info["district_name"],
                "state_name": states[state_pid],
                "state_pid": state_pid
            }
        else:
            print(f"WARN: District {info['district_name']} has no valid state (parentId={state_pid})")

    print(f"Created mapping for {len(district_state_map)} districts")

    # Upload to S3
    s3 = boto3.client('s3')

    mapping_json = json.dumps(district_state_map, indent=2, ensure_ascii=False)

    print(f"\nUploading to s3://{BUCKET}/{OUTPUT_KEY}...")
    s3.put_object(
        Bucket=BUCKET,
        Key=OUTPUT_KEY,
        Body=mapping_json.encode('utf-8'),
        ContentType='application/json'
    )

    print(f"✅ Done!")
    print(f"\nMapping saved to: s3://{BUCKET}/{OUTPUT_KEY}")
    print(f"Districts mapped: {len(district_state_map)}")

    # Show sample
    print(f"\nSample mappings:")
    for i, (district_pid, info) in enumerate(list(district_state_map.items())[:5]):
        print(f"  {district_pid}: {info['district_name']} → {info['state_name']}")
        if i >= 4:
            break

    return district_state_map


if __name__ == "__main__":
    process_place_file()
