"""
Daily feed aggregates — EMR Serverless PySpark.

Reads mongo-prod (Post, User, Place) + appevents from Glue Catalog, computes:
  - features/eco: district/state/lang × posts/reporters/users over 7d
  - features/creator: creator_prior_rate (conv/lock), verified, viewcount

Writes parquet to S3.

Note: Static post meta (topic/importance/etc.) is now captured by the 15-min Lambda
(in dynamic/) because those fields arrive async after post creation — daily would miss
fresh posts.

UPDATES:
- Added Place tree walk for district->state resolution
- Implemented state-level ecosystem aggregates
- Added proper null handling for location resolution
"""
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, count, countDistinct, lit, sum as sql_sum, when

IST = ZoneInfo("Asia/Kolkata")
BUCKET = sys.argv[1] if len(sys.argv) > 1 else "nearme-feed-store"
RUN_DATE = sys.argv[2] if len(sys.argv) > 2 else datetime.now(IST).strftime("%Y-%m-%d")
MONGO_URI = sys.argv[3] if len(sys.argv) > 3 else "mongodb://mongo-prod:27017"

spark = (SparkSession.builder
    .appName("feed-daily-aggregates")
    .config("spark.mongodb.read.connection.uri", MONGO_URI)
    .config("spark.mongodb.read.database", "nearme")
    .getOrCreate())

ref_date = datetime.strptime(RUN_DATE, "%Y-%m-%d").replace(tzinfo=IST)
window_start = int((ref_date - timedelta(days=7)).timestamp() * 1000)
prior_window_start = int((ref_date - timedelta(days=14)).timestamp() * 1000)
prior_window_end = int((ref_date - timedelta(days=7)).timestamp() * 1000)

# ============================================================
# READ MONGO
# ============================================================

# Post (last 14 days, for prior + current windows)
Post = (spark.read.format("mongodb")
    .option("collection", "Post")
    .option("pipeline", f'[{{"$match": {{"c": {{"$gte": {prior_window_start}}}}}}}]')
    .load()
    .select(
        col("id").alias("post_id"),
        col("c").alias("created_ms"),
        col("topic"), col("subTopic"),
        col("districtImportanceLevel").alias("distImp"),
        col("stateImportanceLevel").alias("stateImportanceLevel"),
        col("nationalImportanceLevel").alias("natImp"),
        col("isLocal"), col("isReporter"),
        col("containsVisualMedia").alias("media"),
        col("duration").alias("media_duration"),
        col("titleLength").alias("titleLen"),
        col("contentLang").alias("postLang"),
        col("creatorID").alias("creatorID"),
        col("location.pid").alias("location_pid")
    ))

# User
User = (spark.read.format("mongodb")
    .option("collection", "User")
    .load()
    .select(
        col("id").alias("user_id"),
        col("verified"),
        col("viewCount").alias("creator_viewcount")
    ))

# Place (for district/state resolution)
Place = (spark.read.format("mongodb")
    .option("collection", "PlaceWithLatLngV3")
    .load()
    .select(
        col("id").alias("pid"),
        col("ty").alias("place_type"),
        col("name").alias("place_name"),
        col("parentId")
    ))

# ============================================================
# BUILD DISTRICT -> STATE MAPPING
# ============================================================

# Get all districts with their parent state IDs
districts = (Place
    .filter(col("place_type") == "DISTRICT")
    .select(
        col("pid").alias("district_pid"),
        col("parentId").alias("state_pid")
    ))

# Get all states
states = (Place
    .filter(col("place_type") == "STATE")
    .select(
        col("pid").alias("state_pid"),
        col("place_name").alias("state_name")
    ))

# Join to create district->state mapping
district_state_map = districts.join(states, "state_pid", "left")

print(f"District->State mapping: {district_state_map.count()} districts")

# ============================================================
# READ APPEVENTS (Glue Catalog / S3)
# ============================================================

# feed_locked (last 14 days)
locks = (spark.read.table("closeapp.appevents")
    .filter((col("event_name") == "feed_locked") & (col("date").between((ref_date - timedelta(14)).strftime("%Y-%m-%d"), RUN_DATE)))
    .select(
        col("user_id"),
        col("event_time"),
        col("postId").alias("post_id"),
        col("isLocked"),
        col("userAge")  # for day-0 filter later
    )
    .filter(col("isLocked") == True))

# initiate_transaction (last 14 days, post-attributed only)
initiates = (spark.read.table("closeapp.appevents")
    .filter((col("event_name") == "initiate_transaction") & (col("date").between((ref_date - timedelta(14)).strftime("%Y-%m-%d"), RUN_DATE)))
    .filter(col("isInitiatedFromPost") == True)
    .select(
        col("user_id"),
        col("event_time"),
        col("origin").alias("origin_str")  # "post_<postId>"
    )
    .withColumn("post_id", col("origin_str").substr(6, 100)))  # extract postId from "post_..."

# ============================================================
# 1. ECO AGGREGATES (7d)
# ============================================================

Post_7d = Post.filter(col("created_ms") >= window_start)

# Join posts with district->state mapping
Post_7d_with_geo = Post_7d.join(
    district_state_map.select("district_pid", "state_name"),
    Post_7d.location_pid == district_state_map.district_pid,
    "left"
)

# District-level aggregates
eco_district = (Post_7d_with_geo
    .groupBy(col("location_pid").alias("district_pid"))
    .agg(
        count("*").alias("district_posts_7d"),
        countDistinct(when(col("isReporter"), col("creatorID"))).alias("district_reporters_7d")
    ))

# State-level aggregates (now implemented!)
eco_state = (Post_7d_with_geo
    .filter(col("state_name").isNotNull())  # Only posts with resolved state
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

# district_users_7d (distinct day-0 users who locked a post in this district over 7d)
# join locks (7d) with Post to get district, count distinct users
locks_7d = locks.filter(col("event_time") >= window_start)
eco_users = (locks_7d.join(Post_7d_with_geo.select("post_id", "location_pid"), "post_id", "left")
    .groupBy(col("location_pid").alias("district_pid"))
    .agg(countDistinct("user_id").alias("district_users_7d")))

# Combine district-level with users
eco_district_full = eco_district.join(eco_users, "district_pid", "outer")

# Write eco aggregates (district, state, lang as separate outputs)
eco_district_full.write.mode("overwrite").parquet(f"s3://{BUCKET}/features/eco/district/date={RUN_DATE}/")
eco_state.write.mode("overwrite").parquet(f"s3://{BUCKET}/features/eco/state/date={RUN_DATE}/")
eco_lang.write.mode("overwrite").parquet(f"s3://{BUCKET}/features/eco/lang/date={RUN_DATE}/")

print(f"Eco aggregates written: {eco_district_full.count()} districts, {eco_state.count()} states, {eco_lang.count()} languages")

# ============================================================
# 2. CREATOR AGGREGATES
# ============================================================

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

# join User for verified/viewcount
creator = creator_rate.join(User.withColumnRenamed("user_id", "creatorID"), "creatorID", "left")

creator.write.mode("overwrite").parquet(f"s3://{BUCKET}/features/creator/date={RUN_DATE}/")

print(f"Creator aggregates written: {creator.count()} creators")

spark.stop()
print(f"DONE {RUN_DATE}: eco (district/state/lang), creator written to s3://{BUCKET}/")