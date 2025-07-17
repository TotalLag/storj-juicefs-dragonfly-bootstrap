#!/bin/bash
set -e

# Start Gunicorn in the background
gunicorn src.server:app --bind "0.0.0.0:$PORT" &

# Check if REDIS_URL is set
if [ -z "$REDIS_URL" ]; then
  # If REDIS_URL is not set, start DragonflyDB
  echo "Starting DragonflyDB..."
  mkdir -p /data/dragonfly
  if [ -n "$DRAGONFLY_PASSWORD" ]; then
    echo "Using pre-configured DragonflyDB password."
  else
    echo "Generating new DragonflyDB password."
    export DRAGONFLY_PASSWORD=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32)
    echo "Generated DragonflyDB password: $DRAGONFLY_PASSWORD"
  fi
  dragonfly --maxmemory 256mb \
            --logtostderr \
            --requirepass "$DRAGONFLY_PASSWORD" \
            --dir /data/dragonfly \
            --dbfilename dump.rdb \
            --nodf_snapshot_format \
            --snapshot_cron "* * * * *" &

  # Wait for DragonflyDB to start
  sleep 5

  # Check if a database file already exists
  if [ -f "/data/dragonfly/dump.rdb" ]; then
    echo "Existing database found at /data/dragonfly/dump.rdb. Skipping restore."
  else
    echo "No existing database found. Checking for backup to restore."
    # Check for backup and restore from Storj
    if [ -n "$STORJ_BUCKET_URL" ]; then
      # Extract S3 endpoint and bucket name from URL
      S3_ENDPOINT_URL=$(echo "$STORJ_BUCKET_URL" | sed -E 's|/[^/]+$||')
      BUCKET_NAME=$(echo "$STORJ_BUCKET_URL" | sed -E 's|.*/||')
      echo "Checking for backups in S3 bucket: $BUCKET_NAME at endpoint: $S3_ENDPOINT_URL"

      # Configure AWS CLI
      aws configure set aws_access_key_id "$STORJ_ACCESS_KEY"
      aws configure set aws_secret_access_key "$STORJ_SECRET_KEY"
      aws configure set default.region "$STORJ_DEFAULT_REGION"

      # List files and get the latest backup
      LATEST_BACKUP=$(aws s3 ls "s3://$BUCKET_NAME/sharedvol/meta/" --endpoint-url "$S3_ENDPOINT_URL" | grep 'dump-.*\.json\.gz' | sort -r | head -n 1 | awk '{print $4}')

      if [ -n "$LATEST_BACKUP" ]; then
        echo "Found latest backup: $LATEST_BACKUP"
        echo "Downloading backup from S3..."
        if aws s3 cp "s3://$BUCKET_NAME/sharedvol/meta/$LATEST_BACKUP" "/tmp/$LATEST_BACKUP" --endpoint-url "$S3_ENDPOINT_URL"; then
          echo "Download successful."
          echo "Restoring from backup..."
          if juicefs load "redis://:$DRAGONFLY_PASSWORD@localhost:6379" "/tmp/$LATEST_BACKUP"; then
            echo "Restore successful."
            echo "Configuring JuiceFS secret key..."
            juicefs config --secret-key "$STORJ_SECRET_KEY" "redis://:$DRAGONFLY_PASSWORD@localhost:6379"
          else
            echo "Restore failed."
          fi
          echo "Cleaning up downloaded backup file..."
          rm "/tmp/$LATEST_BACKUP"
        else
          echo "Download failed."
        fi
      else
        echo "No backup found in S3, skipping restore."
      fi
    else
      echo "STORJ_BUCKET_URL not set, skipping restore from Storj."
    fi
  fi

  wait
else
  # If REDIS_URL is set, run the proxy
  echo "Starting proxy..."
  python src/proxy.py
fi