#!/bin/bash
set -e

# --- Local Dragonfly Setup ---
echo "Starting local DragonflyDB..."
mkdir -p /data/dragonfly

if [ -z "$DRAGONFLY_PASSWORD" ]; then
  echo "Generating new DragonflyDB password..."
  export DRAGONFLY_PASSWORD=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32)
  echo "Generated DragonflyDB password: $DRAGONFLY_PASSWORD"
else
  echo "Using provided DragonflyDB password."
fi

# Start DragonflyDB
dragonfly --maxmemory 256mb \
          --logtostderr \
          --requirepass "$DRAGONFLY_PASSWORD" \
          --dir /data/dragonfly \
          --dbfilename dump.rdb \
          --nodf_snapshot_format \
          --snapshot_cron "* * * * *" &

# Give Dragonfly time to initialize
sleep 5

# --- JuiceFS Volume Check ---
META_URL="redis://:$DRAGONFLY_PASSWORD@localhost:6379"

echo "Checking if JuiceFS volume exists..."
if juicefs status "$META_URL"; then
  echo "JuiceFS volume exists."

  # Check for local dump.rdb
  if [ -f "/data/dragonfly/dump.rdb" ]; then
    echo "Local dump.rdb found. No restore needed."
  else
    echo "No local dump.rdb found. Attempting to restore from backup..."

    if [ -n "$STORJ_BUCKET_URL" ]; then
      # Parse bucket and endpoint
      S3_ENDPOINT_URL=$(echo "$STORJ_BUCKET_URL" | sed -E 's|/[^/]+$||')
      BUCKET_NAME=$(echo "$STORJ_BUCKET_URL" | sed -E 's|.*/||')

      # Configure AWS CLI
      aws configure set aws_access_key_id "$STORJ_ACCESS_KEY"
      aws configure set aws_secret_access_key "$STORJ_SECRET_KEY"
      aws configure set default.region "$STORJ_DEFAULT_REGION"

      echo "Looking for latest metadata backup in S3..."
      LATEST_BACKUP=$(aws s3 ls "s3://$BUCKET_NAME/sharedvol/meta/" --endpoint-url "$S3_ENDPOINT_URL" | grep 'dump-.*\.json\.gz' | sort -r | head -n 1 | awk '{print $4}')

      if [ -n "$LATEST_BACKUP" ]; then
        echo "Found backup: $LATEST_BACKUP"
        echo "Downloading..."
        if aws s3 cp "s3://$BUCKET_NAME/sharedvol/meta/$LATEST_BACKUP" "/tmp/$LATEST_BACKUP" --endpoint-url "$S3_ENDPOINT_URL"; then
          echo "Restoring metadata with JuiceFS..."
          juicefs load "$META_URL" "/tmp/$LATEST_BACKUP" && echo "Restore complete."
          rm "/tmp/$LATEST_BACKUP"
        else
          echo "Backup download failed."
        fi
      else
        echo "No backup found in S3."
      fi
    else
      echo "STORJ_BUCKET_URL not set. Cannot restore."
    fi
  fi

else
  echo "JuiceFS volume does not exist. Initializing new volume..."

  if [ -n "$STORJ_BUCKET_URL" ]; then
    # Parse bucket and endpoint
    S3_ENDPOINT_URL=$(echo "$STORJ_BUCKET_URL" | sed -E 's|/[^/]+$||')
    BUCKET_NAME=$(echo "$STORJ_BUCKET_URL" | sed -E 's|.*/||')

    echo "Formatting JuiceFS volume..."
    juicefs format \
      --storage s3 \
      --bucket "$S3_ENDPOINT_URL/$BUCKET_NAME" \
      --access-key "$STORJ_ACCESS_KEY" \
      --secret-key "$STORJ_SECRET_KEY" \
      "$META_URL" \
      "sharedvol"
    echo "Format complete."

    # Configure AWS CLI
    aws configure set aws_access_key_id "$STORJ_ACCESS_KEY"
    aws configure set aws_secret_access_key "$STORJ_SECRET_KEY"
    aws configure set default.region "$STORJ_DEFAULT_REGION"

    echo "Searching for latest backup in S3..."
    LATEST_BACKUP=$(aws s3 ls "s3://$BUCKET_NAME/sharedvol/meta/" --endpoint-url "$S3_ENDPOINT_URL" | grep 'dump-.*\.json\.gz' | sort -r | head -n 1 | awk '{print $4}')

    if [ -n "$LATEST_BACKUP" ]; then
      echo "Found backup: $LATEST_BACKUP"
      if aws s3 cp "s3://$BUCKET_NAME/sharedvol/meta/$LATEST_BACKUP" "/tmp/$LATEST_BACKUP" --endpoint-url "$S3_ENDPOINT_URL"; then
        echo "Restoring JuiceFS metadata..."
        juicefs load "$META_URL" "/tmp/$LATEST_BACKUP" && echo "Restore complete."
        rm "/tmp/$LATEST_BACKUP"
      else
        echo "Failed to download backup."
      fi
    else
      echo "No backup found in S3."
    fi

    echo "Saving Storj credentials to JuiceFS volume..."
    juicefs config --secret-key "$STORJ_SECRET_KEY" "$META_URL"
  else
    echo "STORJ_BUCKET_URL not set. Cannot initialize or restore JuiceFS volume."
  fi
fi

wait