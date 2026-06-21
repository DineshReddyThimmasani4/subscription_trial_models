"""
Create labels for training data.

For each (user, post, snapshot_time) triple:
  - Query events AFTER snapshot_time
  - Check if day-0 user converted
  - Label: y = 0 (no convert) / 1 (initiate) / 2 (trial)

Run this AFTER you have 7+ days of Lambda snapshots.

Usage:
  python create_labels.py --start-date 2026-06-14 --end-date 2026-06-21
"""

import boto3
import time
from datetime import datetime, timedelta

athena = boto3.client('athena', region_name='ap-south-1')
s3 = boto3.client('s3', region_name='ap-south-1')

ATHENA_DATABASE = "closeapp"
ATHENA_OUTPUT = "s3://nearme-feed-store/athena-results/"
BUCKET = "nearme-feed-store"

def create_labels_table(start_date, end_date):
    """
    Create labels by joining Lambda snapshots with day-0 user outcomes.

    Point-in-time discipline:
      - Features: From snapshot at time T (BEFORE user saw post)
      - Labels: From events AFTER user saw post (time >= T + epsilon)
    """

    query = f"""
    -- ═══════════════════════════════════════════════════════════════
    -- STEP 1: Identify day-0 users in time window
    -- ═══════════════════════════════════════════════════════════════

    WITH day0_users AS (
        SELECT DISTINCT user_id
        FROM {ATHENA_DATABASE}.appevents
        WHERE event_name = 'feed_first_call'
          AND userAge = 0
          AND date BETWEEN '{start_date}' AND '{end_date}'
    ),

    -- ═══════════════════════════════════════════════════════════════
    -- STEP 2: Get day-0 user post views (feed_locked events)
    -- ═══════════════════════════════════════════════════════════════

    post_views AS (
        SELECT
            e.user_id,
            e.postId AS post_id,
            e.event_time AS view_time
        FROM {ATHENA_DATABASE}.appevents e
        INNER JOIN day0_users u ON e.user_id = u.user_id
        WHERE e.event_name = 'feed_locked'
          AND e.isLocked = true
          AND e.date BETWEEN '{start_date}' AND '{end_date}'
    ),

    -- ═══════════════════════════════════════════════════════════════
    -- STEP 3: Match views with nearest PRIOR snapshot
    -- User saw post at view_time, use snapshot from BEFORE that
    -- ═══════════════════════════════════════════════════════════════

    -- Load Lambda snapshots (you'll need to create external table first)
    -- For now, assuming snapshots are in S3 as JSONL files

    -- Simplified: Use Athena table over S3 snapshots
    post_views_with_snapshot AS (
        SELECT
            pv.user_id,
            pv.post_id,
            pv.view_time,
            -- Find snapshot time <= view_time (nearest prior snapshot)
            MAX(d.run_ts) AS snapshot_ts
        FROM post_views pv
        CROSS JOIN (
            -- Read all snapshots in date range
            -- NOTE: You need to create external table 'dynamic_snapshots' over S3
            SELECT DISTINCT
                post_id,
                CAST(run_ts AS TIMESTAMP) AS run_ts
            FROM dynamic_snapshots
            WHERE date BETWEEN '{start_date}' AND '{end_date}'
        ) d
        WHERE pv.post_id = d.post_id
          AND d.run_ts <= pv.view_time
        GROUP BY pv.user_id, pv.post_id, pv.view_time
    ),

    -- ═══════════════════════════════════════════════════════════════
    -- STEP 4: Query outcomes AFTER user viewed post
    -- ═══════════════════════════════════════════════════════════════

    -- Initiate transaction events (after view)
    initiates AS (
        SELECT DISTINCT
            pvs.user_id,
            pvs.post_id,
            pvs.snapshot_ts
        FROM post_views_with_snapshot pvs
        INNER JOIN {ATHENA_DATABASE}.appevents e
            ON e.user_id = pvs.user_id
            AND e.event_name = 'initiate_transaction'
            AND e.isInitiatedFromPost = true
            AND SUBSTR(e.origin, 6) = pvs.post_id  -- origin = 'post_<postId>'
            AND e.event_time >= pvs.view_time  -- AFTER user saw post
            AND e.date BETWEEN '{start_date}' AND DATE_ADD('day', 3, CAST('{end_date}' AS DATE))
    ),

    -- Trial events (after view)
    trials AS (
        SELECT DISTINCT
            pvs.user_id,
            pvs.post_id,
            pvs.snapshot_ts
        FROM post_views_with_snapshot pvs
        INNER JOIN {ATHENA_DATABASE}.appevents e
            ON e.user_id = pvs.user_id
            AND e.event_name = 'subscription_trial_started'
            AND e.event_time >= pvs.view_time  -- AFTER user saw post
            AND e.date BETWEEN '{start_date}' AND DATE_ADD('day', 3, CAST('{end_date}' AS DATE))
    )

    -- ═══════════════════════════════════════════════════════════════
    -- STEP 5: Assign labels
    -- ═══════════════════════════════════════════════════════════════

    SELECT
        pvs.user_id,
        pvs.post_id,
        pvs.snapshot_ts AS run_ts,
        pvs.view_time,
        CASE
            WHEN t.user_id IS NOT NULL THEN 2  -- Started trial
            WHEN i.user_id IS NOT NULL THEN 1  -- Initiated transaction
            ELSE 0                              -- No conversion
        END AS y
    FROM post_views_with_snapshot pvs
    LEFT JOIN trials t
        ON t.user_id = pvs.user_id
        AND t.post_id = pvs.post_id
        AND t.snapshot_ts = pvs.snapshot_ts
    LEFT JOIN initiates i
        ON i.user_id = pvs.user_id
        AND i.post_id = pvs.post_id
        AND i.snapshot_ts = pvs.snapshot_ts
    """

    print(f"Creating labels table for {start_date} to {end_date}...")

    response = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={'Database': ATHENA_DATABASE},
        ResultConfiguration={'OutputLocation': ATHENA_OUTPUT}
    )

    query_id = response['QueryExecutionId']
    print(f"Query started: {query_id}")

    # Wait for completion
    while True:
        status = athena.get_query_execution(QueryExecutionId=query_id)
        state = status['QueryExecution']['Status']['State']

        if state == 'SUCCEEDED':
            print("✅ Labels created successfully!")
            location = status['QueryExecution']['ResultConfiguration']['OutputLocation']
            print(f"Results: {location}")
            return query_id
        elif state in ['FAILED', 'CANCELLED']:
            reason = status['QueryExecution']['Status'].get('StateChangeReason', 'Unknown')
            raise RuntimeError(f"Query failed: {reason}")

        print(f"Status: {state}...")
        time.sleep(10)


def create_dynamic_snapshots_table():
    """
    Create external table over Lambda snapshots in S3.
    Run this ONCE before creating labels.
    """

    query = f"""
    CREATE EXTERNAL TABLE IF NOT EXISTS {ATHENA_DATABASE}.dynamic_snapshots (
        run_ts STRING,
        post_id STRING,
        created_ts BIGINT,
        views INT,
        shareCount INT,
        mediaClickedCount INT,
        reactionCount INT,
        commentCount INT,
        readModeCount INT,
        avgDur DOUBLE,
        listenpct DOUBLE,
        topic STRING,
        subTopic STRING,
        distImp INT,
        stateImp INT,
        natImp INT,
        isLocal BOOLEAN,
        isReporter BOOLEAN,
        media BOOLEAN,
        media_duration DOUBLE,
        titleLen INT,
        postLang STRING,
        creatorID STRING,
        location_pid STRING,
        post_state STRING,
        post_locks_so_far INT,
        post_initiates_so_far INT,
        post_trials_so_far INT,
        post_day0_locks INT,
        post_day0_initiates INT,
        post_day0_trials INT,
        post_day0_initiates_per_view DOUBLE,
        post_day0_trials_per_view DOUBLE,
        post_day0_initiates_per_lock DOUBLE
    )
    PARTITIONED BY (date STRING)
    ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
    LOCATION 's3://{BUCKET}/dynamic/'
    """

    print("Creating external table over Lambda snapshots...")

    response = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={'Database': ATHENA_DATABASE},
        ResultConfiguration={'OutputLocation': ATHENA_OUTPUT}
    )

    query_id = response['QueryExecutionId']

    # Wait for completion
    while True:
        status = athena.get_query_execution(QueryExecutionId=query_id)
        state = status['QueryExecution']['Status']['State']

        if state == 'SUCCEEDED':
            print("✅ External table created!")
            break
        elif state in ['FAILED', 'CANCELLED']:
            reason = status['QueryExecution']['Status'].get('StateChangeReason', 'Unknown')
            raise RuntimeError(f"Failed to create table: {reason}")

        time.sleep(5)

    # Add partitions
    print("Adding partitions...")

    # Get date range from S3
    response = s3.list_objects_v2(
        Bucket=BUCKET,
        Prefix='dynamic/date=',
        Delimiter='/'
    )

    dates = []
    for prefix in response.get('CommonPrefixes', []):
        date_str = prefix['Prefix'].split('date=')[1].rstrip('/')
        dates.append(date_str)

    print(f"Found {len(dates)} date partitions")

    for date in dates:
        partition_query = f"""
        ALTER TABLE {ATHENA_DATABASE}.dynamic_snapshots
        ADD IF NOT EXISTS PARTITION (date='{date}')
        LOCATION 's3://{BUCKET}/dynamic/date={date}/'
        """

        athena.start_query_execution(
            QueryString=partition_query,
            QueryExecutionContext={'Database': ATHENA_DATABASE},
            ResultConfiguration={'OutputLocation': ATHENA_OUTPUT}
        )

    print("✅ Partitions added!")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Create training labels')
    parser.add_argument('--start-date', required=True, help='Start date YYYY-MM-DD')
    parser.add_argument('--end-date', required=True, help='End date YYYY-MM-DD')
    parser.add_argument('--create-table', action='store_true', help='Create external table first')

    args = parser.parse_args()

    if args.create_table:
        print("Step 1: Creating external table over Lambda snapshots...")
        create_dynamic_snapshots_table()
        print()

    print("Step 2: Creating labels...")
    query_id = create_labels_table(args.start_date, args.end_date)

    print(f"""
    ✅ Labels created!

    Next steps:
    1. Query the results from Athena
    2. Join with Lambda snapshots + EMR features
    3. Build complete training dataset

    Query ID: {query_id}
    """)