# Daily Workflow - Same-Day Conversion Focus

**Updated**: 2026-06-22  
**Key Change**: Labels generated NEXT DAY (not 7-day lag)

---

## 🎯 Core Insight

**Most trials start on Day 0** → Focus on same-day conversions!

---

## ⏰ Daily Timeline

### Day 0 (e.g., June 21)

**Throughout the day:**
```
00:00-23:59
  - Lambda runs every 15 min → Captures features
  - Day-0 users view posts
  - Users convert SAME DAY (within hours)
```

**Example user journey:**
```
10:00 AM - User signs up (Day-0)
10:05 AM - Views post in feed
10:20 AM - Initiates transaction (15 min later)
10:35 AM - Starts trial (30 min later)

Result: y = 2 (full conversion, same day)
```

---

### Day 1 (e.g., June 22)

**2:00 AM UTC - Generate labels for previous day**
```bash
# Automated daily cron job
python create_labels.py \
  --start-date 2026-06-21 \
  --end-date 2026-06-21
```

**What happens:**
1. Query all Day-0 user views from June 21
2. Check if they converted SAME DAY (June 21)
3. Assign label: 0/1/2
4. Store to `s3://.../features/labels/date=2026-06-21/`

**Morning (6:00 AM onwards) - Labels ready!**
```
✅ Labels for June 21 available
✅ Features for June 21 already available (Lambda captured them)
✅ EMR aggregates available (ran daily at 2 AM)

→ Can train model with June 21 data!
```

---

## 📊 Complete Daily Pipeline

### Every 15 minutes (24/7)
```
Lambda: feed-ranking-snapshot
  ↓
Captures 35 features per post
  ↓
s3://.../features/snapshots/ts=<timestamp>/
```

### Daily 2:00 AM UTC
```
┌─────────────────────────────────┐
│ EMR: daily aggregates           │
│   - Ecosystem features (7)      │
│   - Creator features (4)        │
│   - Output: s3://.../features/  │
└─────────────────────────────────┘
         │
         ├─ Completes ~2:30 AM
         │
         v
┌─────────────────────────────────┐
│ create_labels.py (Day-1 data)   │
│   - Day-0 user views            │
│   - Same-day conversions        │
│   - Output: s3://.../labels/    │
└─────────────────────────────────┘
         │
         ├─ Completes ~3:00 AM
         │
         v
    Labels ready for training!
```

### Daily 6:00 AM onwards (optional)
```
Train model with PREVIOUS day's data
  ↓
Evaluate metrics
  ↓
Deploy if better than baseline
```

---

## 🔢 Label Distribution (Expected)

For Day-0 users on same-day conversions:

```
y=0 (no conversion):  85-90%  (most users don't convert same day)
y=1 (initiated):       5-10%  (initiated but didn't trial yet)
y=2 (trial):           3-5%   (full conversion same day)
```

**Check actual distribution:**
```sql
SELECT 
    date,
    y,
    COUNT(*) as count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY date), 2) as pct
FROM closeapp.training_labels
WHERE date = '2026-06-21'
GROUP BY date, y
ORDER BY y;
```

---

## 📅 Timeline to First Model

### Current Status: June 22, 2026

```
June 22 (Today):
  ✅ Lambda running (capturing features)
  ✅ EMR running (daily aggregates)
  ⏳ Accumulating data

June 23 (Tomorrow morning):
  ✅ Labels for June 22 ready at 3 AM
  ▶️ Can train FIRST MODEL!
  
  Steps:
    1. Run create_labels.py for June 22 (automated)
    2. Run build_training_data.py
    3. Train XGBoost/LightGBM model
    4. Evaluate metrics
```

**Time to first model: ~1 DAY** (not 7+ days!)

---

## 🤖 Automated Cron Jobs

### Setup cron (on EC2 or Lambda)

**Daily label generation (2:30 AM UTC):**
```bash
30 2 * * * python /path/to/create_labels.py \
  --start-date $(date -d 'yesterday' +\%Y-\%m-\%d) \
  --end-date $(date -d 'yesterday' +\%Y-\%m-\%d) \
  >> /var/log/label-generation.log 2>&1
```

**Daily training (6:00 AM UTC, optional):**
```bash
0 6 * * * python /path/to/build_training_data.py \
  --start-date $(date -d '7 days ago' +\%Y-\%m-\%d) \
  --end-date $(date -d 'yesterday' +\%Y-\%m-\%d) \
  >> /var/log/training.log 2>&1
```

---

## 📈 Training Strategy

### Option 1: Daily Retraining
```bash
# Train with last 7 days of data
python train_model.py \
  --start-date $(date -d '7 days ago' +%Y-%m-%d) \
  --end-date $(date -d 'yesterday' +%Y-%m-%d)
```

**Pros**: Always fresh model  
**Cons**: Expensive (train daily)

---

### Option 2: Weekly Retraining
```bash
# Every Monday, train with last 30 days
python train_model.py \
  --start-date $(date -d '30 days ago' +%Y-%m-%d) \
  --end-date $(date -d 'yesterday' +%Y-%m-%d)
```

**Pros**: More training data, stable model  
**Cons**: Model slightly stale

---

### Option 3: Incremental Training (Recommended)
```bash
# Train initial model with 30 days
python train_model.py --start-date 2026-06-01 --end-date 2026-06-30

# Daily: Update with new day's data (online learning)
python update_model.py --date $(date -d 'yesterday' +%Y-%m-%d)
```

**Pros**: Best of both (fresh + stable)  
**Cons**: More complex implementation

---

## 🎯 Key Differences from 7-Day Approach

| Aspect | 7-Day Lag | Same-Day (Current) |
|--------|-----------|-------------------|
| Conversion window | 7 days | Same day (hours) |
| Label generation | Weekly | **Daily** ✅ |
| Time to train | 7+ days | **1 day** ✅ |
| Conversion rate | Higher (~10-15%) | Lower (~5-10%) |
| Use case | Long-term LTV | **Immediate engagement** ✅ |
| Iteration speed | Slow | **Fast** ✅ |

---

## 📊 Example: 3-Day Data Snapshot

### June 21 Labels (generated June 22):
```
Total views: 50,000
y=0: 43,500 (87%)
y=1: 4,500 (9%)
y=2: 2,000 (4%)
```

### June 22 Labels (generated June 23):
```
Total views: 52,000
y=0: 45,500 (87.5%)
y=1: 4,300 (8.3%)
y=2: 2,200 (4.2%)
```

### June 23 Labels (generated June 24):
```
Total views: 48,000
y=0: 42,000 (87.5%)
y=1: 4,000 (8.3%)
y=2: 2,000 (4.2%)
```

**Total training data after 3 days**: 150,000 instances ✅

---

## ✅ Recommended Daily Schedule

```
2:00 AM - EMR daily aggregates start
2:30 AM - EMR completes, create_labels.py starts
3:00 AM - Labels ready for previous day
6:00 AM - (Optional) Train model with last 7 days
8:00 AM - (Optional) Deploy new model
         - Monitor metrics
         - A/B test vs baseline
```

---

## 🔍 Validation Queries

### Check today's label generation:
```sql
-- Did labels generate for yesterday?
SELECT 
    date,
    COUNT(*) as total_instances,
    COUNT(DISTINCT user_id) as unique_users,
    COUNT(DISTINCT post_id) as unique_posts,
    SUM(CASE WHEN y=0 THEN 1 ELSE 0 END) as no_conversion,
    SUM(CASE WHEN y=1 THEN 1 ELSE 0 END) as initiated,
    SUM(CASE WHEN y=2 THEN 1 ELSE 0 END) as trialed
FROM closeapp.training_labels
WHERE date = date_format(current_date - interval '1' day, '%Y-%m-%d')
GROUP BY date;
```

### Check label freshness:
```sql
-- What's the most recent date with labels?
SELECT MAX(date) as latest_label_date
FROM closeapp.training_labels;

-- Expected: Yesterday's date
```

---

## 🎉 Summary

**Old approach**: Wait 7 days → Generate labels → Train  
**New approach**: Wait 1 day → Generate labels → Train ✅

**Benefits**:
- ✅ **Fast iteration**: Train NEXT DAY, not after 7 days
- ✅ **Same-day conversions**: Aligns with user behavior (most trials on Day 0)
- ✅ **Daily labels**: Automated pipeline, continuous training data
- ✅ **Quick feedback**: Test model changes rapidly

**Timeline**:
- June 22 (today): Data collection
- June 23 (tomorrow): Labels ready, **TRAIN FIRST MODEL!** 🚀

---

**Next step: Run `create_labels.py` tomorrow morning for June 22 data!**