"""
Daily feed aggregates — EMR Serverless PySpark - NO MONGODB VERSION

Reads classification dumps (S3) + appevents (Glue Catalog), computes:
  - features/eco: district/state/lang × posts/reporters/users over 7d
  - features/creator: creator_prior_rate, verified (inferred), viewcount (computed)

Writes parquet to S3.

NO MONGODB NEEDED!
- Post data: From classification dumps (S3 parquet)
- Place mapping: From S3 JSON (pre-extracted)
- User data: Computed from appevents (viewcount) + inferred (verified from isReporter)
"""
import sys
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pyspark.sql import SparkSession
from pyspark.sql.functions import (
    col, count, countDistinct, lit, from_json, when,
    sum as sql_sum
)
from pyspark.sql.types import StructType, StructField, StringType, LongType, BooleanType, IntegerType

IST = ZoneInfo("Asia/Kolkata")
BUCKET = sys.argv[1] if len(sys.argv) > 1 else "nearme-feed-store"
RUN_DATE = sys.argv[2] if len(sys.argv) > 2 else datetime.now(IST).strftime("%Y-%m-%d")
CLASSIFICATION_BUCKET = sys.argv[3] if len(sys.argv) > 3 else "closeapp-athena"

spark = (SparkSession.builder
    .appName("feed-daily-aggregates-no-mongodb")
    .getOrCreate())

ref_date = datetime.strptime(RUN_DATE, "%Y-%m-%d").replace(tzinfo=IST)
window_start = int((ref_date - timedelta(days=7)).timestamp() * 1000)
prior_window_start = int((ref_date - timedelta(days=14)).timestamp() * 1000)
prior_window_end = int((ref_date - timedelta(days=7)).timestamp() * 1000)

# ============================================================
# READ FROM S3 - NO MONGODB!
# ============================================================

print("Reading classification dumps from S3...")

# Define schema for post data JSON
post_schema = StructType([
    StructField("id", StringType(), True),
    StructField("postId", StringType(), True),
    StructField("created", LongType(), True),
    StructField("createdAt", LongType(), True),
    StructField("topic", StringType(), True),
    StructField("subTopic", StringType(), True),
    StructField("sub_topic", StringType(), True),
    StructField("districtImportanceLevel", IntegerType(), True),
    StructField("district_score", IntegerType(), True),
    StructField("stateImportanceLevel", IntegerType(), True),
    StructField("state_score", IntegerType(), True),
    StructField("nationalImportanceLevel", IntegerType(), True),
    StructField("national_score", IntegerType(), True),
    StructField("isLocal", BooleanType(), True),
    StructField("is_local", BooleanType(), True),
    StructField("isReporter", BooleanType(), True),
    StructField("is_reporter", BooleanType(), True),
    StructField("contentLang", StringType(), True),
    StructField("lang", StringType(), True),
    StructField("creatorID", StringType(), True),
    StructField("creator_pid", StringType(), True),
    StructField("creatorId", StringType(), True),
    StructField("location", StructType([
        StructField("pid", StringType(), True)
    ]), True),
    StructField("district_pid", StringType(), True),
    StructField("location_pid", StringType(), True)
])

# Read classification dumps (appevents format)
# Date range: last 14 days
date_list = [(ref_date - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]

classification_raw = spark.read.parquet(
    *[f"s3://{CLASSIFICATION_BUCKET}/post_classification/event_name=post_classification_data_dump/date={d}/"
      for d in date_list]
)

# Parse JSON from data column
Post = (classification_raw
    .filter(col("event_name") == "post_classification_data_dump")
    .withColumn("post_data", from_json(col("data"), post_schema))
    .select(
        # Use coalesce to handle different field names
        (col("post_data.id") | col("post_data.postId")).alias("post_id"),
        (col("post_data.created") | col("post_data.createdAt")).alias("created_ms"),
        col("post_data.topic").alias("topic"),
        (col("post_data.subTopic") | col("post_data.sub_topic")).alias("subTopic"),
        (col("post_data.districtImportanceLevel") | col("post_data.district_score")).alias("distImp"),
        (col("post_data.stateImportanceLevel") | col("post_data.state_score")).alias("stateImp"),
        (col("post_data.nationalImportanceLevel") | col("post_data.national_score")).alias("natImp"),
        (col("post_data.isLocal") | col("post_data.is_local")).alias("isLocal"),
        (col("post_data.isReporter") | col("post_data.is_reporter")).alias("isReporter"),
        (col("post_data.contentLang") | col("post_data.lang")).alias("postLang"),
        (col("post_data.creatorID") | col("post_data.creator_pid") | col("post_data.creatorId")).alias("creatorID"),
        (col("post_data.location.pid") | col("post_data.district_pid") | col("post_data.location_pid")).alias("location_pid")
    )
    .filter(col("post_id").isNotNull())
    .dropDuplicates(["post_id"]))

print(f"Loaded {Post.count()} posts from classification dumps")

# Load district->state mapping from S3 JSON
print("Loading district->state mapping from S3...")
district_state_json = spark.read.option("multiLine", True).json(f"s3://{BUCKET}/config/district_state_mapping.json")

# Transform JSON structure: {district_pid: {state_name: "X", state_pid: "Y"}}
# to DataFrame: district_pid, state_name
district_state_map = (district_state_json
    .select(col("_1").alias("district_pid"), col("_2.state_name").alias("state_name")))

print(f"Loaded {district_state_map.count()} district mappings")

# ============================================================
# READ APPEVENTS (Glue Catalog / S3)
# ============================================================

print("Reading appevents from Glue Catalog...")

# Parse JSON from appevents data column
appevents_schema = StructType([
    StructField("postId", StringType(), True),
    StructField("isLocked", StringType(), True),  # "true"/"false" string
    StructField("userAge", IntegerType(), True),
    StructField("isInitiatedFromPost", StringType(), True)
])

# feed_locked (last 14 days)
locks_raw = (spark.read.table("closeapp.appevents")
    .filter((col("event_name") == "feed_locked") &
            (col("date").between((ref_date - timedelta(14)).strftime("%Y-%m-%d"), RUN_DATE)))
    .withColumn("event_data", from_json(col("data"), appevents_schema)))

locks = (locks_raw
    .filter(col("event_data.isLocked") == "true")
    .select(
        col("user_id"),
        col("event_time"),
        col("event_data.postId").alias("post_id"),
        col("event_data.userAge").alias("userAge")
    ))

print(f"Loaded {locks.count()} lock events")

# initiate_transaction (last 14 days, post-attributed only)
initiates_raw = (spark.read.table("closeapp.appevents")
    .filter((col("event_name") == "initiate_transaction") &
            (col("date").between((ref_date - timedelta(14)).strftime("%Y-%m-%d"), RUN_DATE)))
    .withColumn("event_data", from_json(col("data"), appevents_schema)))

initiates = (initiates_raw
    .filter(col("event_data.isInitiatedFromPost") == "true")
    .select(
        col("user_id"),
        col("event_time"),
        col("event_data.postId").alias("post_id")
    ))

print(f"Loaded {initiates.count()} initiate events")

# ============================================================
# 1. ECO AGGREGATES (7d)
# ============================================================

print("Computing ecosystem aggregates...")

Post_7d = Post.filter(col("created_ms") >= window_start)

# Join posts with district->state mapping
Post_7d_with_geo = Post_7d.join(district_state_map, Post_7d.location_pid == district_state_map.district_pid, "left")

# District-level aggregates
eco_district = (Post_7d_with_geo
    .groupBy(col("location_pid").alias("district_pid"))
    .agg(
        count("*").alias("district_posts_7d"),
        countDistinct(when(col("isReporter"), col("creatorID"))).alias("district_reporters_7d")
    ))

# State-level aggregates
eco_state = (Post_7d_with_geo
    .filter(col("state_name").isNotNull())
    .groupBy("state_name")
    .agg(
        count("*").alias("state_posts_7d"),
        countDistinct(when(col("isReporter"), col("creatorID"))).alias("state_reporters_7d")
    ))

print(f"State aggregates: {eco_state.count()} states")

# Language-level aggregates
eco_lang = (Post_7d
    .groupBy(col("postLang"))
    .agg(
        count("*").alias("lang_posts_7d"),
        countDistinct(when(col("isReporter"), col("creatorID"))).alias("lang_reporters_7d")
    ))

# district_users_7d (distinct users who locked a post in this district over 7d)
locks_7d = locks.filter(col("event_time") >= window_start)
eco_users = (locks_7d.join(Post_7d_with_geo.select("post_id", "location_pid"), "post_id", "left")
    .groupBy(col("location_pid").alias("district_pid"))
    .agg(countDistinct("user_id").alias("district_users_7d")))

# Combine district-level with users
eco_district_full = eco_district.join(eco_users, "district_pid", "outer").fillna(0)

# Write eco aggregates
eco_district_full.write.mode("overwrite").parquet(f"s3://{BUCKET}/features/eco/district/date={RUN_DATE}/")
eco_state.write.mode("overwrite").parquet(f"s3://{BUCKET}/features/eco/state/date={RUN_DATE}/")
eco_lang.write.mode("overwrite").parquet(f"s3://{BUCKET}/features/eco/lang/date={RUN_DATE}/")

print(f"Eco aggregates written: {eco_district_full.count()} districts, {eco_state.count()} states, {eco_lang.count()} languages")

# ============================================================
# 2. CREATOR AGGREGATES - NO MONGODB!
# ============================================================

print("Computing creator aggregates...")

# creator_prior_rate: (initiates / locks) over PRIOR window (day -14 to -7), by creator
Post_prior = Post.filter((col("created_ms") >= prior_window_start) & (col("created_ms") < prior_window_end))
locks_prior = locks.filter((col("event_time") >= prior_window_start) & (col("event_time") < prior_window_end))
initiates_prior = initiates.filter((col("event_time") >= prior_window_start) & (col("event_time") < prior_window_end))

creator_locks = (locks_prior.join(Post_prior.select("post_id", "creatorID"), "post_id")
    .groupBy("creatorID")
    .agg(count("*").alias("locks")))

creator_initiates = (initiates_prior.join(Post_prior.select("post_id", "creatorID"), "post_id")
    .groupBy("creatorID")
    .agg(count("*").alias("initiates")))

creator_rate = (creator_locks.join(creator_initiates, "creatorID", "left")
    .fillna(0, subset=["initiates"])
    .withColumn("creator_prior_rate", col("initiates") / col("locks"))
    .withColumn("creator_has_prior", when(col("locks") > 0, 1).otherwise(0))
    .select("creatorID", "creator_prior_rate", "creator_has_prior"))

# creator_viewcount: Compute from ALL locks (not just prior window)
print("Computing creator_viewcount from appevents...")
creator_viewcount = (locks
    .join(Post.select("post_id", "creatorID"), "post_id")
    .groupBy("creatorID")
    .agg(count("*").alias("creator_viewcount")))

# creator_verified: Infer from isReporter posts
print("Inferring creator_verified from isReporter status...")
creator_verified_ids = (Post
    .filter(col("isReporter") == True)
    .select("creatorID")
    .distinct()
    .withColumn("creator_verified", lit(True)))

# Combine all creator features
creator = (creator_rate
    .join(creator_viewcount, "creatorID", "left")
    .join(creator_verified_ids, "creatorID", "left")
    .fillna({"creator_viewcount": 0, "creator_verified": False}))

creator.write.mode("overwrite").parquet(f"s3://{BUCKET}/features/creator/date={RUN_DATE}/")

print(f"Creator aggregates written: {creator.count()} creators")

spark.stop()
print(f"✅ DONE {RUN_DATE}: eco (district/state/lang), creator written to s3://{BUCKET}/")
print("🎉 NO MONGODB USED - All data from S3 + appevents!")