# Data Leakage Prevention - Temporal Boundaries

## 🎯 Critical Question: Do Variables Cross Time T?

**Answer: YES, we need to be VERY careful about temporal boundaries!**

---

## ⚠️ The Data Leakage Problem

### Training Instance Structure:
```
At time T, user U sees post P in their feed

Features (X): What we know at time T (BEFORE showing post)
Label (Y): What happens AFTER time T (user's conversion)
```

### The Leakage Risk:
If features include information from AFTER time T, or include the current user's interaction, we're **cheating** - using future information to predict the future!

---

## 🔍 Variable-by-Variable Analysis

### ✅ **SAFE Variables (No Leakage Risk)**

#### 1. Post Metadata (18 variables) - **SAFE**
```
post_id, created_ts, topic, subTopic, classified, 
distImp, stateImp, natImp, isLocal, isReporter,
media, media_duration, titleLen, postLang,
creatorID, location_pid, post_state, age_hours
```
**Why safe?**: These are STATIC properties of the post, determined at creation time (before T)

---

#### 2. Creator Features (4 variables) - **SAFE**
```
creator_prior_rate, creator_has_prior, 
creator_verified, creator_viewcount
```
**Why safe?**: Computed from historical window (day -14 to -7 before T), doesn't include current user

---

#### 3. Ecosystem Context (7 variables) - **SAFE**
```
district_posts_7d, district_reporters_7d, district_users_7d,
state_posts_7d, state_reporters_7d, 
lang_posts_7d, lang_reporters_7d
```
**Why safe?**: Aggregated over past 7 days before T, independent of current user

---

#### 4. User-Post Relationship (6 variables) - **SAFE**
```
serving_district_pid, serving_state, serving_lang,
tier, same_district, same_state
```
**Why safe?**: Context of WHERE/HOW post is being shown, known before showing it

---

### ⚠️ **RISKY Variables (Need Careful Handling)**

#### 5. Post Engagement (13 variables) - **REQUIRES EXCLUSION**

**Current Implementation Issue**:
```python
# Lambda snapshot aggregates ALL events before time T
views = COUNT(*) FROM post_seen WHERE event_time < T
```

**The Problem**:
- If we're predicting "will user U convert after seeing post P at time T"
- And `views` includes user U's view at time T
- That's circular! We're using the current interaction as a feature

**Two Scenarios**:

**Scenario A: Predicting BEFORE user views post (feed ranking)**
```
Time T = when we RANK posts for user's feed (before serving)

BAD: Use engagement counts including user U's potential future view
GOOD: Use engagement counts from OTHER users only (exclude user U entirely)
```

**Scenario B: Predicting AFTER user views post (lock prediction)**
```
Time T = when user SEES post (after feed_locked event)

BAD: Use engagement including user U's current view
GOOD: Use engagement from OTHER users only (exclude user U)
```

**Solution**:
```sql
-- Engagement from OTHER users, before time T
SELECT 
    post_id,
    COUNT(*) as views,
    SUM(media_clicked) as media_clicks,
    ...
FROM post_seen_events
WHERE event_time < T
  AND user_id != CURRENT_USER_ID  -- ✅ Exclude current user!
GROUP BY post_id
```

---

#### 6. Post Historical Performance (9 variables) - **REQUIRES EXCLUSION**

**Current Variables**:
```
post_locks_so_far, post_initiates_so_far, post_trials_so_far,
post_day0_locks, post_day0_initiates, post_day0_trials,
post_day0_initiates_per_view, post_day0_trials_per_view, 
post_day0_initiates_per_lock
```

**The Problem**:
- If we're predicting "will user U lock/initiate after viewing post P"
- These counts SHOULD NOT include user U's decision
- They should represent "how well this post performed for OTHER users"

**Solution**:
```sql
-- Conversions from OTHER users, before time T
SELECT
    post_id,
    COUNT(*) as locks,
    SUM(initiated) as initiates,
    ...
FROM conversion_events
WHERE event_time < T
  AND user_id != CURRENT_USER_ID  -- ✅ Exclude current user!
GROUP BY post_id
```

---

## 🎯 Correct Training Data Generation

### Step 1: Identify Training Instances
```sql
-- For each day-0 user who viewed a post
SELECT
    user_id,
    post_id,
    view_time AS T  -- This is our time boundary
FROM feed_locked_events
WHERE userAge = 0
  AND isLocked = true
```

### Step 2: Extract Features (BEFORE time T, EXCLUDING current user)
```sql
-- Engagement from OTHER users before T
WITH other_user_engagement AS (
    SELECT
        post_id,
        COUNT(*) as views_other_users,
        SUM(media_clicked) as media_clicks_other_users,
        ...
    FROM post_seen_events
    WHERE event_time < T
      AND user_id != CURRENT_USER_ID  -- ✅ Critical!
    GROUP BY post_id
),

-- Conversions from OTHER users before T
other_user_conversions AS (
    SELECT
        post_id,
        COUNT(*) as locks_other_users,
        SUM(initiated) as initiates_other_users,
        ...
    FROM conversion_events  
    WHERE event_time < T
      AND user_id != CURRENT_USER_ID  -- ✅ Critical!
    GROUP BY post_id
)

-- Combine with static features
SELECT
    -- Static features (always safe)
    p.post_id,
    p.topic,
    p.created_ts,
    ...
    
    -- Engagement from OTHER users
    e.views_other_users,
    e.media_clicks_other_users,
    ...
    
    -- Conversions from OTHER users  
    c.locks_other_users,
    c.initiates_other_users,
    ...
    
FROM posts p
LEFT JOIN other_user_engagement e ON p.post_id = e.post_id
LEFT JOIN other_user_conversions c ON p.post_id = c.post_id
WHERE p.post_id = CURRENT_POST_ID
```

### Step 3: Extract Label (AFTER time T)
```sql
-- What did current user do AFTER viewing post?
SELECT
    user_id,
    post_id,
    CASE
        WHEN trial_started THEN 2
        WHEN initiate_transaction THEN 1  
        ELSE 0
    END AS y
FROM (
    SELECT
        pv.user_id,
        pv.post_id,
        MAX(CASE WHEN e.event_name = 'initiate_transaction' 
                 AND e.event_time >= pv.view_time  -- ✅ AFTER view
                 AND e.event_time < pv.view_time + 7*24*3600*1000  -- within 7 days
            THEN 1 ELSE 0 END) AS initiate_transaction,
        MAX(CASE WHEN e.event_name = 'subscription_trial_started'
                 AND e.event_time >= pv.view_time  -- ✅ AFTER view
                 AND e.event_time < pv.view_time + 7*24*3600*1000
            THEN 1 ELSE 0 END) AS trial_started
    FROM post_views pv
    LEFT JOIN appevents e ON e.user_id = pv.user_id
    GROUP BY pv.user_id, pv.post_id
)
```

---

## 📊 Correct Variable Definitions

### Independent Variables (ALL 57 variables are features!)

All 57 variables are INPUTS to the model, but with critical caveat:

**Engagement & Conversion history MUST exclude current user**:
```
views → views_from_other_users_before_T
locks_so_far → locks_from_other_users_before_T
initiates_so_far → initiates_from_other_users_before_T
...
```

### Dependent Variable (1 ordinal label)

**`y`** = Current user's outcome AFTER viewing post at time T
- 0 = No conversion
- 1 = Initiated transaction  
- 2 = Started trial

---

## 🔧 Implementation Fix Needed

### Current Lambda Implementation:
```python
# PROBLEM: Includes ALL users
views = COUNT(*) FROM post_seen WHERE event_time < T
```

### Fixed Implementation:
```python
# SOLUTION: Exclude current user
views_other_users = COUNT(*) 
    FROM post_seen 
    WHERE event_time < T
      AND user_id != current_user_id
```

### Where to Apply Fix:

1. **Lambda snapshot function** - Already computes aggregates before T ✅
   - But doesn't know about "current user" (runs every 15 min for all posts)
   - **FIX**: Keep as-is (aggregates over ALL users), but...

2. **Training data generation** (`create_labels.py`) - **CRITICAL FIX NEEDED**
   - When creating training instance for (user U, post P, time T)
   - **MUST** recompute engagement/conversion features EXCLUDING user U
   - OR use snapshot from slightly BEFORE user U's view time

---

## ✅ Recommended Approach

### Option 1: Use Snapshot from BEFORE User View (Simpler)
```python
# User U views post P at view_time T
# Use snapshot from T - 15min (before user saw it)
# Snapshot naturally excludes user U's interaction

features = load_snapshot(post_id=P, timestamp=T - 900000)  # 15 min earlier
label = compute_outcome(user=U, post=P, after_time=T)
```

**Pros**: 
- Simpler implementation
- Lambda snapshots already computed
- Natural temporal separation

**Cons**:
- Features might be slightly stale (up to 15 min old)
- Still includes user U's past interactions with OTHER posts

---

### Option 2: Exclude Current User (More Accurate)
```python
# Explicitly exclude user U from all aggregates
features = compute_features(
    post_id=P, 
    before_time=T,
    exclude_user=U  # ✅ Critical!
)
label = compute_outcome(user=U, post=P, after_time=T)
```

**Pros**:
- Most accurate
- Completely eliminates leakage

**Cons**:
- Requires per-user feature computation
- More expensive at training time

---

## 🎯 Recommendation

**Use Option 1** (snapshot from before user view):
1. Lambda creates snapshots every 15 min with ALL users
2. Training data uses snapshot timestamp slightly BEFORE user viewed post
3. This naturally excludes current user's view event
4. Simple, efficient, and safe

**Critical check in `create_labels.py`**:
```python
# Line 79: Find snapshot time <= view_time
MAX(d.run_ts) AS snapshot_ts
WHERE d.run_ts <= pv.view_time  # ✅ BEFORE user saw post
```

This ensures features come from BEFORE the user interaction! ✅

---

## 📋 Summary

**Question**: Do variables cross time T?

**Answer**: 
- ✅ Post metadata (18) - SAFE (static)
- ✅ Creator features (4) - SAFE (historical window)
- ✅ Ecosystem (7) - SAFE (7-day aggregate)
- ✅ User-post relationship (6) - SAFE (serving context)
- ⚠️ Engagement (13) - SAFE if using snapshot BEFORE user view
- ⚠️ Post performance (9) - SAFE if using snapshot BEFORE user view

**Key insight**: Lambda snapshots aggregate over ALL users before time T. When creating training data, we use the snapshot from BEFORE each user viewed the post, which naturally excludes their interaction. ✅

**Total**: All 57 variables are FEATURES (independent variables)  
**Label**: User's conversion outcome AFTER viewing (0/1/2)