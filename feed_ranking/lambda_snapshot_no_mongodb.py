"""
Lambda function for feed ranking - 15-minute snapshots
NO MONGODB VERSION - Uses only S3 + Athena appevents

Triggered every 15 minutes, computes point-in-time features:
- Post metadata (18 variables) from classification dumps
- Engagement counts (8 variables) from post_seen events in appevents
- Conversion history (9 variables) from feed_locked/initiate_transaction events
Total: 35 variables captured

Output: s3://nearme-feed-store/features/snapshots/ts=<timestamp>/
"""
import json
import boto3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

s3 = boto3.client('s3')
athena = boto3.client('athena', region_name='ap-south-1')

BUCKET = "nearme-feed-store"
CLASSIFICATION_BUCKET = "closeapp-athena"
IST = ZoneInfo("Asia/Kolkata")

def lambda_handler(event, context):
    """Main handler - triggered every 15 minutes"""

    run_ts = int(datetime.now(IST).timestamp() * 1000)
    run_date = datetime.fromtimestamp(run_ts / 1000, IST).strftime("%Y-%m-%d")

    print(f"Starting snapshot at {run_ts}")

    # =================================================================
    # 1. READ POST METADATA from Classification Dumps (S3)
    # =================================================================

    # Query to get recent classified posts
    posts_query = f"""
    SELECT
        json_extract_scalar(data, '$.id') as post_id,
        COALESCE(
            CAST(json_extract_scalar(data, '$.created') AS BIGINT),
            CAST(json_extract_scalar(data, '$.createdAt') AS BIGINT)
        ) as created_ts,
        json_extract_scalar(data, '$.topic') as topic,
        COALESCE(
            json_extract_scalar(data, '$.subTopic'),
            json_extract_scalar(data, '$.sub_topic')
        ) as subTopic,
        COALESCE(
            CAST(json_extract_scalar(data, '$.districtImportanceLevel') AS INT),
            CAST(json_extract_scalar(data, '$.district_score') AS INT)
        ) as distImp,
        COALESCE(
            CAST(json_extract_scalar(data, '$.stateImportanceLevel') AS INT),
            CAST(json_extract_scalar(data, '$.state_score') AS INT)
        ) as stateImp,
        COALESCE(
            CAST(json_extract_scalar(data, '$.nationalImportanceLevel') AS INT),
            CAST(json_extract_scalar(data, '$.national_score') AS INT)
        ) as natImp,
        COALESCE(
            json_extract_scalar(data, '$.isLocal'),
            json_extract_scalar(data, '$.is_local')
        ) = 'true' as isLocal,
        COALESCE(
            json_extract_scalar(data, '$.isReporter'),
            json_extract_scalar(data, '$.is_reporter')
        ) = 'true' as isReporter,
        json_extract_scalar(data, '$.media') as media,
        CAST(json_extract_scalar(data, '$.duration') AS INT) as media_duration,
        LENGTH(json_extract_scalar(data, '$.title')) as titleLen,
        COALESCE(
            json_extract_scalar(data, '$.contentLang'),
            json_extract_scalar(data, '$.lang')
        ) as postLang,
        COALESCE(
            json_extract_scalar(data, '$.creatorID'),
            json_extract_scalar(data, '$.creator_pid'),
            json_extract_scalar(data, '$.creatorId')
        ) as creatorID,
        COALESCE(
            json_extract_scalar(data, '$.location.pid'),
            json_extract_scalar(data, '$.district_pid'),
            json_extract_scalar(data, '$.location_pid')
        ) as location_pid
    FROM {CLASSIFICATION_BUCKET}.post_classification
    WHERE event_name = 'post_classification_data_dump'
      AND date = '{run_date}'
      AND json_extract_scalar(data, '$.id') IS NOT NULL
    """

    posts_result = run_athena_query(posts_query)
    posts = {row['post_id']: row for row in posts_result}

    print(f"Loaded {len(posts)} posts from classification dumps")

    # =================================================================
    # 2. COMPUTE ENGAGEMENT from post_seen Events (Athena)
    # =================================================================

    engagement_query = f"""
    WITH postseen_parsed AS (
        SELECT
            json_extract_scalar(data, '$.postId') as post_id,
            json_extract_scalar(data, '$.postEngagement.mediaClicked') as media_clicked,
            json_extract_scalar(data, '$.postEngagement.readModeOpen') as read_mode_open,
            CAST(json_extract_scalar(data, '$.postEngagement.screenTime') AS INTEGER) as screen_time,
            CAST(json_extract_scalar(data, '$.maxListenDuration') AS INTEGER) as max_listen_duration
        FROM closeapp.appevents
        WHERE event_name = 'post_seen'
          AND date >= '{(datetime.fromtimestamp(run_ts/1000, IST) - timedelta(days=1)).strftime("%Y-%m-%d")}'
          AND event_time < {run_ts}
          AND json_extract_scalar(data, '$.postId') IS NOT NULL
    )
    SELECT
        post_id,
        COUNT(*) as views,
        SUM(CASE WHEN media_clicked = 'true' THEN 1 ELSE 0 END) as mediaClickedCount,
        SUM(CASE WHEN read_mode_open = 'true' THEN 1 ELSE 0 END) as readModeCount,
        AVG(screen_time) as avgDur,
        AVG(max_listen_duration) as listenpct
    FROM postseen_parsed
    GROUP BY post_id
    """

    engagement_result = run_athena_query(engagement_query)
    engagement_map = {row['post_id']: row for row in engagement_result}

    print(f"Computed engagement for {len(engagement_map)} posts from post_seen events")

    # =================================================================
    # 3. COMPUTE SHARES, REACTIONS, COMMENTS from appevents
    # =================================================================

    social_query = f"""
    SELECT
        json_extract_scalar(data, '$.postId') as post_id,
        SUM(CASE WHEN event_name = 'post_shared' THEN 1 ELSE 0 END) as shareCount,
        SUM(CASE WHEN event_name = 'post_reacted' THEN 1 ELSE 0 END) as reactionCount,
        SUM(CASE WHEN event_name = 'comment_posted' THEN 1 ELSE 0 END) as commentCount
    FROM closeapp.appevents
    WHERE event_name IN ('post_shared', 'post_reacted', 'comment_posted')
      AND date >= '{(datetime.fromtimestamp(run_ts/1000, IST) - timedelta(days=1)).strftime("%Y-%m-%d")}'
      AND event_time < {run_ts}
      AND json_extract_scalar(data, '$.postId') IS NOT NULL
    GROUP BY json_extract_scalar(data, '$.postId')
    """

    social_result = run_athena_query(social_query)
    social_map = {row['post_id']: row for row in social_result}

    print(f"Computed social engagement for {len(social_map)} posts")

    # =================================================================
    # 4. COMPUTE CONVERSION HISTORY (locks, initiates, trials)
    # =================================================================

    conversions_query = f"""
    WITH events AS (
        SELECT
            json_extract_scalar(data, '$.postId') as post_id,
            event_name,
            event_time,
            CAST(json_extract_scalar(data, '$.userAge') AS INT) as user_age
        FROM closeapp.appevents
        WHERE event_name IN ('feed_locked', 'initiate_transaction', 'trial_payment_success')
          AND date >= '{(datetime.fromtimestamp(run_ts/1000, IST) - timedelta(days=7)).strftime("%Y-%m-%d")}'
          AND event_time < {run_ts}
          AND json_extract_scalar(data, '$.postId') IS NOT NULL
    )
    SELECT
        post_id,
        -- Total conversions (all time before T)
        SUM(CASE WHEN event_name = 'feed_locked' THEN 1 ELSE 0 END) as post_locks_so_far,
        SUM(CASE WHEN event_name = 'initiate_transaction' THEN 1 ELSE 0 END) as post_initiates_so_far,
        SUM(CASE WHEN event_name = 'trial_payment_success' THEN 1 ELSE 0 END) as post_trials_so_far,
        -- Day-0 conversions (user_age = 0)
        SUM(CASE WHEN event_name = 'feed_locked' AND user_age = 0 THEN 1 ELSE 0 END) as post_day0_locks,
        SUM(CASE WHEN event_name = 'initiate_transaction' AND user_age = 0 THEN 1 ELSE 0 END) as post_day0_initiates,
        SUM(CASE WHEN event_name = 'trial_payment_success' AND user_age = 0 THEN 1 ELSE 0 END) as post_day0_trials
    FROM events
    GROUP BY post_id
    """

    conversions_result = run_athena_query(conversions_query)
    conversions_map = {row['post_id']: row for row in conversions_result}

    print(f"Computed conversion history for {len(conversions_map)} posts")

    # =================================================================
    # 5. COMBINE ALL FEATURES
    # =================================================================

    snapshot = []

    for post_id, post in posts.items():
        engagement = engagement_map.get(post_id, {})
        social = social_map.get(post_id, {})
        conversions = conversions_map.get(post_id, {})

        views = engagement.get('views', 0)

        feature_row = {
            # Post metadata (18 variables)
            'post_id': post_id,
            'created_ts': post.get('created_ts', 0),
            'topic': post.get('topic'),
            'subTopic': post.get('subTopic'),
            'classified': 1 if post.get('topic') and post.get('subTopic') else 0,
            'distImp': post.get('distImp', 0),
            'stateImp': post.get('stateImp', 0),
            'natImp': post.get('natImp', 0),
            'isLocal': post.get('isLocal', False),
            'isReporter': post.get('isReporter', False),
            'media': post.get('media'),
            'media_duration': post.get('media_duration', 0),
            'titleLen': post.get('titleLen', 0),
            'postLang': post.get('postLang'),
            'creatorID': post.get('creatorID'),
            'location_pid': post.get('location_pid'),
            'age_hours': (run_ts - post.get('created_ts', run_ts)) / 3600000,

            # Engagement (8 variables)
            'views': views,
            'shareCount': social.get('shareCount', 0),
            'mediaClickedCount': engagement.get('mediaClickedCount', 0),
            'reactionCount': social.get('reactionCount', 0),
            'commentCount': social.get('commentCount', 0),
            'readModeCount': engagement.get('readModeCount', 0),
            'avgDur': engagement.get('avgDur', 0.0),
            'listenpct': engagement.get('listenpct', 0.0),

            # Conversion history (9 variables)
            'post_locks_so_far': conversions.get('post_locks_so_far', 0),
            'post_initiates_so_far': conversions.get('post_initiates_so_far', 0),
            'post_trials_so_far': conversions.get('post_trials_so_far', 0),
            'post_day0_locks': conversions.get('post_day0_locks', 0),
            'post_day0_initiates': conversions.get('post_day0_initiates', 0),
            'post_day0_trials': conversions.get('post_day0_trials', 0),
            'post_day0_initiates_per_view': conversions.get('post_day0_initiates', 0) / views if views > 0 else 0,
            'post_day0_trials_per_view': conversions.get('post_day0_trials', 0) / views if views > 0 else 0,
            'post_day0_initiates_per_lock': conversions.get('post_day0_initiates', 0) / conversions.get('post_day0_locks', 1),

            # Snapshot metadata
            'snapshot_ts': run_ts
        }

        snapshot.append(feature_row)

    # =================================================================
    # 6. WRITE TO S3
    # =================================================================

    output_key = f"features/snapshots/ts={run_ts}/data.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=output_key,
        Body=json.dumps(snapshot, indent=2),
        ContentType='application/json'
    )

    print(f"✅ Snapshot written: s3://{BUCKET}/{output_key}")
    print(f"   {len(snapshot)} posts, 35 variables per post")
    print("   NO MONGODB USED - All from S3 + Athena appevents!")

    return {
        'statusCode': 200,
        'body': json.dumps({
            'snapshot_ts': run_ts,
            'posts_count': len(snapshot),
            'variables': 35,
            'output': f's3://{BUCKET}/{output_key}'
        })
    }


def run_athena_query(query_string):
    """Execute Athena query and return results as list of dicts"""

    response = athena.start_query_execution(
        QueryString=query_string,
        QueryExecutionContext={'Database': 'closeapp'},
        ResultConfiguration={'OutputLocation': f's3://{BUCKET}/athena-results/'}
    )

    execution_id = response['QueryExecutionId']

    # Wait for completion
    import time
    while True:
        result = athena.get_query_execution(QueryExecutionId=execution_id)
        state = result['QueryExecution']['Status']['State']

        if state == 'SUCCEEDED':
            break
        elif state in ['FAILED', 'CANCELLED']:
            raise Exception(f"Query {state}: {result['QueryExecution']['Status'].get('StateChangeReason', '')}")

        time.sleep(1)

    # Get results
    results = athena.get_query_results(QueryExecutionId=execution_id, MaxResults=10000)

    # Parse results
    columns = [col['Name'] for col in results['ResultSet']['ResultSetMetadata']['ColumnInfo']]
    rows = []

    for row in results['ResultSet']['Rows'][1:]:  # Skip header
        row_data = {}
        for i, col in enumerate(columns):
            value = row['Data'][i].get('VarCharValue')
            # Try to convert to appropriate type
            if value is not None:
                try:
                    # Try int first
                    if '.' not in value:
                        row_data[col] = int(value)
                    else:
                        row_data[col] = float(value)
                except (ValueError, TypeError):
                    # Keep as string
                    row_data[col] = value
            else:
                row_data[col] = None

        rows.append(row_data)

    return rows