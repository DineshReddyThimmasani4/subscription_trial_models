# Feed Ranking: Independent vs Dependent Variables

## 📊 Summary

| Type | Count | Purpose |
|------|-------|---------|
| **Independent Variables (Features)** | **48** | Used to PREDICT conversions |
| **Dependent Variables (Labels)** | **9** | What we're PREDICTING |
| **Total** | **57** | All variables |

---

## ✅ Independent Variables (Features) = 48

These are the **input features (X)** used to train the model to predict conversions.

### 1. Post Metadata (18 variables)
Static properties of the post:
1. `post_id` - Identifier
2. `created_ts` - Creation timestamp
3. `topic` - Classification topic
4. `subTopic` - Classification subtopic
5. `classified` - Is classified (0/1)
6. `distImp` - District importance (0-3)
7. `stateImp` - State importance (0-3)
8. `natImp` - National importance (0-3)
9. `isLocal` - Local news flag
10. `isReporter` - Posted by reporter
11. `media` - Has media (image/video)
12. `media_duration` - Video/audio duration
13. `titleLen` - Title length
14. `postLang` - Content language
15. `creatorID` - Creator identifier
16. `location_pid` - District PID
17. `post_state` - State name
18. `age_hours` - Post age in hours

### 2. Post Engagement (13 variables)
How users engage with the post:

**Raw Counts (8)**:
19. `views` - Total views
20. `shareCount` - Share count
21. `mediaClickedCount` - Media clicks
22. `reactionCount` - Reaction count
23. `commentCount` - Comment count
24. `readModeCount` - Read mode opens
25. `avgDur` - Average view duration
26. `listenpct` - Average listen percentage

**Derived Ratios (5)**:
27. `shares_pv` - Shares per view
28. `videoclicks_pv` - Video clicks per view
29. `reactions_pv` - Reactions per view
30. `comments_pv` - Comments per view
31. `seemore_pv` - Read mode per view

### 3. Creator Features (4 variables)
Historical creator performance:
32. `creator_prior_rate` - Prior conversion rate (day -14 to -7)
33. `creator_has_prior` - Has prior history (0/1)
34. `creator_verified` - Is verified creator
35. `creator_viewcount` - Total creator views

### 4. User-Post Relationship (6 variables)
Contextual serving information:
36. `serving_district_pid` - Which feed/district
37. `serving_state` - State of feed
38. `serving_lang` - Language of feed
39. `tier` - Feed tier (district/state/national)
40. `same_district` - Post from same district (0/1)
41. `same_state` - Post from same state (0/1)

### 5. Ecosystem Context (7 variables)
Activity in post's ecosystem:
42. `district_posts_7d` - District posts (7 days)
43. `district_reporters_7d` - District reporters (7 days)
44. `district_users_7d` - District users (7 days)
45. `state_posts_7d` - State posts (7 days)
46. `state_reporters_7d` - State reporters (7 days)
47. `lang_posts_7d` - Language posts (7 days)
48. `lang_reporters_7d` - Language reporters (7 days)

---

## 🎯 Dependent Variables (Labels) = 9

These are the **target outcomes (Y)** we're trying to predict. They represent conversion performance.

### Post Conversion History (9 variables)

**Overall Performance (3)**:
1. `post_locks_so_far` - Total locks before time T
2. `post_initiates_so_far` - Total initiates before time T
3. `post_trials_so_far` - Total trials before time T

**Day-0 Volume (3)**:
4. `post_day0_locks` - Day-0 locks before time T
5. `post_day0_initiates` - Day-0 initiates before time T
6. `post_day0_trials` - Day-0 trials before time T

**Day-0 Rates (3)**:
7. `post_day0_initiates_per_view` - Day-0 initiate rate
8. `post_day0_trials_per_view` - Day-0 trial rate
9. `post_day0_initiates_per_lock` - Day-0 "magnet rate"

---

## 🔍 Why This Split?

### Independent Variables (Features - X):
- These represent what we **KNOW** at the time we rank the post
- Available **BEFORE** showing post to user
- Used to **predict** which posts will convert
- Examples: post topic, creator quality, ecosystem activity, age

### Dependent Variables (Labels - Y):
- These represent what we're **TRYING TO PREDICT**
- Only known **AFTER** showing post to users
- The model learns to predict these from the features
- Examples: will user lock? will they initiate? will they trial?

---

## 📈 Training Process

### Step 1: Create Training Data
For each (post, time T) snapshot:
```
Features (X):
  - Post metadata (18 vars)
  - Engagement at time T (13 vars)
  - Creator features (4 vars)
  - User-post relationship (6 vars)
  - Ecosystem context (7 vars)
  Total: 48 independent variables

Labels (Y):
  - Conversions AFTER time T (9 vars)
  - Typically: conversions in next 1-7 days
```

### Step 2: Train Model
```
Model learns:
  Given these 48 features (X)
  Predict these 9 outcomes (Y)
```

### Step 3: Inference (Production)
```
For new post at time T:
  - Extract 48 features from current data
  - Model predicts 9 conversion metrics
  - Rank posts by predicted conversion potential
```

---

## 🎯 Primary Target Variable

While we have 9 dependent variables, the **primary target** for ranking is typically:

**`post_day0_initiates_per_lock`** - Day-0 "Magnet Rate"

**Why this metric?**
- Focuses on **new user acquisition** (day-0)
- Measures **conversion efficiency** (initiates per lock, not just raw volume)
- Captures the "magnet" quality - does post attract new users?
- Most actionable for feed ranking

**Alternative targets**:
- `post_day0_initiates_per_view` - For optimizing impressions
- `post_day0_trials_per_view` - For optimizing trial conversions
- `post_locks_so_far` - For maximizing total locks (volume)

---

## 📊 Variable Type Summary

| Category | Independent (X) | Dependent (Y) | Total |
|----------|-----------------|---------------|-------|
| Post Meta/Static | 18 | 0 | 18 |
| Post Engagement | 13 | 0 | 13 |
| Creator | 4 | 0 | 4 |
| User-Post Relationship | 6 | 0 | 6 |
| Ecosystem Context | 7 | 0 | 7 |
| **Post Dep Var Performance** | **0** | **9** | **9** |
| **TOTAL** | **48** | **9** | **57** |

---

## 🎯 Key Insight

**Independent Variables (48)**: What we know NOW  
**Dependent Variables (9)**: What we want to predict for the FUTURE

The model learns the relationship between the 48 features and 9 outcomes, then uses this to rank posts by predicted conversion potential.