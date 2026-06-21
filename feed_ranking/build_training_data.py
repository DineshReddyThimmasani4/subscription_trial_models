"""
Build complete training dataset by joining all sources.

Joins:
  - Labels (from create_labels.py)
  - Lambda snapshots (features at time T)
  - EMR ecosystem features
  - EMR creator features

Output: Training-ready parquet with all 57 features + label

Usage:
  python build_training_data.py --start-date 2026-06-14 --end-date 2026-06-21
"""

import boto3
import time

athena = boto3.client('athena', region_name='ap-south-1')

ATHENA_DATABASE = "closeapp"
ATHENA_OUTPUT = "s3://nearme-feed-store/athena-results/"
BUCKET = "nearme-feed-store"


def build_training_dataset(start_date, end_date):
    """
    Join all feature sources with labels to create final training dataset.

    Result: One row per (user, post, time) with all 57 features + y
    """

    query = f"""
    -- ═══════════════════════════════════════════════════════════════
    -- Join all sources to create complete training dataset
    -- ═══════════════════════════════════════════════════════════════

    WITH labels AS (
        -- From create_labels.py output
        SELECT
            user_id,
            post_id,
            run_ts,
            y
        FROM training_labels
        WHERE run_ts BETWEEN TIMESTAMP '{start_date}' AND TIMESTAMP '{end_date}'
    ),

    dynamic_features AS (
        -- Lambda snapshots (38 features)
        SELECT
            post_id,
            run_ts,
            -- Metadata
            created_ts,
            -- Engagement (raw counts)
            views,
            shareCount,
            mediaClickedCount,
            reactionCount,
            commentCount,
            readModeCount,
            avgDur,
            listenpct,
            -- Static post meta
            topic,
            subTopic,
            distImp,
            stateImp,
            natImp,
            isLocal,
            isReporter,
            media,
            media_duration,
            titleLen,
            postLang,
            creatorID,
            location_pid,
            post_state,
            -- Conversion history
            post_locks_so_far,
            post_initiates_so_far,
            post_trials_so_far,
            post_day0_locks,
            post_day0_initiates,
            post_day0_trials,
            post_day0_initiates_per_view,
            post_day0_trials_per_view,
            post_day0_initiates_per_lock
        FROM dynamic_snapshots
        WHERE date BETWEEN '{start_date}' AND '{end_date}'
    ),

    eco_features AS (
        -- EMR ecosystem features (7 features)
        -- Read from parquet files
        SELECT
            district_pid,
            date,
            district_posts_7d,
            district_reporters_7d,
            district_users_7d
        FROM read_parquet('s3://{BUCKET}/features/eco/district/date=*/*.parquet')
        WHERE date BETWEEN '{start_date}' AND '{end_date}'
    ),

    state_eco_features AS (
        SELECT
            state_name,
            date,
            state_posts_7d,
            state_reporters_7d
        FROM read_parquet('s3://{BUCKET}/features/eco/state/date=*/*.parquet')
        WHERE date BETWEEN '{start_date}' AND '{end_date}'
    ),

    lang_eco_features AS (
        SELECT
            postLang,
            date,
            lang_posts_7d,
            lang_reporters_7d
        FROM read_parquet('s3://{BUCKET}/features/eco/lang/date=*/*.parquet')
        WHERE date BETWEEN '{start_date}' AND '{end_date}'
    ),

    creator_features AS (
        -- EMR creator features (4 features)
        SELECT
            creatorID,
            date,
            creator_prior_rate,
            creator_has_prior,
            creator_verified,
            creator_viewcount
        FROM read_parquet('s3://{BUCKET}/features/creator/date=*/*.parquet')
        WHERE date BETWEEN '{start_date}' AND '{end_date}'
    )

    -- ═══════════════════════════════════════════════════════════════
    -- Final join with computed runtime features
    -- ═══════════════════════════════════════════════════════════════

    SELECT
        -- Keys
        l.user_id,
        l.post_id,
        d.run_ts,

        -- Target
        l.y,

        -- Dynamic features (from Lambda)
        d.created_ts,
        d.views,
        d.shareCount,
        d.mediaClickedCount,
        d.reactionCount,
        d.commentCount,
        d.readModeCount,
        d.avgDur,
        d.listenpct,

        -- Computed ratios (runtime features)
        CAST(d.shareCount AS DOUBLE) / NULLIF(d.views, 0) AS shares_pv,
        CAST(d.mediaClickedCount AS DOUBLE) / NULLIF(d.views, 0) AS videoclicks_pv,
        CAST(d.reactionCount AS DOUBLE) / NULLIF(d.views, 0) AS reactions_pv,
        CAST(d.commentCount AS DOUBLE) / NULLIF(d.views, 0) AS comments_pv,
        CAST(d.readModeCount AS DOUBLE) / NULLIF(d.views, 0) AS seemore_pv,

        -- Age at snapshot time
        (CAST(TO_UNIXTIME(CAST(d.run_ts AS TIMESTAMP)) AS BIGINT) * 1000 - d.created_ts) / 3600000.0 AS age_hours,

        -- Classified flag
        CASE WHEN d.topic IS NOT NULL AND d.subTopic IS NOT NULL THEN 1 ELSE 0 END AS classified,

        -- Static post meta
        d.topic,
        d.subTopic,
        d.distImp,
        d.stateImp,
        d.natImp,
        d.isLocal,
        d.isReporter,
        d.media,
        d.media_duration,
        d.titleLen,
        d.postLang,
        d.creatorID,
        d.location_pid,
        d.post_state,

        -- Conversion history
        d.post_locks_so_far,
        d.post_initiates_so_far,
        d.post_trials_so_far,
        d.post_day0_locks,
        d.post_day0_initiates,
        d.post_day0_trials,
        d.post_day0_initiates_per_view,
        d.post_day0_trials_per_view,
        d.post_day0_initiates_per_lock,

        -- Ecosystem features
        COALESCE(e.district_posts_7d, 0) AS district_posts_7d,
        COALESCE(e.district_reporters_7d, 0) AS district_reporters_7d,
        COALESCE(e.district_users_7d, 0) AS district_users_7d,
        COALESCE(se.state_posts_7d, 0) AS state_posts_7d,
        COALESCE(se.state_reporters_7d, 0) AS state_reporters_7d,
        COALESCE(le.lang_posts_7d, 0) AS lang_posts_7d,
        COALESCE(le.lang_reporters_7d, 0) AS lang_reporters_7d,

        -- Creator features
        COALESCE(c.creator_prior_rate, 0.0) AS creator_prior_rate,
        COALESCE(c.creator_has_prior, 0) AS creator_has_prior,
        COALESCE(c.creator_verified, false) AS creator_verified,
        COALESCE(c.creator_viewcount, 0) AS creator_viewcount

    FROM labels l
    INNER JOIN dynamic_features d
        ON l.post_id = d.post_id
        AND l.run_ts = d.run_ts
    LEFT JOIN eco_features e
        ON d.location_pid = e.district_pid
        AND CAST(d.run_ts AS DATE) = e.date
    LEFT JOIN state_eco_features se
        ON d.post_state = se.state_name
        AND CAST(d.run_ts AS DATE) = se.date
    LEFT JOIN lang_eco_features le
        ON d.postLang = le.postLang
        AND CAST(d.run_ts AS DATE) = le.date
    LEFT JOIN creator_features c
        ON d.creatorID = c.creatorID
        AND CAST(d.run_ts AS DATE) = c.date
    """

    print(f"Building training dataset for {start_date} to {end_date}...")
    print("This may take 10-30 minutes for large datasets...")

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
            stats = status['QueryExecution']['Statistics']
            print(f"\n✅ Training dataset created!")
            print(f"Data scanned: {stats.get('DataScannedInBytes', 0) / 1e9:.2f} GB")
            print(f"Execution time: {stats.get('EngineExecutionTimeInMillis', 0) / 1000:.1f} seconds")

            location = status['QueryExecution']['ResultConfiguration']['OutputLocation']
            print(f"\nResults: {location}")

            # Get row count
            return query_id
        elif state in ['FAILED', 'CANCELLED']:
            reason = status['QueryExecution']['Status'].get('StateChangeReason', 'Unknown')
            raise RuntimeError(f"Query failed: {reason}")

        print(f"Status: {state}...")
        time.sleep(15)


def get_dataset_stats(query_id):
    """Get statistics about the created dataset."""

    stats_query = f"""
    SELECT
        COUNT(*) AS total_rows,
        COUNT(DISTINCT user_id) AS unique_users,
        COUNT(DISTINCT post_id) AS unique_posts,
        SUM(CASE WHEN y = 0 THEN 1 ELSE 0 END) AS y_0,
        SUM(CASE WHEN y = 1 THEN 1 ELSE 0 END) AS y_1,
        SUM(CASE WHEN y = 2 THEN 1 ELSE 0 END) AS y_2,
        CAST(SUM(CASE WHEN y = 1 THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*) * 100 AS pct_initiate,
        CAST(SUM(CASE WHEN y = 2 THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*) * 100 AS pct_trial
    FROM (
        -- Results from previous query
        SELECT * FROM "{query_id}"
    )
    """

    print("\nGetting dataset statistics...")

    response = athena.start_query_execution(
        QueryString=stats_query,
        QueryExecutionContext={'Database': ATHENA_DATABASE},
        ResultConfiguration={'OutputLocation': ATHENA_OUTPUT}
    )

    stats_query_id = response['QueryExecutionId']

    while True:
        status = athena.get_query_execution(QueryExecutionId=stats_query_id)
        state = status['QueryExecution']['Status']['State']

        if state == 'SUCCEEDED':
            break
        elif state in ['FAILED', 'CANCELLED']:
            print("Could not get stats")
            return

        time.sleep(3)

    # Fetch results
    results = athena.get_query_results(QueryExecutionId=stats_query_id)

    if len(results['ResultSet']['Rows']) > 1:
        row = results['ResultSet']['Rows'][1]['Data']
        print(f"""
╔══════════════════════════════════════════════════════════════╗
║                   TRAINING DATASET STATS                     ║
╠══════════════════════════════════════════════════════════════╣
║  Total rows:        {row[0].get('VarCharValue', '0'):>15}                      ║
║  Unique users:      {row[1].get('VarCharValue', '0'):>15}                      ║
║  Unique posts:      {row[2].get('VarCharValue', '0'):>15}                      ║
║                                                              ║
║  Label distribution:                                         ║
║    y=0 (no convert): {row[3].get('VarCharValue', '0'):>14} ({100 - float(row[6].get('VarCharValue', '0')) - float(row[7].get('VarCharValue', '0')):.2f}%)                   ║
║    y=1 (initiate):   {row[4].get('VarCharValue', '0'):>14} ({row[6].get('VarCharValue', '0')}%)                   ║
║    y=2 (trial):      {row[5].get('VarCharValue', '0'):>14} ({row[7].get('VarCharValue', '0')}%)                   ║
╚══════════════════════════════════════════════════════════════╝
        """)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Build training dataset')
    parser.add_argument('--start-date', required=True, help='Start date YYYY-MM-DD')
    parser.add_argument('--end-date', required=True, help='End date YYYY-MM-DD')

    args = parser.parse_args()

    query_id = build_training_dataset(args.start_date, args.end_date)

    get_dataset_stats(query_id)

    print(f"""
    ✅ Training dataset ready!

    Next steps:
    1. Download the dataset:
       aws s3 cp {ATHENA_OUTPUT}{query_id}.csv training_data.csv

    2. Or use it directly in Athena for model training

    3. Train your model!
    """)