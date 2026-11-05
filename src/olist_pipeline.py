import hashlib
import os
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
import psycopg

from olist_model import FEATURE_COLUMNS, NUMERIC_COLUMNS, fit_model, save_predictor, split_by_time


REQUIRED_COLUMNS = ["order_id", "order_purchase_timestamp", *FEATURE_COLUMNS, "late"]
DB_CONNECT_TIMEOUT = 10


def extract(run_key):
    directory = (Path(os.environ["OLIST_WORKDIR"]) / run_key).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, connect_timeout=DB_CONNECT_TIMEOUT) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM public.orders ORDER BY order_purchase_timestamp, order_id")
            frame = pd.DataFrame(cursor.fetchall(), columns=[column.name for column in cursor.description])
    snapshot = directory / "snapshot.csv"
    frame.to_csv(snapshot, index=False, encoding="utf-8")
    return str(snapshot)


def validate(snapshot):
    frame = pd.read_csv(snapshot, encoding="utf-8")
    if frame.empty:
        raise ValueError("Snapshot must not be empty")
    missing_columns = sorted(set(REQUIRED_COLUMNS) - set(frame.columns))
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")
    if not frame["order_id"].is_unique:
        raise ValueError("order_id must be unique")
    if not np.isfinite(frame[NUMERIC_COLUMNS].to_numpy(dtype=float)).all():
        raise ValueError("Numeric features must be finite")
    if not frame["late"].isin([0, 1]).all():
        raise ValueError("late must contain only 0/1 labels")
    return str(snapshot)


def prepare(snapshot):
    snapshot = Path(snapshot)
    snapshot_sha256 = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    frame = pd.read_csv(snapshot, encoding="utf-8")
    cutoff = os.environ.get("OLIST_CUTOFF", "2017-07-31")
    train_frame, validation_frame = split_by_time(frame, cutoff=cutoff)
    train_path = snapshot.parent / "train.csv"
    validation_path = snapshot.parent / "validation.csv"
    train_frame.to_csv(train_path, index=False, encoding="utf-8")
    validation_frame.to_csv(validation_path, index=False, encoding="utf-8")
    return {
        "snapshot_path": str(snapshot),
        "snapshot_sha256": snapshot_sha256,
        "train_path": str(train_path),
        "validation_path": str(validation_path),
        "cutoff": cutoff,
        "train_rows": len(train_frame),
        "validation_rows": len(validation_frame),
    }


def train(prepared, *, dag_run_id, epochs=None, seed=None):
    epochs = int(os.environ.get("OLIST_EPOCHS", "5")) if epochs is None else epochs
    seed = int(os.environ.get("OLIST_SEED", "42")) if seed is None else seed
    lr = float(os.environ.get("OLIST_LR", "0.003"))
    train_frame = pd.read_csv(prepared["train_path"], encoding="utf-8")
    validation_frame = pd.read_csv(prepared["validation_path"], encoding="utf-8")
    mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
    mlflow.set_experiment("olist_training")
    with mlflow.start_run(run_name=dag_run_id, tags={"dag_run_id": dag_run_id}) as run:
        mlflow.log_params({
            "epochs": epochs,
            "seed": seed,
            "lr": lr,
            "cutoff": prepared["cutoff"],
            "train_rows": prepared["train_rows"],
            "validation_rows": prepared["validation_rows"],
            "snapshot_sha256": prepared["snapshot_sha256"],
        })
        mlflow.log_artifact(prepared["snapshot_path"], artifact_path="data")
        predictor, history = fit_model(
            train_frame,
            validation_frame,
            epochs=epochs,
            seed=seed,
            lr=lr,
        )
        for record in history:
            metrics = {
                "train_loss": record["train_loss"],
                "validation_pr_auc": record["validation_pr_auc"],
            }
            mlflow.log_metrics(metrics, step=record["epoch"])
        model_directory = Path(prepared["train_path"]).parent / "model"
        save_predictor(predictor, model_directory)
        mlflow.log_artifacts(str(model_directory), artifact_path="model")
        return run.info.run_id
