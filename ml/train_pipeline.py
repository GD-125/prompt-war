"""
VenueIQ — Vertex AI Custom Training Pipeline
Trains a time-series crowd movement forecasting model on historical Firestore data.
Exports to Vertex AI Model Registry for online prediction.

Run manually:     python train_pipeline.py
Scheduled via:    Cloud Scheduler → Cloud Run job (weekly retraining)
"""

import os
import json
import math
import logging
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Dict, Optional

from google.cloud import firestore, aiplatform, storage
from google.cloud.aiplatform import pipeline_jobs
import joblib

PROJECT_ID  = os.environ.get("GCP_PROJECT_ID", "venueiq-prod")
REGION      = os.environ.get("GCP_REGION",     "us-central1")
BUCKET      = os.environ.get("GCS_BUCKET",     f"gs://{PROJECT_ID}-ml-artifacts")
MODEL_NAME  = "venueiq-crowd-forecaster"
SERVING_IMG = "us-docker.pkg.dev/vertex-ai/prediction/sklearn-cpu.1-3:latest"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("venueiq-training")

# ── Feature Engineering ───────────────────────────────────────────────────────

def extract_features(density_series: List[float],
                     timestamps:     List[str],
                     capacity:       float) -> np.ndarray:
    """
    Feature vector per timestep:
      - Normalised occupancy (0–1)
      - Rate of change (first derivative)
      - Acceleration (second derivative)
      - Time-of-day sin/cos (cyclical encoding)
      - Day-of-week sin/cos
      - Rolling mean (5, 15, 30 ticks)
      - Zone type one-hot
    """
    n = len(density_series)
    if n < 3:
        return np.zeros((n, 12))

    occ = np.array([c / max(capacity, 1) for c in density_series])
    roc = np.gradient(occ)  # rate of change
    acc = np.gradient(roc)  # acceleration

    # Cyclical time features
    tod_sin, tod_cos = [], []
    dow_sin, dow_cos = [], []
    for ts_str in timestamps:
        try:
            dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            secs = dt.hour * 3600 + dt.minute * 60 + dt.second
            tod_sin.append(math.sin(2 * math.pi * secs / 86400))
            tod_cos.append(math.cos(2 * math.pi * secs / 86400))
            dow_sin.append(math.sin(2 * math.pi * dt.weekday() / 7))
            dow_cos.append(math.cos(2 * math.pi * dt.weekday() / 7))
        except Exception:
            tod_sin.append(0); tod_cos.append(0)
            dow_sin.append(0); dow_cos.append(0)

    tod_sin = np.array(tod_sin)
    tod_cos = np.array(tod_cos)
    dow_sin = np.array(dow_sin)
    dow_cos = np.array(dow_cos)

    # Rolling statistics
    def rolling_mean(arr, w):
        result = np.zeros(len(arr))
        for i in range(len(arr)):
            result[i] = np.mean(arr[max(0, i-w):i+1])
        return result

    rm5  = rolling_mean(occ, 5)
    rm15 = rolling_mean(occ, 15)
    rm30 = rolling_mean(occ, 30)

    return np.column_stack([occ, roc, acc, tod_sin, tod_cos, dow_sin, dow_cos, rm5, rm15, rm30])


def create_supervised_dataset(features: np.ndarray,
                               horizon_ticks: int = 10,
                               window: int = 20) -> Tuple[np.ndarray, np.ndarray]:
    """
    Sliding window to create (X, y) pairs.
    X: window of feature vectors
    y: occupancy at horizon_ticks steps ahead
    """
    X, y = [], []
    occ_col = 0  # first column is normalised occupancy
    for i in range(window, len(features) - horizon_ticks):
        X.append(features[i-window:i].flatten())
        y.append(features[i + horizon_ticks, occ_col])
    return np.array(X), np.array(y)


# ── Data Loader ───────────────────────────────────────────────────────────────

async def load_training_data(days_back: int = 30) -> List[Dict]:
    """Load zone history from Firestore for all venues."""
    db = firestore.AsyncClient(project=PROJECT_ID)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()

    records = []
    async for venue_doc in db.collection("zone_history").stream():
        venue_data = venue_doc.to_dict()
        venue_id   = venue_doc.id
        for zone_id, snapshots in venue_data.get("zones", {}).items():
            filtered = [s for s in snapshots if s.get("ts", "") >= cutoff]
            if len(filtered) < 60:
                continue  # need at least 30 min of data
            filtered.sort(key=lambda x: x["ts"])
            records.append({
                "venue_id": venue_id,
                "zone_id":  zone_id,
                "counts":   [s["count"] for s in filtered],
                "timestamps": [s["ts"] for s in filtered],
            })

    logger.info(f"Loaded {len(records)} zone time-series for training")
    return records


# ── Model Training (Gradient Boosted Trees via sklearn) ───────────────────────

def train_model(records: List[Dict], horizon_mins: int = 10,
                ping_interval_sec: int = 30):
    """
    Train an XGBoost-style GBT model (using sklearn GBR as Vertex-compatible alternative).
    In production, replace with XGBoost or a Temporal Fusion Transformer on Vertex AI.
    """
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import mean_absolute_error, r2_score

    horizon_ticks = int(horizon_mins * 60 / ping_interval_sec)
    WINDOW        = 20

    X_all, y_all = [], []
    for rec in records:
        capacity = 3000  # TODO: fetch from Firestore venue metadata
        features = extract_features(rec["counts"], rec["timestamps"], capacity)
        X, y     = create_supervised_dataset(features, horizon_ticks, WINDOW)
        X_all.append(X)
        y_all.append(y)

    if not X_all:
        logger.error("No training data available")
        return None

    X = np.vstack(X_all)
    y = np.concatenate(y_all)
    logger.info(f"Training set: {X.shape[0]} samples, {X.shape[1]} features")

    X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.15, random_state=42)

    pipeline = Pipeline([
        ('scaler', StandardScaler()),
        ('gbr', GradientBoostingRegressor(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.05,
            subsample=0.8,
            min_samples_leaf=5,
            random_state=42,
        )),
    ])

    pipeline.fit(X_train, y_train)

    val_preds = pipeline.predict(X_val)
    mae  = mean_absolute_error(y_val, val_preds)
    r2   = r2_score(y_val, val_preds)
    logger.info(f"Validation MAE: {mae:.4f} occupancy ratio | R²: {r2:.4f}")

    return pipeline, {"mae": mae, "r2": r2, "n_samples": len(X)}


# ── Vertex AI Model Registration ──────────────────────────────────────────────

def upload_model_to_vertex(model_pipeline, metrics: Dict):
    """Save model artifact to GCS, register in Vertex AI Model Registry."""
    import tempfile, os

    aiplatform.init(project=PROJECT_ID, location=REGION)

    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = os.path.join(tmpdir, "model.joblib")
        joblib.dump(model_pipeline, model_path)

        # Upload to GCS
        gcs_path = f"{BUCKET}/models/{MODEL_NAME}/{datetime.now().strftime('%Y%m%d_%H%M%S')}/model.joblib"
        blob = storage.Client().bucket(BUCKET.replace("gs://", "")).blob(
            gcs_path.replace(BUCKET + "/", "")
        )
        blob.upload_from_filename(model_path)
        logger.info(f"Model uploaded to {gcs_path}")

        # Register model
        model = aiplatform.Model.upload(
            display_name=MODEL_NAME,
            artifact_uri=os.path.dirname(gcs_path),
            serving_container_image_uri=SERVING_IMG,
            labels={
                "team":    "venueiq",
                "horizon": "10min",
                "mae":     str(round(metrics["mae"], 4)),
            },
        )
        logger.info(f"Model registered: {model.resource_name}")

        # Deploy to endpoint
        endpoint = aiplatform.Endpoint.list(
            filter=f'display_name="{MODEL_NAME}-endpoint"',
            order_by="create_time desc",
        )
        if endpoint:
            endpoint[0].deploy(model=model, traffic_percentage=100,
                               machine_type="n1-standard-2")
            logger.info(f"Model deployed to existing endpoint: {endpoint[0].resource_name}")
        else:
            new_ep = model.deploy(
                endpoint_display_name=f"{MODEL_NAME}-endpoint",
                machine_type="n1-standard-2",
                min_replica_count=1,
                max_replica_count=5,
                traffic_percentage=100,
            )
            logger.info(f"New endpoint created: {new_ep.resource_name}")

        return model.resource_name


# ── Main ──────────────────────────────────────────────────────────────────────

import asyncio

async def main():
    logger.info("=== VenueIQ ML Training Pipeline ===")

    records = await load_training_data(days_back=30)
    if not records:
        logger.error("No training data — aborting")
        return

    result = train_model(records, horizon_mins=10)
    if not result:
        return

    pipeline, metrics = result
    logger.info(f"Training complete: {metrics}")

    resource_name = upload_model_to_vertex(pipeline, metrics)
    logger.info(f"Pipeline complete. Model: {resource_name}")


if __name__ == "__main__":
    asyncio.run(main())
