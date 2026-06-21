# Feed Ranking Pipeline - Deployment Status

## ✅ COMPLETED

### 1. Lambda Function (15-min Dynamic Snapshots)
**Function**: `feed-ranking-snapshot`  
**Status**: ✅ **DEPLOYED & RUNNING**

- **Package**: Lambda code + AWS SDK Pandas layer (includes pyarrow)
- **Trigger**: EventBridge - `rate(15 minutes)`
- **Timeout**: 600 seconds (10 min)
- **Memory**: 1024 MB
- **VPC**: NO (no MongoDB needed!)
- **Environment**:
  - `BUCKET=nearme-feed-store`
  - `ATHENA_DATABASE=closeapp`
  - `CLASSIFICATION_BUCKET=closeapp-athena`

**Data Sources**:
- ✅ S3 classification dumps: `s3://closeapp-athena/post_classification/`
- ✅ Athena appevents: engagement + conversions (NO MongoDB!)
- ✅ S3 static mapping: `s3://nearme-feed-store/config/district_state_mapping.json`

**Output**: `s3://nearme-feed-store/dynamic/date=YYYY-MM-DD/run=HHMM/snapshot.jsonl`

**Last Test**: 2026-06-22 00:15 UTC - Ran in 12.3 seconds ✅

---

## 🚧 PENDING

### 2. EMR Serverless Daily Job (Ecosystem + Creator Features)
**Status**: ⏳ **CODE READY - NEEDS DEPLOYMENT**

**Requirements**:
1. EMR Serverless Application with PySpark
2. MongoDB connection (for Post, User, Place collections)
3. Glue Catalog access (for appevents table)
4. S3 write permissions to `s3://nearme-feed-store/features/`

**Deployment Steps**:
```bash
# 1. Create EMR Serverless application (if not exists)
aws emr-serverless create-application \
  --name feed-ranking-daily-aggregates \
  --type SPARK \
  --release-label emr-7.1.0

# 2. Upload job script to S3
aws s3 cp /tmp/feed_files_updated/daily_job_updated.py \
  s3://nearme-feed-store/jobs/daily_job.py

# 3. Submit job (daily via EventBridge or cron)
aws emr-serverless start-job-run \
  --application-id <APP_ID> \
  --execution-role-arn <EMR_ROLE_ARN> \
  --job-driver '{
    "sparkSubmit": {
      "entryPoint": "s3://nearme-feed-store/jobs/daily_job.py",
      "sparkSubmitParameters": "--conf spark.jars.packages=org.mongodb.spark:mongo-spark-connector_2.12:10.2.0"
    }
  }' \
  --configuration-overrides '{
    "monitoringConfiguration": {
      "s3MonitoringConfiguration": {
        "logUri": "s3://nearme-feed-store/emr-logs/"
      }
    }
  }'
```

**Output**:
- `s3://nearme-feed-store/features/eco/district/date=YYYY-MM-DD/` (district aggregates)
- `s3://nearme-feed-store/features/eco/state/date=YYYY-MM-DD/` (state aggregates)
- `s3://nearme-feed-store/features/eco/lang/date=YYYY-MM-DD/` (language aggregates)
- `s3://nearme-feed-store/features/creator/date=YYYY-MM-DD/` (creator features)

---

## 📊 Pipeline Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                  FEED RANKING PIPELINE                         │
└────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  LAMBDA (every 15 min) - feed-ranking-snapshot              │
│  ✅ DEPLOYED & RUNNING                                      │
├─────────────────────────────────────────────────────────────┤
│  INPUT:                                                       │
│    • S3 classification dumps (post metadata)                 │
│    • Athena appevents (engagement + conversions)             │
│    • S3 static mapping (district→state)                      │
│  OUTPUT:                                                      │
│    • 38 features per post (dynamic snapshots)                │
│    • s3://nearme-feed-store/dynamic/                         │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  EMR SERVERLESS (daily) - ecosystem + creator features       │
│  ⏳ PENDING DEPLOYMENT                                       │
├─────────────────────────────────────────────────────────────┤
│  INPUT:                                                       │
│    • MongoDB (Post, User, Place)                             │
│    • Athena appevents (conversions)                          │
│  OUTPUT:                                                      │
│    • 11 features (7 ecosystem + 4 creator)                   │
│    • s3://nearme-feed-store/features/                        │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  ATHENA QUERIES (on-demand)                                  │
│  📝 SCRIPTS READY                                            │
├─────────────────────────────────────────────────────────────┤
│  • create_labels.py - Creates y variable from outcomes       │
│  • build_training_data.py - Joins all sources (57 vars + y) │
└─────────────────────────────────────────────────────────────┘
```

---

## 🎯 Next Steps

### Immediate (within 7 days):
1. **Deploy EMR daily job** - ecosystem + creator features
2. **Let data accumulate** - Lambda runs every 15 min, EMR runs daily
3. **Wait 7 days** - Need full 7-day window for training

### After 7 Days:
1. Run `create_labels.py` - Create training labels
2. Run `build_training_data.py` - Build complete training dataset (57 features + y)
3. **Train model** - XGBoost/LightGBM on training data
4. **Deploy model** - Real-time serving layer

---

## 📁 File Locations

### Deployed Code:
- Lambda: `feed-ranking-snapshot` (AWS Lambda)
- Layer: `arn:aws:lambda:ap-south-1:336392948345:layer:AWSSDKPandas-Python311:26`

### Ready to Deploy:
- `/tmp/feed_files_updated/daily_job_updated.py` - EMR daily job
- `/tmp/feed_files_updated/create_labels.py` - Labels script
- `/tmp/feed_files_updated/build_training_data.py` - Training data builder

### S3 Data:
- Dynamic snapshots: `s3://nearme-feed-store/dynamic/`
- Place mapping: `s3://nearme-feed-store/config/district_state_mapping.json`
- Classification dumps: `s3://closeapp-athena/post_classification/`

---

## 🔍 Verification

### Check Lambda is Running:
```bash
# Check recent executions
aws lambda list-tags --resource arn:aws:lambda:ap-south-1:914864774004:function:feed-ranking-snapshot

# View CloudWatch logs
aws logs tail /aws/lambda/feed-ranking-snapshot --follow

# Check S3 output
aws s3 ls s3://nearme-feed-store/dynamic/ --recursive | tail -20
```

### Test Lambda Manually:
```bash
aws lambda invoke \
  --function-name feed-ranking-snapshot \
  --region ap-south-1 \
  --log-type Tail \
  /tmp/response.json
```

---

## 📊 Variables Status

- **57 total variables**
- **55 ready** (96%)
- **2 pending verification** (media_clicks, read_mode - need to confirm event names in appevents)

### Breakdown:
- **38 from Lambda** (post meta + engagement + conversion-history)
- **11 from EMR** (4 creator + 7 ecosystem)
- **8 computed at runtime** (ratios, age_hours, classified flag)

---

## 💰 Cost Estimate

### Lambda (15-min, 1024MB, 12s avg):
- Executions: 96/day × 30 = 2,880/month
- Compute: 2,880 × 12s × 1024MB = ~$2/month
- Athena: ~$50/month (query costs)
- **Total Lambda: ~$52/month**

### EMR (daily, ~30 min):
- Executions: 30/month
- Cost: ~$3/run = **$90/month**

### Storage:
- Dynamic snapshots: ~10GB/month = **$0.25/month**
- Feature data: ~5GB/month = **$0.13/month**

**Total Pipeline Cost: ~$142/month**

---

## ✅ Success Criteria

Lambda deployment is **SUCCESSFUL** if:
- ✅ Function runs without errors
- ✅ EventBridge trigger fires every 15 minutes
- ✅ Output files created in S3
- ✅ Logs show "INCREMENTAL complete" or "COLD START complete"
- ✅ Execution time < 60 seconds (currently 12s)

**Status: ALL CRITERIA MET** ✅