# Label Storage - Training Labels (Y)

**Status**: ✅ **UPDATED** - Labels now stored persistently in S3

---

## 📊 What Are Labels?

**Labels (Y)** = User conversion outcome after viewing post

**3 classes (ordinal)**:
- **0** = User viewed post but did NOT initiate transaction
- **1** = User initiated transaction but did NOT start trial
- **2** = User initiated AND started trial (full conversion)

---

## 🗄️ Storage Location

### S3 Path:
```
s3://nearme-feed-store/features/labels/
  date=2026-06-21/
    - part-001.snappy.parquet
    - part-002.snappy.parquet
  date=2026-06-22/
    - part-001.snappy.parquet
```

### Athena Table:
```sql
Database: closeapp
Table: training_labels

Schema:
  user_id: STRING
  post_id: STRING
  run_ts: BIGINT (snapshot timestamp used for features)
  view_time: BIGINT (when user viewed post)
  y: INT (0/1/2 - the label)
  date: STRING (partition key - date of user view)
```

---

## 🔄 Label Generation Pipeline

### Step 1: Setup (Run Once)
```bash
python create_labels.py \
  --create-tables \
  --start-date 2026-06-14 \
  --end-date 2026-06-21
```

This creates:
- `training_labels` table in Athena (persistent storage)
- `dynamic_snapshots` table (external table over Lambda snapshots)

### Step 2: Generate Labels (Run Daily)
```bash
# Wait 7 days after users viewed posts (for conversion window)
python create_labels.py \
  --start-date 2026-06-21 \
  --end-date 2026-06-21
```

This:
1. Finds all day-0 users who viewed posts on 2026-06-21
2. Looks at their conversion events in next 7 days
3. Assigns label: 0 (no conversion), 1 (initiated), 2 (trial)
4. Stores to `s3://nearme-feed-store/features/labels/date=2026-06-21/`

---

## 📅 Timeline Example

```
Day 0 (June 21):
  - User views post at 10:00 AM
  - Lambda captures snapshot (features) at 10:00 AM
  - We DON'T know label yet (need to wait for conversions)

Days 1-7 (June 22-28):
  - User may or may not convert
  - June 22 11:00 AM: User initiates transaction ✅
  - June 23: No trial started

Day 8 (June 29):
  - Conversion window closed (7 days passed)
  - Run create_labels.py for June 21
  - Label = 1 (initiated but no trial)
  - Store to s3://.../labels/date=2026-06-21/

Day 8+:
  - Label available for training
  - Join with features from June 21 snapshot
  - Train model
```

---

## 🔍 How Labels Are Computed

### Input: Raw Events
```
Event 1: feed_locked (user viewed post)
  user_id: user123
  post_id: post456
  event_time: 1718900000000 (June 21, 10:00 AM)
  userAge: 0

Event 2: initiate_transaction (16 minutes later)
  user_id: user123
  event_time: 1718900960000 (June 21, 10:16 AM)
  isInitiatedFromPost: true
  origin: "post_post456"

Event 3: subscription_trial_started (not found)
```

### Logic:
```python
view_time = Event1.event_time  # June 21, 10:00 AM
conversion_window = [view_time, view_time + 7 days]

initiated = check_if_initiated(user, post, after=view_time, within=7days)
trialed = check_if_trialed(user, after=view_time, within=7days)

if trialed:
    y = 2
elif initiated:
    y = 1
else:
    y = 0
```

### Result:
```
user_id  | post_id  | view_time      | y
---------|----------|----------------|---
user123  | post456  | 1718900000000  | 1
```

---

## 🎯 Data Leakage Prevention

### Critical Rule: Features from BEFORE user view, Labels from AFTER

```
Time T = view_time (when user saw post)
         |
         v
[Snapshot at T-15min] → Features (X) - ALL 57 variables
         |
         | User views post
         |
[Events after T] → Label (Y) - 0/1/2
```

**Why snapshot at T-15min?**
- Lambda creates snapshots every 15 minutes
- Use snapshot from BEFORE user viewed post
- Ensures features don't include current user's interaction ✅

---

## 📊 Label Distribution Analysis

### Check label balance:
```sql
SELECT 
    date,
    y,
    COUNT(*) as count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY date), 2) as pct
FROM closeapp.training_labels
GROUP BY date, y
ORDER BY date, y;
```

**Expected distribution** (for day-0 users):
```
y=0 (no conversion): ~90-95%
y=1 (initiated): ~3-5%
y=2 (trial): ~1-2%
```

⚠️ **Imbalanced classes** - will need:
- Class weights in model
- Stratified sampling
- Or focal loss

---

## 🔗 Join with Features

### Training Data Structure:
```
Labels (Y):                     Features (X):
- user_id                       - 57 variables from snapshot
- post_id                       - Captured at run_ts (before view)
- run_ts (timestamp)    JOIN    - All features at time T
- view_time
- y (0/1/2)
```

### Join Query:
```sql
SELECT
    l.user_id,
    l.post_id,
    l.y,  -- Label
    -- Features from Lambda snapshot
    d.views,
    d.shareCount,
    d.topic,
    ...  -- All 57 features
FROM training_labels l
JOIN dynamic_snapshots d
    ON l.post_id = d.post_id
    AND l.run_ts = d.run_ts
```

---

## 💾 Storage Details

### Format: Parquet (Snappy compression)
- Efficient columnar storage
- Fast querying
- Small file size

### Partitioning: By date
- Easy to add new labels daily
- Query only relevant dates
- Efficient for incremental training

### Size Estimate:
```
Per training instance:
  user_id: 36 bytes (UUID)
  post_id: 36 bytes (UUID)
  run_ts: 8 bytes (BIGINT)
  view_time: 8 bytes (BIGINT)
  y: 4 bytes (INT)
  Total: ~92 bytes/row

Daily volume (example):
  10,000 day-0 users × 5 posts/user = 50,000 rows
  50,000 × 92 bytes = 4.6 MB/day (uncompressed)
  ~1-2 MB/day (with Parquet compression)

Monthly: ~30-60 MB
```

---

## 🚀 Daily Workflow

### Day 0-6: Collection
```
Lambda runs every 15 min → Captures features
Users view posts → Potentially convert
```

### Day 7: Label Generation
```bash
# Example: Generate labels for June 21 (run on June 29)
python create_labels.py \
  --start-date 2026-06-21 \
  --end-date 2026-06-21
```

### Day 8+: Training
```bash
# Build complete training dataset
python build_training_data.py \
  --start-date 2026-06-14 \
  --end-date 2026-06-21

# Train model
python train_model.py --input s3://.../training_data.parquet
```

---

## ✅ Benefits of Persistent Storage

1. **Compute once, use many times**
   - Multiple training runs
   - Hyperparameter tuning
   - A/B testing different models

2. **Fast training iterations**
   - Labels pre-computed
   - Just read from S3 and train

3. **Easy debugging**
   - Inspect label distribution
   - Check class balance
   - Validate data quality

4. **Incremental growth**
   - Add new labels daily
   - Accumulate training data over time

5. **Consistent with features**
   - Lambda → features
   - EMR → aggregated features
   - create_labels.py → labels
   - All in S3, all partitioned by date

---

## 🎯 Summary

**Labels (Y)** are now stored persistently:

**Location**: `s3://nearme-feed-store/features/labels/`  
**Format**: Parquet (partitioned by date)  
**Schema**: user_id, post_id, run_ts, view_time, y  
**Generation**: Run `create_labels.py` 7 days after user views  
**Usage**: Join with features to create training dataset

**Complete Feature Pipeline**:
1. Lambda (15-min) → `s3://.../features/snapshots/` (35 vars)
2. EMR (daily) → `s3://.../features/eco/`, `features/creator/` (11 vars)
3. **create_labels.py (7-day lag) → `s3://.../features/labels/` (Y labels)** ✅
4. build_training_data.py → Join all → Complete training dataset

All 57 features + 1 label = **Ready to train!** 🎉