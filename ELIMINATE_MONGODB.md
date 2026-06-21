# Can We Eliminate User Collection from EMR Job?

## 🎯 What User Collection Provides

**Only 2 variables**:
1. `creator_verified` - Boolean: Is creator verified?
2. `creator_viewcount` - Integer: Total views for this creator

**Used in**: Creator features (4 variables total)
- creator_prior_rate (computed from appevents) ✅
- creator_has_prior (computed from appevents) ✅
- creator_verified (from User collection) ⚠️
- creator_viewcount (from User collection) ⚠️

---

## ✅ OPTION 1: Compute viewCount from appevents (RECOMMENDED)

### Can We Calculate viewCount?

**YES! From appevents feed_locked events**

```sql
-- Creator viewcount = Total locks across all their posts
SELECT 
    p.creatorID,
    COUNT(*) as creator_viewcount
FROM closeapp.appevents e
JOIN posts p ON e.postId = p.id
WHERE e.event_name = 'feed_locked'
  AND e.isLocked = true
GROUP BY p.creatorID
```

**In Spark**:
```python
# Join locks with Post (from classification dumps) to get creatorID
creator_viewcount = (locks
    .join(Post.select("post_id", "creatorID"), "post_id")
    .groupBy("creatorID")
    .agg(count("*").alias("creator_viewcount")))
```

**Pros**:
- ✅ No MongoDB needed
- ✅ Computed from actual view events (more accurate)
- ✅ Uses same data source as other features

**Cons**:
- ⚠️ Might differ slightly from MongoDB viewCount
- ⚠️ Only counts views in appevents (not historical)

**Verdict**: ✅ **EASY TO IMPLEMENT**

---

## ✅ OPTION 2: Skip creator_verified (SET TO FALSE)

### Do We Actually Need Verified Status?

**What is it?**: Boolean indicating if creator is verified/official

**Usage in Model**: Feature indicating trustworthiness/authority

**Alternatives**:
1. **Set all to FALSE** (assume no one verified)
2. **Infer from isReporter** (reporters are semi-verified)
3. **Skip this variable** (train without it)

**Impact Analysis**:
- Only 1 of 57 variables (1.7%)
- Probably low importance (most creators not verified)
- Model can work without it

**Verdict**: ✅ **LOW IMPACT - Can skip or set to FALSE**

---

## ✅ OPTION 3: Get verified from Classification Dumps

### Check if Classification System Knows Verified Status

**Possibility**: Classification dumps might include creator metadata

**Need to verify**: Parse actual classification dump JSON to see if it has:
- `creator.verified`
- `creatorVerified`
- `isVerifiedCreator`

**How to check**:
```python
# Read classification dump
data = json.loads(row['data'])
print(data.keys())  # Check all available fields
```

**If exists**: ✅ Use from classification dumps  
**If not**: ⚠️ Use Option 1 or 2

---

## 🎯 RECOMMENDED APPROACH: Eliminate User Collection Completely

### Implementation Plan:

#### 1. Replace creator_viewcount with Computed Value
```python
# OLD (from MongoDB User):
creator_viewcount = User.viewCount

# NEW (compute from appevents):
creator_viewcount = (locks_all
    .join(Post.select("post_id", "creatorID"), "post_id")
    .groupBy("creatorID")
    .agg(count("*").alias("creator_viewcount")))
```

#### 2. Handle creator_verified
**Option A**: Set to FALSE for all creators
```python
creator_verified = lit(False)
```

**Option B**: Infer from isReporter
```python
# If creator has isReporter posts, mark as verified
creator_verified = (Post
    .filter(col("isReporter") == True)
    .select("creatorID")
    .distinct()
    .withColumn("creator_verified", lit(True)))
```

**Option C**: Try to find in classification dumps
```python
# Parse from classification data if available
creator_verified = post_data.creator_verified
```

---

## 📊 Comparison: With vs Without MongoDB User

### With MongoDB User (Current):
```
Data Sources:
- MongoDB Post → Replace with classification dumps ✅
- MongoDB User → Used for 2 variables ⚠️
- MongoDB Place → Replace with S3 JSON ✅
- Appevents → Already using ✅

Variables: 11/11 ✅
MongoDB dependency: YES ❌
```

### Without MongoDB User (Proposed):
```
Data Sources:
- Classification dumps (S3) → Post data ✅
- S3 JSON → Place mapping ✅
- Appevents → All engagement/conversions ✅

Variables: 
- creator_viewcount: Computed from appevents ✅
- creator_verified: Set to FALSE or infer from isReporter ✅

Total: 11/11 ✅
MongoDB dependency: NO ✅
```

---

## ✅ Benefits of Eliminating User Collection

1. **No MongoDB at all**: Complete elimination
2. **Simpler deployment**: No VPC, no connection strings
3. **Faster execution**: S3 + Spark optimized
4. **More accurate viewcount**: From actual events, not cached count
5. **Consistent data sources**: Everything from S3/appevents

---

## ⚠️ Potential Issues

### 1. ViewCount Might Differ
- **MongoDB User.viewCount**: Cumulative, all-time views
- **Computed from appevents**: Only views in appevents table (might have retention)

**Solution**: Document the difference, or add "all-time" vs "recent" clarification

### 2. Verified Status Unknown
- **MongoDB User.verified**: True/False based on admin verification
- **Inferred/Assumed**: Might be inaccurate

**Solution**: 
- Acceptable loss (1 variable of 57)
- Or infer from isReporter (good proxy)
- Or check if in classification dumps

---

## 🚀 Implementation Steps

### Step 1: Update daily_job.py to Remove MongoDB User

```python
# REMOVE:
User = (spark.read.format("mongodb")
    .option("collection", "User")
    .load()
    .select(col("id").alias("user_id"), col("verified"), col("viewCount")))

# ADD:
# Compute creator_viewcount from appevents
creator_viewcount = (locks
    .join(Post.select("post_id", "creatorID"), "post_id")
    .groupBy("creatorID")
    .agg(count("*").alias("creator_viewcount")))

# Infer creator_verified from isReporter
creator_verified_ids = (Post
    .filter(col("isReporter") == True)
    .select("creatorID")
    .distinct()
    .withColumn("creator_verified", lit(True)))
```

### Step 2: Update Join Logic

```python
# OLD:
creator = creator_rate.join(User.withColumnRenamed("user_id", "creatorID"), "creatorID", "left")

# NEW:
creator = (creator_rate
    .join(creator_viewcount, "creatorID", "left")
    .join(creator_verified_ids, "creatorID", "left")
    .fillna({"creator_viewcount": 0, "creator_verified": False}))
```

### Step 3: Test with Sample Data

```bash
# Run EMR job with new logic
# Verify output has all 4 creator variables:
# - creator_prior_rate ✅
# - creator_has_prior ✅
# - creator_viewcount ✅ (computed)
# - creator_verified ✅ (inferred)
```

---

## 📋 Final Recommendation

### ✅ ELIMINATE User Collection Completely

**Why**:
1. Only 2 variables (3.5% of total)
2. Both can be computed/inferred from other sources
3. Removes ALL MongoDB dependency
4. Simpler, faster, more maintainable

**How**:
1. Compute `creator_viewcount` from appevents locks
2. Infer `creator_verified` from isReporter posts
3. Remove MongoDB User connection entirely

**Impact**:
- Variables: 11/11 still available ✅
- MongoDB: Completely eliminated ✅
- Deployment: Much simpler (no VPC, no DB connection) ✅
- Performance: Faster (S3 + Spark optimized) ✅

---

## 🎯 Summary

**Question**: Why do we need MongoDB User collection?

**Answer**: We DON'T!

- `creator_viewcount` → Compute from appevents ✅
- `creator_verified` → Infer from isReporter or set to FALSE ✅

**Result**: ✅ **ZERO MongoDB dependency for entire EMR job**

**Next Step**: Update daily_job.py to eliminate ALL MongoDB collections and use only S3 + appevents.