"""
Create labels for training data - SAME DAY conversions.

For each (user, post, snapshot_time) triple:
  - Day-0 users who viewed posts
  - Check if they converted SAME DAY (within hours)
  - Label: y = 0 (no convert) / 1 (initiate) / 2 (trial)

SAME DAY FOCUS:
  Most trials start on Day 0, so we focus on same-day conversions.
  Run daily at 2 AM for PREVIOUS day's data.

Timeline:
  Day 0 (June 21): Users view posts, may convert same day
  Day 1 (June 22 2AM): Run this script for June 21
  Day 1 (June 22 morning): Labels ready, can train!

Usage:
  # One-time setup
  python create_labels.py --create-tables --start-date 2026-06-21 --end-date 2026-06-21

  # Daily run (for previous day)
  python create_labels.py --start-date 2026-06-21 --end-date 2026-06-21

  # Backfill multiple days
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

    Stores results persistently in S3 as partitioned Parquet table.

    SAME-DAY CONVERSION FOCUS:
      - Day-0 users who view posts
      - Check if they convert on SAME DAY (within hours)
      - Label: 0 (no conversion), 1 (initiated), 2 (trial)

    Point-in-time discipline:
      - Features: From snapshot at time T (BEFORE user saw post)
      - Labels: From events AFTER user saw post, SAME DAY
    """

    query = f"""
    -- ═══════════════════════════════════════════════════════════════
    -- Create persistent labels table in S3
    -- Day-0 conversions on SAME DAY only
    -- ═══════════════════════════════════════════════════════════════

    INSERT INTO {ATHENA_DATABASE}.training_labels

    -- ═══════════════════════════════════════════════════════════════
    -- STEP 1: Get day-0 user post views (feed_locked events)
    -- ═══════════════════════════════════════════════════════════════

    WITH post_views AS (
        SELECT
            e.user_id,
            json_extract_scalar(e.data, '$.postId') AS post_id,
            e.event_time AS view_time,
            CAST(json_extract_scalar(e.data, '$.userAge') AS INT) AS user_age
        FROM {ATHENA_DATABASE}.appevents e
        WHERE e.event_name = 'feed_locked'
          AND json_extract_scalar(e.data, '$.isLocked') = 'true'
          AND CAST(json_extract_scalar(e.data, '$.userAge') AS INT) = 0  -- Day-0 users only
          AND e.date BETWEEN '{start_date}' AND '{end_date}'
          AND json_extract_scalar(e.data, '$.postId') IS NOT NULL
    ),

    -- ═══════════════════════════════════════════════════════════════
    -- STEP 2: Match views with nearest PRIOR snapshot
    -- User saw post at view_time, use snapshot from BEFORE that
    -- ═══════════════════════════════════════════════════════════════

    post_views_with_snapshot AS (
        SELECT
            pv.user_id,
            pv.post_id,
            pv.view_time,
            pv.user_age,
            -- Find snapshot time <= view_time (nearest prior snapshot)
            MAX(CAST(d.run_ts AS BIGINT)) AS snapshot_ts
        FROM post_views pv
        INNER JOIN {ATHENA_DATABASE}.dynamic_snapshots d
            ON pv.post_id = d.post_id
            AND CAST(d.run_ts AS BIGINT) <= pv.view_time
            AND d.date BETWEEN '{start_date}' AND '{end_date}'
        GROUP BY pv.user_id, pv.post_id, pv.view_time, pv.user_age
    ),

    -- ═══════════════════════════════════════════════════════════════
    -- STEP 3: Query SAME-DAY outcomes AFTER user viewed post
    -- ═══════════════════════════════════════════════════════════════

    -- Initiate transaction events (SAME DAY after view)
    initiates AS (
        SELECT DISTINCT
            pvs.user_id,
            pvs.post_id,
            pvs.snapshot_ts
        FROM post_views_with_snapshot pvs
        INNER JOIN {ATHENA_DATABASE}.appevents e
            ON e.user_id = pvs.user_id
            AND e.event_name = 'initiate_transaction'
            AND json_extract_scalar(e.data, '$.isInitiatedFromPost') = 'true'
            AND (
                json_extract_scalar(e.data, '$.origin') = CONCAT('post_', pvs.post_id)
                OR SUBSTR(json_extract_scalar(e.data, '$.origin'), 6) = pvs.post_id
            )
            AND e.event_time >= pvs.view_time  -- AFTER user saw post
            AND date_format(from_unixtime(e.event_time / 1000), '%Y-%m-%d') =
                date_format(from_unixtime(pvs.view_time / 1000), '%Y-%m-%d')  -- ✅ SAME DAY!
            AND e.date BETWEEN '{start_date}' AND date_format(date_add('day', 1, date_parse('{end_date}', '%Y-%m-%d')), '%Y-%m-%d')
    ),

    -- Trial events (SAME DAY after view)
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
            AND date_format(from_unixtime(e.event_time / 1000), '%Y-%m-%d') =
                date_format(from_unixtime(pvs.view_time / 1000), '%Y-%m-%d')  -- ✅ SAME DAY!
            AND e.date BETWEEN '{start_date}' AND date_format(date_add('day', 1, date_parse('{end_date}', '%Y-%m-%d')), '%Y-%m-%d')
    )

    -- ═══════════════════════════════════════════════════════════════
    -- STEP 4: Assign labels based on SAME-DAY conversions
    -- ═══════════════════════════════════════════════════════════════

    SELECT
        pvs.user_id,
        pvs.post_id,
        pvs.snapshot_ts AS run_ts,
        pvs.view_time,
        CASE
            WHEN t.user_id IS NOT NULL THEN 2  -- Started trial (same day)
            WHEN i.user_id IS NOT NULL THEN 1  -- Initiated transaction (same day)
            ELSE 0                              -- No conversion (same day)
        END AS y,
        date_format(from_unixtime(pvs.view_time / 1000), '%Y-%m-%d') AS date
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

    print(f"Creating labels for {start_date} to {end_date}...")
    print(f"Conversion window: SAME DAY (Day-0 conversions only)")
    print(f"Storing to: s3://{BUCKET}/features/labels/")

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


def create_training_labels_table():
    """
    Create persistent training_labels table in S3.
    Run this ONCE before generating labels.
    """

    query = f"""
    CREATE EXTERNAL TABLE IF NOT EXISTS {ATHENA_DATABASE}.training_labels (
        user_id STRING,
        post_id STRING,
        run_ts BIGINT,
        view_time BIGINT,
        y INT
    )
    PARTITIONED BY (date STRING)
    STORED AS PARQUET
    LOCATION 's3://{BUCKET}/features/labels/'
    TBLPROPERTIES (
        'parquet.compression'='SNAPPY'
    )
    """

    print("Creating training_labels table...")

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
            print("✅ training_labels table created!")
            print(f"Location: s3://{BUCKET}/features/labels/")
            break
        elif state in ['FAILED', 'CANCELLED']:
            reason = status['QueryExecution']['Status'].get('StateChangeReason', 'Unknown')
            raise RuntimeError(f"Failed to create table: {reason}")

        time.sleep(2)


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
    parser.add_argument('--create-tables', action='store_true', help='Create external tables first')

    args = parser.parse_args()

    if args.create_tables:
        print("="*80)
        print("SETUP: Creating external tables")
        print("="*80)

        print("\nStep 1: Creating training_labels table...")
        create_training_labels_table()

        print("\nStep 2: Creating external table over Lambda snapshots...")
        create_dynamic_snapshots_table()
        print()

    print("="*80)
    print("GENERATING LABELS")
    print("="*80)
    print(f"Date range: {args.start_date} to {args.end_date}")
    print(f"Conversion window: SAME DAY (Day-0 conversions only)")
    print(f"Focus: Day-0 users who convert within hours of viewing")
    print()

    query_id = create_labels_table(args.start_date, args.end_date)

    print(f"""
    ✅ Labels created and stored persistently!

    Storage location: s3://{BUCKET}/features/labels/
    Table: {ATHENA_DATABASE}.training_labels

    Label definition:
      y=0: User viewed post, NO conversion (same day)
      y=1: User initiated transaction (same day)
      y=2: User initiated + started trial (same day)

    Check results:
      SELECT date, y, COUNT(*) as cnt,
             ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY date), 2) as pct
      FROM {ATHENA_DATABASE}.training_labels
      GROUP BY date, y
      ORDER BY date, y;

    Daily workflow:
      1. Run this script daily at 2 AM for PREVIOUS day
         Example on June 22: --start-date 2026-06-21 --end-date 2026-06-21
      2. Labels available same morning for training
      3. Can train model NEXT DAY (fast iteration!)

    Next steps:
    1. Run build_training_data.py to join with features
    2. Train model on complete dataset

    Query ID: {query_id}
    """)