"""
15-min dynamic engagement snapshot - NO MONGODB VERSION

Uses only:
  - S3 classification dumps (post metadata)
  - Athena appevents (engagement + conversions)
  - S3 static file (Place mapping)

NO MongoDB connection needed!

EventBridge: rate(15 minutes)
VPC: NO (no MongoDB access needed)
Timeout: 10 minutes
Memory: 1024 MB
"""
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import boto3
import pyarrow.parquet as pq

IST = ZoneInfo("Asia/Kolkata")
BUCKET = os.environ.get("BUCKET", "nearme-feed-store")
PREFIX = os.environ.get("PREFIX", "dynamic")
POST_WINDOW_DAYS = int(os.environ.get("POST_WINDOW_DAYS", "7"))
ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "closeapp")
ATHENA_OUTPUT = os.environ.get("ATHENA_OUTPUT", f"s3://{BUCKET}/athena-results")
CLASSIFICATION_BUCKET = os.environ.get("CLASSIFICATION_BUCKET", "closeapp-athena")
CLASSIFICATION_PREFIX = "post_classification/event_name=post_classification_data_dump"

s3 = boto3.client("s3")
athena = boto3.client("athena")

# Cache for district->state mapping (loaded from S3)
_district_state_map = None


def load_district_state_map_from_s3():
    """
    Load district->state mapping from S3 static file.
    NO MongoDB connection needed!
    """
    global _district_state_map
    if _district_state_map is not None:
        return _district_state_map

    try:
        response = s3.get_object(
            Bucket=BUCKET,
            Key='config/district_state_mapping.json'
        )
        mapping_json = response['Body'].read().decode('utf-8')
        mapping_data = json.loads(mapping_json)

        # Extract district_pid -> state_name
        _district_state_map = {
            district_pid: info['state_name']
            for district_pid, info in mapping_data.items()
        }

        print(f"Loaded district->state mapping: {len(_district_state_map)} districts")
        return _district_state_map

    except Exception as e:
        print(f"WARN: Could not load Place mapping from S3: {e}")
        return {}


def check_cold_start(date_str):
    """Check if any snapshot exists for today or yesterday."""
    today = datetime.strptime(date_str, "%Y-%m-%d")
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    for d in [date_str, yesterday]:
        try:
            resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{PREFIX}/date={d}/", MaxKeys=1)
            if resp.get("KeyCount", 0) > 0:
                return False
        except Exception:
            pass
    return True


def load_previous_snapshot(date_str, prev_run_slot):
    """Load previous snapshot (15 min ago) as state."""
    key = f"{PREFIX}/date={date_str}/run={prev_run_slot}/snapshot.jsonl"
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        lines = obj["Body"].read().decode("utf-8").strip().split("\n")
        return {json.loads(line)["post_id"]: json.loads(line) for line in lines if line}
    except s3.exceptions.NoSuchKey:
        return {}
    except Exception as e:
        print(f"WARN: failed to load previous snapshot {key}: {e}")
        return {}


def read_classification_dumps(since_date, now_date):
    """
    Read post classification dumps from S3.

    Files are in appevents format with JSON in the 'data' column.
    """
    posts = {}
    date = datetime.strptime(since_date, "%Y-%m-%d")
    end = datetime.strptime(now_date, "%Y-%m-%d")

    while date <= end:
        date_str = date.strftime("%Y-%m-%d")
        prefix = f"{CLASSIFICATION_PREFIX}/date={date_str}/"

        try:
            resp = s3.list_objects_v2(Bucket=CLASSIFICATION_BUCKET, Prefix=prefix)
            if "Contents" not in resp:
                date += timedelta(days=1)
                continue

            for obj in resp["Contents"]:
                if not obj["Key"].endswith(".parquet"):
                    continue

                local_path = f"/tmp/{os.path.basename(obj['Key'])}"
                s3.download_file(CLASSIFICATION_BUCKET, obj["Key"], local_path)

                table = pq.read_table(local_path)
                df = table.to_pandas()

                # Parse each event row (appevents format)
                for _, row in df.iterrows():
                    # Skip if not classification event
                    if row.get("event_name") != "post_classification_data_dump":
                        continue

                    # Parse JSON from data column
                    data_json = row.get("data")
                    if not data_json:
                        continue

                    try:
                        post_data = json.loads(data_json)
                    except:
                        continue

                    # Extract post ID
                    post_id = post_data.get("id") or post_data.get("postId")
                    if not post_id:
                        continue

                    posts[post_id] = {
                        "id": post_id,
                        "c": post_data.get("created") or post_data.get("createdAt"),
                        "topic": post_data.get("topic"),
                        "subTopic": post_data.get("subTopic") or post_data.get("sub_topic"),
                        "districtImportanceLevel": post_data.get("districtImportanceLevel") or post_data.get("district_score"),
                        "stateImportanceLevel": post_data.get("stateImportanceLevel") or post_data.get("state_score"),
                        "nationalImportanceLevel": post_data.get("nationalImportanceLevel") or post_data.get("national_score"),
                        "isLocal": post_data.get("isLocal") or post_data.get("is_local", False),
                        "isReporter": post_data.get("isReporter") or post_data.get("is_reporter", False),
                        "containsVisualMedia": post_data.get("containsVisualMedia") or (post_data.get("media_duration") or post_data.get("duration")) is not None,
                        "duration": post_data.get("duration") or post_data.get("media_duration"),
                        "titleLength": post_data.get("titleLength") or len(post_data.get("title", "")),
                        "contentLang": post_data.get("contentLang") or post_data.get("lang"),
                        "creatorID": post_data.get("creatorID") or post_data.get("creator_pid") or post_data.get("creatorId"),
                        "location": {"pid": post_data.get("location", {}).get("pid") or post_data.get("district_pid") or post_data.get("location_pid")},
                    }

                os.remove(local_path)

        except Exception as e:
            print(f"WARN: failed to read dumps for {date_str}: {e}")

        date += timedelta(days=1)

    return posts


def query_engagement_and_conversions_athena(post_ids, since_date, since_ts=None):
    """
    Query BOTH engagement AND conversion metrics from appevents in ONE query.

    Returns: {post_id: {
        views, shares, reactions, comments, avg_dur,
        locks, initiates, trials, day0_locks, day0_initiates, day0_trials
    }}
    """
    if not post_ids:
        return {}

    # Chunk to avoid query size limits (Athena max 262KB)
    if len(post_ids) > 1000:
        results = {}
        for i in range(0, len(post_ids), 1000):
            results.update(query_engagement_and_conversions_athena(
                post_ids[i:i+1000], since_date, since_ts
            ))
        return results

    post_list = ",".join(f"'{p}'" for p in post_ids)
    time_filter = f"AND event_time >= TIMESTAMP '{since_ts}'" if since_ts else ""

    query = f"""
    WITH day0_users AS (
        SELECT DISTINCT user_id
        FROM {ATHENA_DATABASE}.appevents
        WHERE event_name = 'feed_first_call'
          AND JSON_EXTRACT_SCALAR(data, '$.userAge') = '0'
          AND date >= '{since_date}'
    ),

    -- ═══════════════════════════════════════════════════════════════
    -- ENGAGEMENT METRICS (from appevents)
    -- ═══════════════════════════════════════════════════════════════

    engagement AS (
        SELECT
            JSON_EXTRACT_SCALAR(data, '$.postId') AS post_id,
            -- Views (feed_locked with isLocked=true)
            SUM(CASE WHEN event_name = 'feed_locked' AND JSON_EXTRACT_SCALAR(data, '$.isLocked') = 'true' THEN 1 ELSE 0 END) AS views,
            -- Shares (post_shared event)
            SUM(CASE WHEN event_name = 'post_shared' THEN 1 ELSE 0 END) AS shares,
            -- Reactions (post_reaction event - confirmed name)
            SUM(CASE WHEN event_name = 'post_reaction' THEN 1 ELSE 0 END) AS reactions,
            -- Comments (comment_created event - confirmed name)
            SUM(CASE WHEN event_name = 'comment_created' THEN 1 ELSE 0 END) AS comments,
            -- Media clicks (check if exists)
            SUM(CASE WHEN event_name = 'media_clicked' THEN 1 ELSE 0 END) AS media_clicks,
            -- Read mode (check if exists)
            SUM(CASE WHEN event_name = 'read_mode_entered' THEN 1 ELSE 0 END) AS read_mode,
            -- Avg duration (from feed_locked events with duration field)
            AVG(CASE WHEN event_name = 'feed_locked' AND JSON_EXTRACT_SCALAR(data, '$.duration') IS NOT NULL
                     THEN CAST(JSON_EXTRACT_SCALAR(data, '$.duration') AS DOUBLE) ELSE NULL END) AS avg_dur
        FROM {ATHENA_DATABASE}.appevents
        WHERE date >= '{since_date}'
          {time_filter}
          AND JSON_EXTRACT_SCALAR(data, '$.postId') IN ({post_list})
          AND event_name IN ('feed_locked', 'post_shared', 'post_reaction', 'comment_created',
                             'media_clicked', 'read_mode_entered')
        GROUP BY JSON_EXTRACT_SCALAR(data, '$.postId')
    ),

    -- ═══════════════════════════════════════════════════════════════
    -- CONVERSION METRICS (day-0 specific)
    -- ═══════════════════════════════════════════════════════════════

    locks AS (
        SELECT
            JSON_EXTRACT_SCALAR(e.data, '$.postId') AS post_id,
            e.user_id,
            CASE WHEN u.user_id IS NOT NULL THEN 1 ELSE 0 END AS is_day0
        FROM {ATHENA_DATABASE}.appevents e
        LEFT JOIN day0_users u ON e.user_id = u.user_id
        WHERE e.event_name = 'feed_locked'
          AND JSON_EXTRACT_SCALAR(e.data, '$.isLocked') = 'true'
          AND e.date >= '{since_date}'
          {time_filter}
          AND JSON_EXTRACT_SCALAR(e.data, '$.postId') IN ({post_list})
    ),

    initiates AS (
        SELECT
            JSON_EXTRACT_SCALAR(e.data, '$.postId') AS post_id,
            e.user_id,
            CASE WHEN u.user_id IS NOT NULL THEN 1 ELSE 0 END AS is_day0
        FROM {ATHENA_DATABASE}.appevents e
        LEFT JOIN day0_users u ON e.user_id = u.user_id
        WHERE e.event_name = 'initiate_transaction'
          AND JSON_EXTRACT_SCALAR(e.data, '$.isInitiatedFromPost') = 'true'
          AND e.date >= '{since_date}'
          {time_filter}
          AND JSON_EXTRACT_SCALAR(e.data, '$.postId') IN ({post_list})
    ),

    trials AS (
        SELECT user_id
        FROM {ATHENA_DATABASE}.appevents e
        JOIN day0_users u ON e.user_id = u.user_id
        WHERE e.event_name = 'subscription_trial_started'
          AND e.date >= '{since_date}'
          {time_filter}
    ),

    conversions AS (
        SELECT
            l.post_id,
            COUNT(*) AS locks,
            SUM(CASE WHEN i.user_id IS NOT NULL THEN 1 ELSE 0 END) AS initiates,
            SUM(CASE WHEN t.user_id IS NOT NULL THEN 1 ELSE 0 END) AS trials,
            SUM(l.is_day0) AS day0_locks,
            SUM(CASE WHEN l.is_day0 = 1 AND i.user_id IS NOT NULL THEN 1 ELSE 0 END) AS day0_initiates,
            SUM(CASE WHEN l.is_day0 = 1 AND t.user_id IS NOT NULL THEN 1 ELSE 0 END) AS day0_trials
        FROM locks l
        LEFT JOIN initiates i ON l.post_id = i.post_id AND l.user_id = i.user_id
        LEFT JOIN trials t ON l.user_id = t.user_id
        GROUP BY l.post_id
    )

    -- ═══════════════════════════════════════════════════════════════
    -- COMBINE ENGAGEMENT + CONVERSIONS
    -- ═══════════════════════════════════════════════════════════════

    SELECT
        COALESCE(e.post_id, c.post_id) AS post_id,
        -- Engagement
        COALESCE(e.views, 0) AS views,
        COALESCE(e.shares, 0) AS shares,
        COALESCE(e.reactions, 0) AS reactions,
        COALESCE(e.comments, 0) AS comments,
        COALESCE(e.media_clicks, 0) AS media_clicks,
        COALESCE(e.read_mode, 0) AS read_mode,
        COALESCE(e.avg_dur, 0.0) AS avg_dur,
        -- Conversions
        COALESCE(c.locks, 0) AS locks,
        COALESCE(c.initiates, 0) AS initiates,
        COALESCE(c.trials, 0) AS trials,
        COALESCE(c.day0_locks, 0) AS day0_locks,
        COALESCE(c.day0_initiates, 0) AS day0_initiates,
        COALESCE(c.day0_trials, 0) AS day0_trials
    FROM engagement e
    FULL OUTER JOIN conversions c ON e.post_id = c.post_id
    """

    # Execute Athena query (use workgroup instead of direct output location)
    response = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": ATHENA_DATABASE},
        WorkGroup="feed-ranking"
    )
    query_id = response["QueryExecutionId"]

    # Wait for completion (timeout 10 min)
    for _ in range(300):
        status = athena.get_query_execution(QueryExecutionId=query_id)
        state = status["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        elif state in ["FAILED", "CANCELLED"]:
            raise RuntimeError(f"Athena query failed: {status}")
        time.sleep(2)
    else:
        raise RuntimeError(f"Athena query timeout: {query_id}")

    # Fetch results
    results = {}
    paginator = athena.get_paginator("get_query_results")
    for page in paginator.paginate(QueryExecutionId=query_id):
        for i, row in enumerate(page["ResultSet"]["Rows"]):
            if i == 0:  # skip header
                continue
            data = [col.get("VarCharValue") for col in row["Data"]]
            if len(data) >= 14:
                results[data[0]] = {
                    "views": int(data[1] or 0),
                    "shares": int(data[2] or 0),
                    "reactions": int(data[3] or 0),
                    "comments": int(data[4] or 0),
                    "media_clicks": int(data[5] or 0),
                    "read_mode": int(data[6] or 0),
                    "avg_dur": float(data[7] or 0.0),
                    "locks": int(data[8] or 0),
                    "initiates": int(data[9] or 0),
                    "trials": int(data[10] or 0),
                    "day0_locks": int(data[11] or 0),
                    "day0_initiates": int(data[12] or 0),
                    "day0_trials": int(data[13] or 0),
                }
    return results


def lambda_handler(event, context):
    """Lambda entry point - NO MONGODB VERSION."""
    print(f"Starting NO-MONGODB snapshot: {POST_WINDOW_DAYS} days, all data from appevents")

    start_time = time.time()
    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    run_slot = now.strftime("%H%M")
    run_ts = now.isoformat()

    # Load district->state mapping from S3 (cached)
    district_state_map = load_district_state_map_from_s3()

    # Check cold start
    is_cold_start = check_cold_start(date_str)
    event_since_date = (now - timedelta(days=POST_WINDOW_DAYS)).strftime("%Y-%m-%d")

    if is_cold_start:
        print(f"COLD START — pulling {POST_WINDOW_DAYS} days, full event scan")
        prev_snapshot = {}
    else:
        print(f"NORMAL RUN — incremental update ({POST_WINDOW_DAYS}d dumps, 15min delta)")
        prev_run_slot = (now - timedelta(minutes=15)).strftime("%H%M")
        prev_snapshot = load_previous_snapshot(date_str, prev_run_slot)

    # Read classification dumps
    if is_cold_start:
        # Cold start: read entire window (7 days)
        print(f"Reading classification dumps from {event_since_date} to {date_str}...")
        post_meta = read_classification_dumps(event_since_date, date_str)
    else:
        # Incremental: only read today's dumps (new posts only)
        print(f"Reading classification dumps from {date_str} (today only)...")
        post_meta = read_classification_dumps(date_str, date_str)

    # Merge with previous snapshot
    if not is_cold_start:
        cutoff_ms = int((now - timedelta(days=POST_WINDOW_DAYS)).timestamp() * 1000)
        for pid, prev_row in prev_snapshot.items():
            if pid not in post_meta and prev_row.get("created_ts", 0) >= cutoff_ms:
                post_meta[pid] = {
                    "id": pid,
                    "c": prev_row.get("created_ts"),
                    "topic": prev_row.get("topic"),
                    "subTopic": prev_row.get("subTopic"),
                    "districtImportanceLevel": prev_row.get("distImp"),
                    "stateImportanceLevel": prev_row.get("stateImp"),
                    "nationalImportanceLevel": prev_row.get("natImp"),
                    "isLocal": prev_row.get("isLocal", False),
                    "isReporter": prev_row.get("isReporter", False),
                    "containsVisualMedia": prev_row.get("media", False),
                    "duration": prev_row.get("media_duration"),
                    "titleLength": prev_row.get("titleLen"),
                    "contentLang": prev_row.get("postLang"),
                    "creatorID": prev_row.get("creatorID"),
                    "location": {"pid": prev_row.get("location_pid")},
                }

    post_ids = list(post_meta.keys())
    print(f"Total posts in window: {len(post_ids)}")

    # Query engagement + conversions from appevents
    new_posts = [pid for pid in post_ids if pid not in prev_snapshot]
    existing_posts = [pid for pid in post_ids if pid in prev_snapshot]

    # For new posts (or cold start), scan full history
    if new_posts or is_cold_start:
        scan_ids = new_posts if not is_cold_start else post_ids
        print(f"Querying appevents for {len(scan_ids)} posts (full history)...")
        new_metrics = query_engagement_and_conversions_athena(scan_ids, event_since_date)
    else:
        new_metrics = {}

    # For existing posts, scan last 15 min delta
    if existing_posts and not is_cold_start:
        delta_since_ts = (now - timedelta(minutes=15)).isoformat()
        print(f"Querying appevents for {len(existing_posts)} posts (15-min delta)...")
        delta_metrics = query_engagement_and_conversions_athena(existing_posts, event_since_date, delta_since_ts)
    else:
        delta_metrics = {}

    # Build rows
    rows = []

    for pid in post_ids:
        meta = post_meta.get(pid, {})
        loc = meta.get("location", {})
        location_pid = loc.get("pid")
        post_state = district_state_map.get(location_pid) if location_pid else None

        # Engagement + conversion metrics (incremental or full)
        if pid in prev_snapshot and not is_cold_start:
            # Incremental
            prev = prev_snapshot[pid]
            delta = delta_metrics.get(pid, {})

            views = prev.get("views", 0) + delta.get("views", 0)
            shares = prev.get("shareCount", 0) + delta.get("shares", 0)
            reactions = prev.get("reactionCount", 0) + delta.get("reactions", 0)
            comments = prev.get("commentCount", 0) + delta.get("comments", 0)
            media_clicks = prev.get("mediaClickedCount", 0) + delta.get("media_clicks", 0)
            read_mode = prev.get("readModeCount", 0) + delta.get("read_mode", 0)

            # For avg_dur, recompute weighted average (approximate)
            avg_dur = delta.get("avg_dur", prev.get("avgDur", 0))

            event_counts = {
                "post_locks_so_far": prev.get("post_locks_so_far", 0) + delta.get("locks", 0),
                "post_initiates_so_far": prev.get("post_initiates_so_far", 0) + delta.get("initiates", 0),
                "post_trials_so_far": prev.get("post_trials_so_far", 0) + delta.get("trials", 0),
                "post_day0_locks": prev.get("post_day0_locks", 0) + delta.get("day0_locks", 0),
                "post_day0_initiates": prev.get("post_day0_initiates", 0) + delta.get("day0_initiates", 0),
                "post_day0_trials": prev.get("post_day0_trials", 0) + delta.get("day0_trials", 0),
            }
        else:
            # New post or cold start
            metrics = new_metrics.get(pid, {})
            views = metrics.get("views", 0)
            shares = metrics.get("shares", 0)
            reactions = metrics.get("reactions", 0)
            comments = metrics.get("comments", 0)
            media_clicks = metrics.get("media_clicks", 0)
            read_mode = metrics.get("read_mode", 0)
            avg_dur = metrics.get("avg_dur", 0.0)

            event_counts = {
                "post_locks_so_far": metrics.get("locks", 0),
                "post_initiates_so_far": metrics.get("initiates", 0),
                "post_trials_so_far": metrics.get("trials", 0),
                "post_day0_locks": metrics.get("day0_locks", 0),
                "post_day0_initiates": metrics.get("day0_initiates", 0),
                "post_day0_trials": metrics.get("day0_trials", 0),
            }

        # Compute rates
        day0_views = event_counts["post_day0_locks"]
        event_counts["post_day0_initiates_per_view"] = (
            event_counts["post_day0_initiates"] / day0_views if day0_views > 0 else 0.0
        )
        event_counts["post_day0_trials_per_view"] = (
            event_counts["post_day0_trials"] / day0_views if day0_views > 0 else 0.0
        )
        event_counts["post_day0_initiates_per_lock"] = (
            event_counts["post_day0_initiates"] / event_counts["post_day0_locks"]
            if event_counts["post_day0_locks"] > 0 else 0.0
        )

        row = {
            # metadata
            "run_ts": run_ts,
            "post_id": pid,
            "created_ts": meta.get("c"),

            # dynamic engagement (from appevents!)
            "views": views,
            "shareCount": shares,
            "mediaClickedCount": media_clicks,
            "reactionCount": reactions,
            "commentCount": comments,
            "readModeCount": read_mode,
            "avgDur": avg_dur,
            "listenpct": 0.0,  # TODO: if available in appevents

            # static post meta
            "topic": meta.get("topic"),
            "subTopic": meta.get("subTopic"),
            "distImp": meta.get("districtImportanceLevel"),
            "stateImp": meta.get("stateImportanceLevel"),
            "natImp": meta.get("nationalImportanceLevel"),
            "isLocal": meta.get("isLocal", False),
            "isReporter": meta.get("isReporter", False),
            "media": meta.get("containsVisualMedia", False),
            "media_duration": meta.get("duration"),
            "titleLen": meta.get("titleLength"),
            "postLang": meta.get("contentLang"),
            "creatorID": meta.get("creatorID"),
            "location_pid": location_pid,
            "post_state": post_state,

            # post conversion-history
            **event_counts,
        }
        rows.append(row)

    # Write JSONL
    key = f"{PREFIX}/date={date_str}/run={run_slot}/snapshot.jsonl"
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    s3.put_object(Bucket=BUCKET, Key=key, Body=body.encode("utf-8"), ContentType="application/x-ndjson")

    elapsed = time.time() - start_time

    print(f"{'COLD START' if is_cold_start else 'INCREMENTAL'} complete: {len(rows)} posts, "
          f"{len(new_posts)} new, {len(existing_posts)} carried forward → {key}")
    print(f"Execution time: {elapsed:.1f}s")

    return {
        "run_ts": run_ts,
        "is_cold_start": is_cold_start,
        "posts": len(rows),
        "new_posts": len(new_posts),
        "incremental_posts": len(existing_posts),
        "s3_key": key,
        "elapsed_seconds": round(elapsed, 1),
        "mongodb_used": False  # NO MONGODB!
    }