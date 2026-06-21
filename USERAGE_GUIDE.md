.# UserAge Extraction - How It Works

## 📍 Where is UserAge?

### Data Structure:
```
appevents table
├── event_id (string)
├── event_time (bigint)
├── data (string) ← JSON STRING containing userAge!
├── platform (string)
├── user_id (string)
├── event_name (string)
└── date (string)
```

### Sample Event Data:
```json
{
  "latency": 68,
  "localitySelectionLevel": "SUB_DISTRICT",
  "category": "all",
  "status": "success",
  "feedMode": "NORMAL",
  "userAge": 438,  ← HERE! User age in days
  "experimentName": "EXP_RECOMMENDATION_NOT_SHOWN",
  "sessionId": "8b84a24a-a3c7-49cb-aa18-dbfe72c8f4e3",
  "districtId": "e88efb76-b1d9-11ed-823e-acde48001122",
  "stateId": "bb377d6e-0d74-485a-86e0-0af889f79cb9"
}
```

## 🎯 What is UserAge?

**Definition**: Number of days since user first opened the app

**Examples**:
- `userAge = 0` → New user (first day) - **TARGET FOR TRAINING**
- `userAge = 1` → User came back on day 2
- `userAge = 438` → User has been using app for 438 days
- `userAge = 1734` → Long-term user (4+ years)

## 📊 UserAge Distribution (2026-06-21)

| User Age | Count | Percentage | Description |
|----------|-------|------------|-------------|
| 1734 | 243,932 | 78% | Very old users |
| **0** | **31,976** | **10%** | **Day-0 users (NEW!)** ← Training target |
| 1 | 10,727 | 3% | Day-1 users |
| 2 | 6,340 | 2% | Day-2 users |
| 3-1733 | ~20,000 | 7% | Other ages |

### Key Insight:
- **6,036 unique day-0 users** on 2026-06-21
- These are the users we're predicting for (will they convert?)
- Each day-0 user sees multiple posts → training examples

## 🔍 How Lambda Extracts UserAge

### Step 1: Query Day-0 Users
```sql
WITH day0_users AS (
    SELECT DISTINCT user_id
    FROM closeapp.appevents
    WHERE event_name = 'feed_first_call'
      AND JSON_EXTRACT_SCALAR(data, '$.userAge') = '0'  ← Parse from JSON!
      AND date >= '2026-06-15'
)
```

**Why we need this**:
- Identifies new users who just started using the app
- These users see posts in their feed
- We predict: Will they initiate transaction or start trial?

### Step 2: Track Their Post Views
```sql
locks AS (
    SELECT
        JSON_EXTRACT_SCALAR(e.data, '$.postId') AS post_id,
        e.user_id,
        CASE WHEN u.user_id IS NOT NULL THEN 1 ELSE 0 END AS is_day0
    FROM closeapp.appevents e
    LEFT JOIN day0_users u ON e.user_id = u.user_id
    WHERE e.event_name = 'feed_locked'
      AND JSON_EXTRACT_SCALAR(e.data, '$.isLocked') = 'true'
)
```

**What this tracks**:
- Which posts did day-0 users view?
- When did they view them?
- Did they lock (fully open) the post?

### Step 3: Track Conversions
```sql
initiates AS (
    SELECT
        JSON_EXTRACT_SCALAR(e.data, '$.postId') AS post_id,
        user_id,
        CASE WHEN u.user_id IS NOT NULL THEN 1 ELSE 0 END AS is_day0
    FROM closeapp.appevents e
    LEFT JOIN day0_users u ON e.user_id = u.user_id
    WHERE e.event_name = 'initiate_transaction'
      AND JSON_EXTRACT_SCALAR(e.data, '$.isInitiatedFromPost') = 'true'
)
```

**What this tracks**:
- Did day-0 user click "buy" after viewing a post?
- Did they start a subscription trial?
- This becomes the **label (y variable)** for training

## ✅ Why This Approach Works

### 1. Accurate JSON Parsing
- `JSON_EXTRACT_SCALAR(data, '$.userAge')` extracts from JSON string
- Handles nested JSON fields correctly
- Works with Athena's Presto SQL engine

### 2. Day-0 User Identification
- Filters `event_name = 'feed_first_call'` (first feed open)
- Checks `userAge = 0` (new user)
- Gets distinct user IDs (no duplicates)

### 3. Point-in-Time Discipline
- Features: Post state BEFORE user saw it
- Labels: User actions AFTER viewing the post
- Prevents data leakage

## 📈 Training Data Creation

### Workflow:
```
Day-0 Users (6,036/day)
    ↓
See Posts (avg 7-10 posts per user)
    ↓
Training Examples (~42,000-60,000/day)
    ↓
Label: Did they convert? (y = 0, 1, or 2)
```

### Example Training Row:
```json
{
  "user_id": "abc123",
  "post_id": "post456",
  "snapshot_time": "2026-06-21T10:30:00",
  "user_age": 0,  ← Day-0 user
  "post_age_hours": 2.5,
  "views": 150,
  "shares": 12,
  "topic": "politics",
  "district": "Central Delhi",
  "y": 1  ← User initiated transaction after viewing
}
```

## 🎯 Production Verification

### Query 1: Count Day-0 Users
```sql
SELECT COUNT(DISTINCT user_id) as day0_users
FROM closeapp.appevents
WHERE event_name = 'feed_first_call'
  AND JSON_EXTRACT_SCALAR(data, '$.userAge') = '0'
  AND date = '2026-06-21';
```
**Result**: 6,036 day-0 users ✅

### Query 2: Verify UserAge Distribution
```sql
SELECT 
    JSON_EXTRACT_SCALAR(data, '$.userAge') as userAge,
    COUNT(*) as count
FROM closeapp.appevents
WHERE event_name = 'feed_first_call'
  AND date = '2026-06-21'
GROUP BY JSON_EXTRACT_SCALAR(data, '$.userAge')
ORDER BY count DESC
LIMIT 10;
```
**Result**: Distribution shows userAge 0-1734 ✅

### Query 3: Verify Lambda Query Works
```sql
-- This is what Lambda executes
WITH day0_users AS (
    SELECT DISTINCT user_id
    FROM closeapp.appevents
    WHERE event_name = 'feed_first_call'
      AND JSON_EXTRACT_SCALAR(data, '$.userAge') = '0'
      AND date >= '2026-06-15'
)
SELECT COUNT(*) FROM day0_users;
```
**Result**: Returns day-0 user count ✅

## 🔧 Why String Comparison '0' Not Integer 0?

```sql
-- This works:
JSON_EXTRACT_SCALAR(data, '$.userAge') = '0'

-- This would NOT work:
JSON_EXTRACT_SCALAR(data, '$.userAge') = 0
```

**Reason**: 
- `JSON_EXTRACT_SCALAR` returns a **string**, not integer
- Need to compare with string '0', not integer 0
- Athena/Presto doesn't auto-convert in this context

**Alternative (type cast)**:
```sql
CAST(JSON_EXTRACT_SCALAR(data, '$.userAge') AS INTEGER) = 0
```
But string comparison is faster and safer.

## 📋 Summary

### Where UserAge Comes From:
✅ `appevents` table → `data` column → JSON string → `userAge` field

### How We Extract It:
✅ `JSON_EXTRACT_SCALAR(data, '$.userAge')` in Athena queries

### What We Use It For:
✅ Identify day-0 users (new users)  
✅ Track their post views  
✅ Track their conversions  
✅ Create training data (user-post-label triples)

### Current Production Status:
✅ Lambda queries working correctly  
✅ Day-0 users identified: ~6K/day  
✅ Training examples generated: ~40-60K/day  
✅ Ready for model training after 7-day data accumulation

### Key Stats (2026-06-21):
- **Total feed_first_call events**: 312,995
- **Day-0 users (userAge=0)**: 31,976 events from 6,036 unique users
- **Average posts/user**: 7-10 posts
- **Expected training examples/day**: 40,000-60,000

---

## 🚀 Next Steps

1. **Monitor for 7 days** - Let Lambda accumulate snapshots
2. **Verify day-0 user counts** - Should be ~6K/day consistently
3. **Check conversion rates** - What % of day-0 users convert?
4. **Run create_labels.py** - Create y variable from outcomes
5. **Build training dataset** - Join all 57 features + labels
6. **Train model** - Predict conversion probability for new users