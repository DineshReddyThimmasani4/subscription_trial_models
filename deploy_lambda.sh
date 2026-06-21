#!/bin/bash
# Deploy Lambda function for feed ranking

set -e

FUNCTION_NAME="feed-ranking-snapshot"
REGION="ap-south-1"
ROLE_ARN="arn:aws:iam::914864774004:role/external-news-rss-lambda-role"
LAYER_ARN="arn:aws:lambda:ap-south-1:336392948345:layer:AWSSDKPandas-Python311:26"

echo "Creating Lambda deployment package..."
zip lambda_function.zip lambda_function.py

echo "Deploying Lambda function..."
aws lambda update-function-code \
  --function-name $FUNCTION_NAME \
  --zip-file fileb://lambda_function.zip \
  --region $REGION

echo "Updating function configuration..."
aws lambda update-function-configuration \
  --function-name $FUNCTION_NAME \
  --timeout 600 \
  --memory-size 1024 \
  --environment Variables="{BUCKET=nearme-feed-store,ATHENA_DATABASE=closeapp,CLASSIFICATION_BUCKET=closeapp-athena}" \
  --layers $LAYER_ARN \
  --region $REGION

echo "✅ Lambda deployed successfully!"
echo "Function: $FUNCTION_NAME"
echo "Region: $REGION"

# Clean up
rm lambda_function.zip